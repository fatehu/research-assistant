"""
智能分块 - Token 计量工具

统一的 Token 估算 / 字符-Token 互转模块。
解决中英文混合场景下"相同字符数 → 实际 Token 数差异 4~5 倍"的问题。

设计原则:
  - 纯 CPU 计算，零外部依赖
  - 对中/英/混合文本使用不同的字符/Token 比率
  - 所有尺寸参数以 Token 为内部单位，对外暴露 Token + 字符双视角

Token/字符比率参考 (基于 bge-m3 / cl100k_base 等常见 tokenizer 的经验值):
  英文:  ~4.0   字符 ≈ 1 Token  (含空格)
  中文:  ~1.5   字符 ≈ 1 Token  (一个汉字通常拆为 1~2 tokens)
  混合:  按实际中/英字符比例加权

注意:
  这些比率是**估算值**。不同的 embedding 模型使用不同的 tokenizer，
  实际比率可能存在 ±20% 的偏差。但对于分块尺寸控制而言，
  估算值已经远优于一刀切的纯字符计数。
"""
import re
from typing import Tuple


# ============== 比率常量 ==============

# 英文: 约 4 个字符（含空格/标点）= 1 Token
CHARS_PER_TOKEN_EN: float = 4.0

# 中文: 约 1.5 个字符 = 1 Token (一个汉字 ≈ 1~2 tokens，加上标点)
CHARS_PER_TOKEN_ZH: float = 1.5

# CJK Unicode 范围 (中日韩统一表意文字)
_CJK_PATTERN = re.compile(
    r'[\u4e00-\u9fff'      # CJK Unified Ideographs
    r'\u3400-\u4dbf'        # CJK Unified Ideographs Extension A
    r'\uf900-\ufaff'        # CJK Compatibility Ideographs
    r'\u3000-\u303f'        # CJK Symbols and Punctuation
    r'\uff00-\uffef]'       # Halfwidth and Fullwidth Forms
)


# ============== 核心函数 ==============

def count_cjk_chars(text: str) -> int:
    """统计文本中的 CJK 字符数"""
    return len(_CJK_PATTERN.findall(text))


def detect_language_ratio(text: str) -> Tuple[float, float]:
    """
    检测文本的中/英语言比例。

    返回:
        (cjk_ratio, non_cjk_ratio)  两者之和为 1.0
    """
    if not text:
        return (0.0, 1.0)

    total_chars = len(text)
    cjk_chars = count_cjk_chars(text)
    non_cjk_chars = total_chars - cjk_chars

    cjk_ratio = cjk_chars / total_chars
    non_cjk_ratio = non_cjk_chars / total_chars

    return (cjk_ratio, non_cjk_ratio)


def estimate_tokens(text: str) -> int:
    """
    估算文本的 Token 数量。

    比 document_service.estimate_tokens 更精确:
    - 按实际中/英字符比例加权
    - 最小返回 1（非空文本）
    """
    if not text or not text.strip():
        return 0

    cjk_chars = count_cjk_chars(text)
    non_cjk_chars = len(text) - cjk_chars

    tokens = cjk_chars / CHARS_PER_TOKEN_ZH + non_cjk_chars / CHARS_PER_TOKEN_EN

    return max(1, int(tokens))


def tokens_to_chars(token_count: int, text_sample: str = "") -> int:
    """
    将 Token 数转换为估算的字符数。

    如果提供 text_sample，会根据样本的实际中/英比例来估算；
    否则使用默认的英文比率。

    用于: 将用户配置的 Token 尺寸转换为内部字符尺寸。
    """
    if token_count <= 0:
        return 0

    if text_sample:
        cjk_ratio, non_cjk_ratio = detect_language_ratio(text_sample)
        # 加权平均: 每个 Token 对应多少字符
        chars_per_token = (
            cjk_ratio * CHARS_PER_TOKEN_ZH +
            non_cjk_ratio * CHARS_PER_TOKEN_EN
        )
    else:
        # 无样本时使用英文比率（保守估计，偏大）
        chars_per_token = CHARS_PER_TOKEN_EN

    return max(1, int(token_count * chars_per_token))


def chars_to_tokens(char_count: int, text_sample: str = "") -> int:
    """
    将字符数转换为估算的 Token 数。

    用于: 将旧的字符配置转换为 Token 尺寸（兼容迁移）。
    """
    if char_count <= 0:
        return 0

    if text_sample:
        cjk_ratio, non_cjk_ratio = detect_language_ratio(text_sample)
        chars_per_token = (
            cjk_ratio * CHARS_PER_TOKEN_ZH +
            non_cjk_ratio * CHARS_PER_TOKEN_EN
        )
    else:
        chars_per_token = CHARS_PER_TOKEN_EN

    return max(1, int(char_count / chars_per_token))


def compute_adaptive_char_limits(
    base_tokens: int,
    text: str,
    *,
    min_tokens: int = 0,
    max_tokens: int = 0,
    overlap_tokens: int = 0,
) -> dict:
    """
    根据文本的实际语言组成，将 Token 配置转换为自适应的字符限制。

    参数:
        base_tokens: 基础块大小（Token）
        text: 待分块的文本（用于检测语言比例）
        min_tokens: 最小语义块大小（Token），0 表示不设
        max_tokens: 最大语义块大小（Token），0 表示不设
        overlap_tokens: 块重叠大小（Token），0 表示不设

    返回:
        {
            "base_chunk_chars": int,
            "min_semantic_chars": int,
            "max_semantic_chars": int,
            "overlap_chars": int,
            "language_ratio": {"cjk": float, "non_cjk": float},
            "chars_per_token": float,
        }
    """
    cjk_ratio, non_cjk_ratio = detect_language_ratio(text)
    chars_per_token = (
        cjk_ratio * CHARS_PER_TOKEN_ZH +
        non_cjk_ratio * CHARS_PER_TOKEN_EN
    )

    # 安全下限
    chars_per_token = max(chars_per_token, 1.0)

    return {
        "base_chunk_chars": max(1, int(base_tokens * chars_per_token)),
        "min_semantic_chars": max(1, int(min_tokens * chars_per_token)) if min_tokens else 0,
        "max_semantic_chars": max(1, int(max_tokens * chars_per_token)) if max_tokens else 0,
        "overlap_chars": max(0, int(overlap_tokens * chars_per_token)) if overlap_tokens else 0,
        "language_ratio": {"cjk": round(cjk_ratio, 3), "non_cjk": round(non_cjk_ratio, 3)},
        "chars_per_token": round(chars_per_token, 2),
    }
