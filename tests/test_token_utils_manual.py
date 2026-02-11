
import sys
import os
import unittest

# Add backend directory to sys.path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'backend'))

from unittest.mock import MagicMock
sys.modules["loguru"] = MagicMock()
sys.modules["fastapi"] = MagicMock()
sys.modules["sqlalchemy"] = MagicMock()
sys.modules["sqlalchemy.orm"] = MagicMock()
sys.modules["sqlalchemy.ext.asyncio"] = MagicMock()
sys.modules["pydantic_settings"] = MagicMock()
sys.modules["app.config"] = MagicMock()
sys.modules["app.services.embedding_service"] = MagicMock()
# We need to mock the service module within the package to avoid its imports
sys.modules["app.services.smart_chunking.service"] = MagicMock()

from app.services.smart_chunking.token_utils import estimate_tokens, compute_adaptive_char_limits
from app.services.smart_chunking.types import ChunkConfig, ChunkingStrategy

class TestTokenUtils(unittest.TestCase):
    def test_estimate_tokens(self):
        # English
        text_en = "Hello world this is a test."
        tokens_en = estimate_tokens(text_en)
        print(f"En text: '{text_en}', tokens: {tokens_en}")
        self.assertTrue(tokens_en > 0)

        # Chinese
        text_zh = "你好世界这是一个测试。"
        tokens_zh = estimate_tokens(text_zh)
        print(f"Zh text: '{text_zh}', tokens: {tokens_zh}")
        self.assertTrue(tokens_zh > 0)
        
        # Mixed
        text_mixed = "Hello world 你好世界"
        tokens_mixed = estimate_tokens(text_mixed)
        print(f"Mixed text: '{text_mixed}', tokens: {tokens_mixed}")
        self.assertTrue(tokens_mixed > 0)

    def test_compute_adaptive_char_limits(self):
        # English configuration
        base_tokens = 128
        
        # Pure English text
        text_en = "This is a long English text used for testing adaptive character limits based on token counts. " * 10
        limits_en = compute_adaptive_char_limits(base_tokens, text_en)
        print(f"Base tokens: {base_tokens}, En text limits: {limits_en}")
        # Expect ~4 chars per token -> ~512 chars
        self.assertTrue(400 < limits_en["base_chunk_chars"] < 600)

        # Pure Chinese text
        text_zh = "这是一个用于测试基于Token计数的自适应字符限制的长中文文本。" * 10
        limits_zh = compute_adaptive_char_limits(base_tokens, text_zh)
        print(f"Base tokens: {base_tokens}, Zh text limits: {limits_zh}")
        # Expect ~1.5 chars per token -> ~192 chars
        self.assertTrue(150 < limits_zh["base_chunk_chars"] < 300)

    def test_chunk_config_initialization(self):
        config = ChunkConfig(
            strategy=ChunkingStrategy.HYBRID,
            use_token_based=True,
            base_chunk_tokens=128,
            overlap_tokens=16,
            min_semantic_tokens=32,
            max_semantic_tokens=384,
            base_chunk_size=500, # Fallback
            chunk_overlap=50,    # Fallback
        )
        self.assertTrue(config.use_token_based)
        self.assertEqual(config.base_chunk_tokens, 128)
        
        # Test resolving char limits
        text_en = "English text " * 50
        resolved_limits = config.resolve_char_limits(text_en)
        print(f"Resolved limits for English: {resolved_limits}")
        self.assertTrue(resolved_limits.base_chunk_size > 400) # Should utilize token logic

        text_zh = "中文文本" * 50
        resolved_limits_zh = config.resolve_char_limits(text_zh)
        print(f"Resolved limits for Chinese: {resolved_limits_zh}")
        self.assertTrue(resolved_limits_zh.base_chunk_size < 300) # Should utilize token logic

if __name__ == '__main__':
    unittest.main()
