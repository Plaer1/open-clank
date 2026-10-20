# services/docs/service.py
"""Docs service — personal document RAG."""

from dataclasses import dataclass
from typing import List, Dict, Any, Optional

from src.rag_singleton import get_rag_manager


@dataclass
class DocChunk:
    """A retrieved document chunk."""
    text: str
    source: str
    score: float
    metadata: Dict[str, Any] = None


@dataclass
class IndexResult:
    """Result of indexing documents."""
    indexed: int
    failed: int
    errors: List[str]


class DocsService:
    """
    Document RAG service.

    Usage:
        service = DocsService()
        await service.index("/path/to/docs")
        results = await service.query("what is async await?")
    """

    def __init__(self, persist_dir: Optional[str] = None):
        # The app singleton is the canonical Frankenmemory RAG authority.
        # ``persist_dir`` remains accepted for source compatibility but never
        # selects the retired Chroma backend.
        self.rag = get_rag_manager()
        if self.rag is None:
            raise RuntimeError("Frankenmemory RAG is unavailable")

    async def query(self, query: str, top_k: int = 5, owner: Optional[str] = None) -> List[DocChunk]:
        """
        Query the document index.

        Args:
            query: Search query
            top_k: Number of results

        Returns:
            List of DocChunk objects
        """
        try:
            results = self.rag.search(query, k=top_k, owner=owner)
        except TypeError:
            # Narrow compatibility for older injected test/extension doubles.
            results = self.rag.search(query, k=top_k)
        chunks = []
        for result in results:
            if not isinstance(result, dict):
                continue
            metadata = result.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            chunks.append(
                DocChunk(
                    text=result.get("document", result.get("text", result.get("content", ""))),
                    source=result.get("source", metadata.get("source", "unknown")),
                    score=result.get("similarity", result.get("score", 0.0)),
                    metadata=metadata,
                )
            )
        return chunks

    async def index(self, directory: str, owner: Optional[str] = None) -> IndexResult:
        """
        Index documents from a directory.

        Args:
            directory: Path to documents

        Returns:
            IndexResult with stats
        """
        try:
            result = self.rag.index_personal_documents(directory, owner=owner)
        except TypeError:
            result = self.rag.index_personal_documents(directory)
        return IndexResult(
            indexed=result.get("indexed_count", result.get("indexed", 0)),
            failed=result.get("failed_count", result.get("failed", 0)),
            errors=result.get("errors", []),
        )

    async def add_document(self, text: str, metadata: Dict[str, Any]) -> bool:
        """Add a single document to the index."""
        return self.rag.add_document(text, metadata)

    def get_stats(self) -> Dict[str, Any]:
        """Get index statistics."""
        return self.rag.get_stats()

    def rebuild_index(self) -> bool:
        """Rebuild the entire index."""
        return self.rag.rebuild_index()
