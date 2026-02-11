
import sys
import os
import unittest
import asyncio

# In Docker, the app package should be importable directly
# if /app is in PYTHONPATH (which it usually is in Dockerfile)

from app.services.smart_chunking.token_utils import estimate_tokens, compute_adaptive_char_limits
from app.services.smart_chunking.types import ChunkConfig, ResolvedCharLimits

class TestTokenMetricsDocker(unittest.TestCase):
    """
    Test token metrics logic inside the Docker container with real dependencies.
    """

    def test_estimate_tokens(self):
        # Pure English
        text_en = "Hello world this is a test."
        tokens_en = estimate_tokens(text_en)
        # 6 words + period -> ~7 tokens? cl100k_base might vary.
        # Our simple estimator:
        # non_cjk / 4.0
        # len("Hello world this is a test.") = 27 chars.
        # 27 / 4 = 6.75 -> 6
        print(f"En text: '{text_en}', tokens: {tokens_en}")
        self.assertTrue(tokens_en > 0)
        self.assertTrue(tokens_en < 20)

        # Pure Chinese
        text_zh = "你好世界这是一个测试。"
        tokens_zh = estimate_tokens(text_zh)
        # 11 chars.
        # 11 / 1.5 = 7.33 -> 7
        print(f"Zh text: '{text_zh}', tokens: {tokens_zh}")
        self.assertTrue(tokens_zh > 0)
        self.assertTrue(tokens_zh < 20)

        # Mixed
        text_mixed = "Hello world 你好世界"
        tokens_mixed = estimate_tokens(text_mixed)
        # "Hello world " (12 chars) -> 3 tokens
        # "你好世界" (4 chars) -> 2 tokens
        # Total ~5.
        print(f"Mixed text: '{text_mixed}', tokens: {tokens_mixed}")
        self.assertTrue(tokens_mixed > 0)

    def test_compute_adaptive_char_limits(self):
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
            use_token_based=True,
            base_chunk_tokens=100,
            overlap_tokens=10,
            min_semantic_tokens=20,
            max_semantic_tokens=200
        )
        self.assertTrue(config.use_token_based)
        
        # Test resolution
        text = "This is a test text."
        resolved = config.resolve_char_limits(text)
        self.assertIsInstance(resolved, ResolvedCharLimits)
        self.assertTrue(resolved.is_token_based)
        # 100 tokens * 4 chars/token = 400 chars (approx)
        self.assertTrue(300 < resolved.base_chunk_size < 500)

if __name__ == "__main__":
    unittest.main()
