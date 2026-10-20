# services/docs/__init__.py
"""Docs service — personal document RAG backed by Frankenmemory.

The legacy Chroma classes remain explicit compatibility imports in their own
module; normal DocsService/RAGManager construction uses Frankenmemory.
"""

from .service import DocsService, DocChunk, IndexResult
from src.rag_manager import RAGManager
from src.frankenmemory_rag import FrankenmemoryRAG

# Preserve the historical export while making its actual default clear.
VectorRAG = FrankenmemoryRAG

__all__ = [
    "DocsService",
    "DocChunk",
    "IndexResult",
    "RAGManager",
    "FrankenmemoryRAG",
    "VectorRAG",
]
