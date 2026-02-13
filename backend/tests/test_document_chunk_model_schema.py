import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.models.knowledge import DocumentChunk


def test_document_chunk_has_embedding_dimension_field():
    assert hasattr(DocumentChunk, "embedding_dimension")


def test_document_chunk_accepts_embedding_dimension_kwarg():
    chunk = DocumentChunk(
        document_id=1,
        knowledge_base_id=1,
        content="test content",
        chunk_index=0,
        embedding_dimension=1024,
    )
    assert chunk.embedding_dimension == 1024
