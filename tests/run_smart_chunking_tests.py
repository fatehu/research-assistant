
import sys
import os
import asyncio
from unittest.mock import MagicMock, AsyncMock

# Add backend directory to sys.path
backend_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'backend'))
sys.path.append(backend_path)

# Mock modules
sys.modules["loguru"] = MagicMock()
sys.modules["fastapi"] = MagicMock()
sys.modules["sqlalchemy"] = MagicMock()
sys.modules["sqlalchemy.orm"] = MagicMock()
sys.modules["sqlalchemy.ext.asyncio"] = MagicMock()
sys.modules["pydantic_settings"] = MagicMock()
sys.modules["app.config"] = MagicMock()

# Mock embedding service
embedding_service_mock = MagicMock()
# embed_texts should return a list of lists of floats
async def mock_embed_texts(texts):
    return [[0.1] * 768 for _ in texts]
embedding_service_mock.embed_texts = AsyncMock(side_effect=mock_embed_texts)

# Mock app.services.embedding_service module
embedding_service_module_mock = MagicMock()
embedding_service_module_mock.embedding_service = embedding_service_mock
sys.modules["app.services.embedding_service"] = embedding_service_module_mock

# Mock app.core.database
sys.modules["app.core.database"] = MagicMock()

# Run pytest
import pytest

if __name__ == "__main__":
    # Target the specific test file
    test_file = os.path.join(backend_path, "tests", "test_smart_chunking.py")
    sys.exit(pytest.main([test_file, "-v"]))
