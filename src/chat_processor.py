# src/chat_processor.py
import asyncio
import logging
import re
import time
from typing import List, Dict, Any, Optional, Tuple
from src.chat_helpers import extract_urls
from src.youtube_handler import is_youtube_url
from src.search import comprehensive_web_search, fetch_webpage_content
from src.prompt_security import UNTRUSTED_CONTEXT_POLICY, untrusted_context_message

logger = logging.getLogger(__name__)


def _clean_search_query(query: str, max_len: int = 200) -> str:
    """Strip fenced code blocks from a search query while preserving inline
    code text.

    This is a focused, defensive cleanup for the *final* web-search query
    selected in ``build_context_preface`` (issue #4547): regardless of whether
    the query came from the LLM-generated path (#4557) or the first-line
    fallback, residual fenced / inline markdown should not leak into the search
    call. Rather than using regex (which is brittle and strips inline code
    text like ``git reset`` from the query), we render the query to HTML via
    ``markdown`` and parse it with ``BeautifulSoup`` so that:

    * ``<pre>`` blocks (fenced / indented code) are removed entirely.
    * ``<code>`` elements (inline code) are preserved as plain text.

    Both libraries are already project dependencies. The result is whitespace
    collapsed and truncated to ``max_len``; an all-code input collapses to an
    empty string, which the caller treats as "no query".
    """
    import markdown as _md
    from bs4 import BeautifulSoup as _BS

    html = _md.markdown(query, extensions=["fenced_code"])
    soup = _BS(html, "html.parser")

    # Remove fenced / indented code blocks.
    for pre in soup.find_all("pre"):
        pre.decompose()

    # Preserve inline code by unwrapping <code> to text.
    for code in soup.find_all("code"):
        code.replace_with(code.get_text())

    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:max_len]


class ChatProcessor:
    def __init__(self, memory_manager, personal_docs_manager, memory_vector=None, skills_manager=None, memory_provider=None):
        self.memory_manager = memory_manager
        self.personal_docs_manager = personal_docs_manager
        self.memory_vector = memory_vector
        self.skills_manager = skills_manager
        self.memory_provider = memory_provider

    # Minimum similarity score for RAG results to be injected
    RAG_SIMILARITY_THRESHOLD = 0.35

    _digest_warned_at: float = 0.0

    def _warn_digest_failure(self, error: Exception) -> None:
        """Digest failures degrade to "no index card" — chat must proceed.
        Warn at most once a minute so a down engine doesn't spam the log."""
        now = time.monotonic()
        if now - self._digest_warned_at >= 60.0:
            self._digest_warned_at = now
            logger.warning("Memory digest unavailable, continuing without index card: %s", error)

    @staticmethod
    def _trust_prefs(owner) -> dict:
        """Per-user trust toggles for the digest split. Fails CLOSED: any
        prefs problem reads as master-off, so nothing auto-captured gains
        force from a broken prefs file."""
        try:
            from routes.prefs_routes import _load_for_user

            return _load_for_user(owner) or {}
        except Exception:
            return {}

    async def build_context_preface(
        self,
        message: str,
        session: Any,
        use_web: bool = False,
        use_rag: bool = True,
        use_memory: bool = True,
        time_filter: Optional[str] = None,
        preset_system_prompt: Optional[str] = None,
        owner: Optional[str] = None,
        character_name: Optional[str] = None,
        agent_mode: bool = False,
        incognito: bool = False,
        use_skills: bool = True,
    ) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]], List[Dict[str, str]]]:
        """Build the context preface for LLM calls.

        Returns:
            Tuple of (preface messages, rag_sources list)

        Note on KV-cache friendliness: the ``system``-role messages assembled
        here are later concatenated into a single system message and sent as
        the very first thing in the payload (see ``llm_core``'s "consolidate
        system messages" step). Local OpenAI-compatible backends (llama.cpp /
        LM Studio) key their KV cache off the byte-identical token prefix, so
        *anything* that changes turn-to-turn — timestamps, retrieved snippets,
        per-turn counts — must NOT be folded into a system message here. Such
        content belongs in a separate ``user``/context message appended near
        the end of the array (see ``current_datetime_context_message`` and
        ``untrusted_context_message`` callers in ``build_chat_context``),
        which keeps the static system prefix byte-identical across turns of
        the same session and lets the backend reuse its cached prefix.
        """
        preface = []
        rag_sources = []

        # Add preset system prompt if specified
        if preset_system_prompt:
            preface.append({
                "role": "system",
                "content": preset_system_prompt
            })
        preface.append({
            "role": "system",
            "content": UNTRUSTED_CONTEXT_POLICY,
        })

        # Memory: pinned (always included) + extended (provider recall or native)
        self._last_used_memories = []  # track what was injected
        if use_memory:
            if self.memory_provider and hasattr(self.memory_provider, "digest"):
                # Provider index card: what the bank HOLDS, never contents.
                # Replaces push-recall entirely — the model pulls details
                # through the memory search tool when a listed topic matters.
                # Not a "used memory": no record_access, no used_memories
                # bookkeeping (it's an index, not retrieval).
                try:
                    from src.memory_digest import (
                        DIGEST_FETCH_TIMEOUT_SECONDS,
                        render_split,
                    )

                    digest = await asyncio.wait_for(
                        self.memory_provider.digest(owner=owner),
                        timeout=DIGEST_FETCH_TIMEOUT_SECONDS,
                    )
                    trusted_block, card = render_split(
                        digest, self._trust_prefs(owner)
                    )
                    if trusted_block:
                        # T6: endorsed guidance is a system message directly
                        # below the persona — real force, never wrapped
                        # untrusted (insert before the policy message).
                        policy_at = next(
                            (i for i, m in enumerate(preface)
                             if m.get("content") == UNTRUSTED_CONTEXT_POLICY),
                            len(preface),
                        )
                        preface.insert(policy_at, {
                            "role": "system",
                            "content": trusted_block,
                        })
                    if card:
                        preface.append(untrusted_context_message(
                            "saved memory: bank index", card,
                        ))
                except Exception as e:
                    self._warn_digest_failure(e)
            elif self.memory_provider:
                # Provider without digest support: legacy push-recall.
                try:
                    hits = await self.memory_provider.recall(message, owner=owner, top_k=3)
                    if hits:
                        ext_text = "\n".join([f"- {h.memory.text}" for h in hits])
                        preface.append(untrusted_context_message(
                            "saved memory: retrieved context",
                            (
                                "Memory context. Do not reference unless the user asks "
                                f"about these topics.\n{ext_text}"
                            ),
                        ))
                        used_ids = []
                        for h in hits:
                            self._last_used_memories.append({
                                "text": h.memory.text,
                                "category": h.memory.category,
                                "type": "pinned" if h.memory.pinned else "recalled",
                            })
                            if h.memory.id:
                                used_ids.append(h.memory.id)
                        if used_ids:
                            try:
                                await self.memory_provider.record_access(used_ids, owner=owner)
                            except Exception as access_error:
                                logger.warning("Provider access accounting failed: %s", access_error)
                except Exception as e:
                    logger.warning("Provider recall failed: %s", e)
            # No provider → no memory context. Every real boot has one: the
            # registry always yields either frankenmemory or the native
            # provider (app_initializer), so a None here is a test fixture.

            # (skills index injection moved out — see below; only fires in
            # agent mode so chat mode and incognito stay clean.)

        # RAG: search if enabled and rag_manager available, inject only above threshold
        if use_rag:
            try:
                rag_manager = getattr(self.personal_docs_manager, 'rag_manager', None)
                if rag_manager:
                    results = rag_manager.search(message, k=5, owner=owner)
                    # Filter by similarity threshold
                    relevant = [r for r in results if r.get("similarity", 0) >= self.RAG_SIMILARITY_THRESHOLD]
                    if relevant:
                        logger.info(f"RAG: {len(relevant)}/{len(results)} results above threshold {self.RAG_SIMILARITY_THRESHOLD}")
                        rag_sources = [
                            {
                                "filename": r["metadata"].get("filename", r["metadata"].get("source", "unknown")),
                                "snippet": r["document"][:200],
                                "similarity": round(r.get("similarity", 0), 3)
                            }
                            for r in relevant
                        ]
                        rag_content = "Relevant documents:\n\n" + "\n\n---\n\n".join(
                            f"[{s['filename']}]\n{r['document']}" for s, r in zip(rag_sources, relevant)
                        )
                        if len(rag_content) > 10000:
                            rag_content = rag_content[:10000] + "\n[Truncated]"
                        preface.append(untrusted_context_message("retrieved documents", rag_content))
            except Exception as e:
                logger.warning(f"RAG retrieval failed: {e}")

        # Add web search if enabled
        web_sources = []
        if use_web:
            try:
                from src.llm_core import llm_call

                t_url, t_model, t_headers = session.endpoint_url, session.model, session.headers

                # Default fallback is the first non-empty line of the original user message
                fallback_query = next((line.strip() for line in message.split("\n") if line.strip()), "")
                search_query = fallback_query

                try:
                    generated_query = llm_call(
                        t_url,
                        t_model,
                        [
                            {
                                "role": "system",
                                "content": (
                                    "Extract a concise search query from the user's message. "
                                    "Reply ONLY with the query."
                                ),
                            },
                            {"role": "user", "content": message},
                        ],
                        headers=t_headers,
                        temperature=0.1,
                        max_tokens=50,
                        timeout=15,
                    ).strip()

                    if generated_query:
                        # LLM successfully generated a non-empty query -> use the generated query
                        search_query = generated_query
                    else:
                        # LLM returned an empty or whitespace-only query -> fall back to original query
                        logger.warning("LLM generated an empty search query, using fallback.")
                except Exception as e:
                    # LLM failed (exception/error) -> fall back to original user query
                    logger.warning(f"Failed to generate search query via LLM, using fallback: {e}")

                search_query = " ".join(search_query.split())
                if len(search_query) > 150:
                    search_query = search_query[:150].strip()

                # Defensive cleanup of the final selected query (interim fix
                # for #4547): strip any residual fenced/inline markdown so that
                # neither the generated query nor the first-line fallback leaks
                # fences or backticks into the search call. No-op on clean
                # generated queries; collapses to "" when the query is all code.
                search_query = _clean_search_query(search_query, max_len=150)

                if search_query:
                    # Execute web search using the final selected query
                    web_context, web_sources = comprehensive_web_search(
                        search_query, time_filter=time_filter, return_sources=True
                    )
                    preface.append(untrusted_context_message("web search results", web_context))
            except Exception as e:
                logger.error(f"Web search failed: {e}")
                preface.append({"role": "system", "content": "Web search encountered an error and could not retrieve results."})

        # Process non-YouTube URLs in message (YouTube handled by preprocess_message)
        # Skip auto-fetch for long pastes (the user already pasted the content —
        # fetching every embedded link buries the actual question under
        # hundreds of KB of duplicate page HTML and confuses the model) or for
        # link-heavy pastes (>3 URLs typically means it's a boilerplate-laden
        # blog post, not a "summarize this URL" request).
        urls = extract_urls(message)
        non_yt_urls = [u for u in urls if not is_youtube_url(u)]
        skip_url_fetch = len(message) > 2000 or len(non_yt_urls) > 3
        if not skip_url_fetch:
            for url in non_yt_urls:
                result = fetch_webpage_content(url)
                if result.get('success'):
                    content = result.get('content', '')[:10000]
                    preface.append(untrusted_context_message(
                        f"web page: {url}",
                        f"Content from {url}:\n\n{content}",
                    ))

        # Skills index — progressive disclosure. Only injected when the
        # model has the `manage_skills` tool available (agent_mode), and
        # never in incognito mode (the user has explicitly opted out of
        # context retention this turn). In plain chat mode the model can't
        # call the tool anyway, so the index would be noise.
        if agent_mode and not incognito and use_skills and self.skills_manager:
            try:
                idx = self.skills_manager.index_for(owner=owner)
            except Exception as e:
                logger.debug(f"Skills index unavailable: {e}")
                idx = []
            if idx:
                by_cat: Dict[str, list] = {}
                for s in idx:
                    by_cat.setdefault(s.get("category") or "general", []).append(s)
                lines = ["[Available skills — call manage_skills(action='view', name='...') to load one when relevant]"]
                for cat in sorted(by_cat):
                    lines.append(f"  {cat}:")
                    for s in sorted(by_cat[cat], key=lambda x: x["name"]):
                        desc = s.get("description") or ""
                        lines.append(f"    - {s['name']}: {desc}" if desc else f"    - {s['name']}")
                preface.append(untrusted_context_message("available skills index", "\n".join(lines)))

        return preface, rag_sources, web_sources
