"""Compatibility facade for the canonical Frankenmemory document index."""

import hashlib
import json
import logging
from typing import List, Dict, Any, Optional

from src.frankenmemory_rag import FrankenmemoryRAG

# Import-compatible name retained for callers and old tests. It always names
# the canonical backend; the environment can no longer select Chroma.
VectorRAG = FrankenmemoryRAG

logger = logging.getLogger(__name__)

class RAGManager:
    """
    A manager class that wraps the canonical Frankenmemory RAG projection.
    """
    
    def __init__(self, persist_directory: Optional[str] = None):
        """Initialize the sole canonical RAG backend.

        ``persist_directory`` remains accepted only for import compatibility;
        Frankenmemory owns its database path.
        """
        self.vector_rag = VectorRAG()
        logger.info("RAGManager initialized with canonical Frankenmemory backend")
    
    # Delegate all methods to VectorRAG
    def search(
        self,
        query: str,
        k: int = 5,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Search for documents - delegates to VectorRAG."""
        scope = {"owner": owner}
        if workspace_id is not None:
            scope["workspace_id"] = workspace_id
        if project_id is not None:
            scope["project_id"] = project_id
        return self.vector_rag.search(query, k, **scope)

    def search_explain(
        self,
        query: str,
        k: int = 5,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.vector_rag.search_explain(
            query,
            k,
            owner=owner,
            workspace_id=workspace_id,
            project_id=project_id,
        )
    
    def index_personal_documents(
        self,
        directory: str,
        file_extensions: Optional[set] = None,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Index documents - delegates to VectorRAG."""
        scope = {"owner": owner}
        if workspace_id is not None:
            scope["workspace_id"] = workspace_id
        if project_id is not None:
            scope["project_id"] = project_id
        return self.vector_rag.index_personal_documents(
            directory,
            file_extensions=file_extensions,
            **scope,
        )
    
    def retrieve(
        self,
        query: str,
        k: int = 5,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> List[str]:
        """Retrieve relevant chunks - delegates to VectorRAG."""
        scope = {"owner": owner}
        if workspace_id is not None:
            scope["workspace_id"] = workspace_id
        if project_id is not None:
            scope["project_id"] = project_id
        try:
            return self.vector_rag.retrieve(query, k, **scope)
        except TypeError:
            if workspace_id is not None or project_id is not None:
                raise
            return self.vector_rag.retrieve(query, k)
    
    def rebuild_index(self) -> bool:
        """Rebuild index - delegates to VectorRAG."""
        return self.vector_rag.rebuild_index()
    
    def get_stats(self, owner: Optional[str] = None) -> Dict[str, Any]:
        """Get stats - delegates to VectorRAG."""
        return self.vector_rag.get_stats(owner=owner)

    def build_embedding_generation(self, **kwargs) -> Dict[str, Any]:
        return self.vector_rag.build_embedding_generation(**kwargs)

    def resume_embedding_generation(self, **kwargs) -> Dict[str, Any]:
        return self.vector_rag.resume_embedding_generation(**kwargs)

    def publish_generation(self, **kwargs) -> Dict[str, Any]:
        return self.vector_rag.publish_generation(**kwargs)

    def rollback_generation(self, **kwargs) -> Dict[str, Any]:
        return self.vector_rag.rollback_generation(**kwargs)
    
    def add_document(self, text: str, metadata: Dict[str, Any]) -> bool:
        """Add single document - delegates to VectorRAG."""
        return self.vector_rag.add_document(text, metadata)
    
    def add_documents_batch(self, docs: List[tuple]) -> Dict[str, Any]:
        """Add documents in batch - delegates to VectorRAG."""
        return self.vector_rag.add_documents_batch(docs)

    def list_sources(
        self,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.vector_rag.list_sources(
            owner=owner,
            workspace_id=workspace_id,
            project_id=project_id,
        )

    def remove_directory(
        self,
        directory: str,
        *,
        owner: Optional[str] = None,
        workspace_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self.vector_rag.remove_directory(
            directory,
            owner=owner,
            workspace_id=workspace_id,
            project_id=project_id,
        )

    def _canonical_lifecycle_backend(self) -> FrankenmemoryRAG:
        backend = self.vector_rag
        if getattr(backend, "backend", None) != "frankenmemory":
            raise RuntimeError("owner lifecycle requires the canonical Frankenmemory RAG backend")
        if not getattr(backend, "healthy", False):
            raise RuntimeError("canonical Frankenmemory RAG backend is unavailable")
        if not callable(getattr(backend, "_connect", None)):
            raise RuntimeError("canonical Frankenmemory RAG lifecycle connection is unavailable")
        return backend

    def owner_inventory(self, owner: str) -> Dict[str, Any]:
        """Return a path/content-free fingerprint of one owner's RAG rows."""
        owner_key = str(owner or "").strip().lower()
        if not owner_key or "\x00" in owner_key:
            raise ValueError("RAG lifecycle owner is required")
        backend = self._canonical_lifecycle_backend()
        identities: list[str] = []
        canonical_semantics: list[str] = []
        derived_identities: list[str] = []
        counts: Dict[str, int] = {}
        queries = {
            "sources": (
                "SELECT DISTINCT s.source_id,s.workspace_key,s.project_key,s.source_revision,"
                "s.content_hash,s.source_type "
                "FROM fm_v2_sources s JOIN fm_v2_documents d ON "
                "d.owner_id=s.owner_id AND d.source_id=s.source_id AND "
                "d.source_revision=s.source_revision WHERE s.owner_id=? "
                "ORDER BY s.source_id,s.source_revision"
            ),
            "documents": (
                "SELECT document_id,source_id,source_revision,content_hash,parser_version "
                "FROM fm_v2_documents WHERE owner_id=? ORDER BY document_id"
            ),
            "chunks": (
                "SELECT chunk_id,document_id,ordinal,content_hash FROM fm_v2_chunks "
                "WHERE owner_id=? ORDER BY chunk_id"
            ),
            "generations": (
                "SELECT generation_id,logical_space,workspace_key,project_key,state,"
                "retention_state,config_fingerprint,source_watermark,row_count "
                "FROM fm_v2_derived_generations WHERE owner_id=? ORDER BY generation_id"
            ),
            "pointers": (
                "SELECT logical_space,workspace_key,project_key,generation_id,"
                "publication_tx,publication_watermark FROM fm_v2_index_pointers "
                "WHERE owner_id=? ORDER BY logical_space,workspace_key,project_key"
            ),
            "publications": (
                "SELECT publication_id,logical_space,workspace_key,project_key,"
                "previous_generation_id,generation_id,action,publication_watermark "
                "FROM fm_v2_index_publications WHERE owner_id=? ORDER BY publication_id"
            ),
        }
        with backend._connect() as connection:
            for domain, query in queries.items():
                rows = connection.execute(query, (owner_key,)).fetchall()
                counts[domain] = len(rows)
                for row in rows:
                    values = [str(value) if value is not None else "" for value in tuple(row)]
                    identity = domain + "\0" + "\0".join(values)
                    identities.append(identity)
                    if domain == "sources":
                        canonical_semantics.append(domain + "\0" + "\0".join(values[1:]))
                    elif domain == "documents":
                        canonical_semantics.append(domain + "\0" + "\0".join(values[2:]))
                    elif domain == "chunks":
                        canonical_semantics.append(domain + "\0" + "\0".join(values[2:]))
                    else:
                        derived_identities.append(identity)
        digest = hashlib.sha256(
            json.dumps(identities, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        canonical_row_count = sum(counts[key] for key in ("sources", "documents", "chunks"))
        derived_row_count = sum(counts[key] for key in ("generations", "pointers", "publications"))
        return {
            "available": True,
            "row_count": sum(counts.values()),
            "canonical_row_count": canonical_row_count,
            "derived_row_count": derived_row_count,
            "counts": counts,
            "digest": digest,
            "canonical_semantic_digest": hashlib.sha256(
                json.dumps(canonical_semantics, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "derived_digest": hashlib.sha256(
                json.dumps(derived_identities, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }

    def rewrite_owner_paths(
        self,
        owner: str,
        *,
        path_map: Optional[Dict[str, str]] = None,
        path_prefixes: Optional[List[tuple[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Rewrite canonical source paths without performing an owner move."""
        backend = self._canonical_lifecycle_backend()
        rewrite = getattr(backend, "rewrite_owner_paths", None)
        if not callable(rewrite):
            raise RuntimeError("canonical RAG source-path lifecycle is unavailable")
        result = rewrite(
            owner,
            path_map=path_map,
            path_prefixes=path_prefixes,
        )
        if not isinstance(result, dict) or result.get("success") is not True:
            raise RuntimeError("canonical RAG source-path lifecycle failed")
        return dict(result)

    def rename_owner(
        self,
        old_owner: str,
        new_owner: str,
        *,
        path_map: Optional[Dict[str, str]] = None,
        path_prefixes: Optional[List[tuple[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Strictly rename canonical RAG state without merging tenants."""
        source = self.owner_inventory(old_owner)
        target = self.owner_inventory(new_owner)
        if target["row_count"]:
            raise RuntimeError("target RAG owner already contains lifecycle rows")
        if not source["row_count"]:
            return {"success": True, "updated_count": 0, "inventory": source}
        backend = self._canonical_lifecycle_backend()
        result = backend.rename_owner(
            old_owner,
            new_owner,
            path_map=path_map,
            path_prefixes=path_prefixes,
        )
        after_source = self.owner_inventory(old_owner)
        after_target = self.owner_inventory(new_owner)
        canonical_mismatch = (
            after_target["canonical_row_count"] != source["canonical_row_count"]
            or after_target["canonical_semantic_digest"]
            != source["canonical_semantic_digest"]
        )
        if after_source["row_count"] or canonical_mismatch:
            raise RuntimeError("canonical RAG owner rename verification failed")
        return {**dict(result), "before": source, "after": after_target}

    def purge_owner(self, owner: str) -> Dict[str, Any]:
        """Strictly purge canonical RAG state and verify the owner is empty."""
        before = self.owner_inventory(owner)
        backend = self._canonical_lifecycle_backend()
        result = backend.purge_owner(owner)
        after = self.owner_inventory(owner)
        if after["row_count"]:
            raise RuntimeError("canonical RAG owner purge verification failed")
        return {**dict(result), "before": before, "after": after}
