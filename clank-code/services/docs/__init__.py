# services/docs/__init__.py
"""Docs service — personal document RAG backed by Memery.

The legacy Chroma classes remain explicit compatibility imports in their own
module; normal DocsService/RAGManager construction uses Memery.
"""

from .service import DocsService, DocChunk, IndexResult
from src.rag_manager import RAGManager
from src.memery_rag import MemeryRAG

# Preserve the historical export while making its actual default clear.
VectorRAG = MemeryRAG

__all__ = [
    "DocsService",
    "DocChunk",
    "IndexResult",
    "RAGManager",
    "MemeryRAG",
    "VectorRAG",
]
