"""
文档处理服务 - 解析、分片、embedding

V3 变更:
  - estimate_tokens 委托给 smart_chunking.token_utils（更精确的中英文估算）
"""
import hashlib
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Tuple, Optional
from loguru import logger

from app.config import settings
from app.services.embedding_service import embedding_service


class TextSplitter:
    """文本分割器"""
    
    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
    
    def split_text(self, text: str) -> List[Tuple[str, int, int]]:
        """
        分割文本
        返回: [(chunk_text, start_char, end_char), ...]
        """
        if not text:
            return []
        
        # 清理文本
        text = self._clean_text(text)
        
        if len(text) <= self.chunk_size:
            return [(text, 0, len(text))]
        
        chunks = []
        start = 0
        
        while start < len(text):
            # 计算结束位置
            end = start + self.chunk_size
            
            if end >= len(text):
                # 最后一块
                chunk = text[start:]
                if chunk.strip():
                    chunks.append((chunk, start, len(text)))
                break
            
            # 尝试在句子边界分割
            end = self._find_sentence_boundary(text, start, end)
            
            chunk = text[start:end]
            if chunk.strip():
                chunks.append((chunk, start, end))
            
            # 计算下一块的起始位置（考虑重叠）
            start = end - self.chunk_overlap
            if start <= chunks[-1][1] if chunks else 0:
                start = end
        
        return chunks
    
    def _clean_text(self, text: str) -> str:
        """清理文本"""
        # 统一换行符
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        # 移除多余空白
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)
        return text.strip()
    
    def _find_sentence_boundary(self, text: str, start: int, end: int) -> int:
        """找到句子边界"""
        # 优先在段落边界分割
        last_para = text.rfind('\n\n', start, end)
        if last_para > start + self.chunk_size // 2:
            return last_para + 2
        
        # 然后在句子边界分割
        sentence_endings = ['. ', '。', '！', '？', '! ', '? ', '；', ';']
        best_pos = end
        
        for ending in sentence_endings:
            pos = text.rfind(ending, start, end)
            if pos > start + self.chunk_size // 2:
                if pos + len(ending) < best_pos:
                    best_pos = pos + len(ending)
                break
        
        # 最后在换行处分割
        if best_pos == end:
            last_newline = text.rfind('\n', start, end)
            if last_newline > start + self.chunk_size // 2:
                return last_newline + 1
        
        return best_pos


class DocumentProcessor:
    """文档处理器"""
    
    SUPPORTED_TYPES = {
        'txt': 'text/plain',
        'md': 'text/markdown',
        'markdown': 'text/markdown',
        'pdf': 'application/pdf',
        'html': 'text/html',
        'htm': 'text/html',
    }

    _CAPTION_PREFIXES = ("figure", "fig", "table", "tab", "图", "表")
    _SENTENCE_ENDINGS = (".", "!", "?", "。", "！", "？")
    
    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        self.splitter = TextSplitter(chunk_size, chunk_overlap)
        self.last_pdf_extractor: Optional[str] = None
    
    async def extract_text(self, file_path: str, file_type: str) -> str:
        """从文件中提取文本"""
        file_type = file_type.lower().replace('.', '')
        
        if file_type in ['txt', 'md', 'markdown']:
            return await self._extract_text_file(file_path)
        elif file_type == 'pdf':
            return await self._extract_pdf(file_path)
        elif file_type in ['html', 'htm']:
            return await self._extract_html(file_path)
        else:
            raise ValueError(f"不支持的文件类型: {file_type}")
    
    async def _extract_text_file(self, file_path: str) -> str:
        """提取纯文本文件"""
        encodings = ['utf-8', 'gbk', 'gb2312', 'latin-1']
        
        for encoding in encodings:
            try:
                with open(file_path, 'r', encoding=encoding) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        
        raise ValueError("无法解码文件")
    
    async def _extract_pdf(self, file_path: str) -> str:
        """提取 PDF 文本"""
        self.last_pdf_extractor = None

        # 优先使用 layout-aware 解析器，减少图表 OCR 碎片噪声
        layout_text = self._extract_pdf_with_layout_parser(file_path)
        if layout_text:
            return layout_text

        try:
            import pypdf

            text_parts = []
            with open(file_path, 'rb') as f:
                reader = pypdf.PdfReader(f)
                for page in reader.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)

            normalized = self._normalize_extracted_text('\n\n'.join(text_parts))
            if normalized:
                self.last_pdf_extractor = "pypdf"
                return normalized
            logger.warning("pypdf 提取结果为空，尝试使用 pdfplumber")
        except ImportError:
            logger.warning("pypdf 未安装，尝试使用 pdfplumber")
        except Exception as e:
            logger.warning(f"pypdf 提取失败，尝试使用 pdfplumber: {e}")

        try:
            import pdfplumber

            text_parts = []
            with pdfplumber.open(file_path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)

            normalized = self._normalize_extracted_text('\n\n'.join(text_parts))
            if normalized:
                self.last_pdf_extractor = "pdfplumber"
                return normalized
        except ImportError:
            raise ValueError("需要安装 pypdf 或 pdfplumber 来处理 PDF 文件")

        raise ValueError("PDF 文本提取失败：无法获得有效文本内容")

    def _extract_pdf_with_layout_parser(self, file_path: str) -> Optional[str]:
        parser_preference = (getattr(settings, "pdf_layout_parser", "auto") or "auto").lower()
        if parser_preference == "none":
            return None

        for parser_name in self._resolve_layout_parser_order(parser_preference):
            extractor = getattr(self, f"_extract_pdf_with_{parser_name}", None)
            if extractor is None:
                continue

            try:
                raw_text = extractor(file_path)
            except ImportError:
                logger.info(f"{parser_name} 未安装，跳过 layout 解析")
                continue
            except Exception as e:
                logger.warning(f"{parser_name} layout 解析失败: {e}")
                continue

            normalized = self._normalize_extracted_text(raw_text)
            if self._is_layout_text_usable(normalized):
                quality = self._layout_quality_metrics(normalized)
                if self._is_layout_text_degraded(quality):
                    logger.warning(
                        f"{parser_name} 结果疑似碎片化，跳过 layout 文本并尝试回退: "
                        f"fragment={quality['fragment_ratio']:.2f}, "
                        f"sentence_like={quality['sentence_like_ratio']:.2f}, "
                        f"lines={quality['lines']}"
                    )
                    continue

                self.last_pdf_extractor = parser_name
                logger.info(f"PDF 使用 {parser_name} 完成 layout 解析")
                return normalized

            logger.warning(
                f"{parser_name} 提取文本长度不足，跳过（chars={len(normalized)}）"
            )

        return None

    @staticmethod
    def _resolve_layout_parser_order(parser_preference: str) -> List[str]:
        if parser_preference == "markitdown":
            return ["markitdown"]
        if parser_preference == "docling":
            return ["docling"]
        if parser_preference not in {"auto", "markitdown", "docling", "none"}:
            logger.warning(f"未知 pdf_layout_parser={parser_preference}，回退到 auto")
        return ["markitdown", "docling"]

    @staticmethod
    def _normalize_extracted_text(text: Optional[str]) -> str:
        if not text:
            return ""
        normalized = text.replace('\r\n', '\n').replace('\r', '\n')
        normalized = re.sub(r'\n{3,}', '\n\n', normalized)
        return normalized.strip()

    @staticmethod
    def _is_layout_text_usable(text: str) -> bool:
        min_chars = max(int(getattr(settings, "pdf_layout_min_chars", 200)), 0)
        return len(text) >= min_chars

    @classmethod
    def _layout_quality_metrics(cls, text: str) -> Dict[str, float]:
        lines = [
            line.strip()
            for line in text.split('\n')
            if line.strip()
        ]
        if not lines:
            return {
                "lines": 0,
                "fragment_ratio": 1.0,
                "sentence_like_ratio": 0.0,
            }

        def is_fragment(line: str) -> bool:
            if len(line) <= 2:
                return True

            lower = line.lower()
            if lower.startswith(cls._CAPTION_PREFIXES):
                return False

            if len(line) < 20 and not line.endswith(cls._SENTENCE_ENDINGS):
                return True

            alpha = sum(1 for ch in line if ch.isalpha())
            if len(line) > 0 and alpha / len(line) < 0.45 and len(line) < 80:
                return True

            return False

        fragment_count = sum(1 for line in lines if is_fragment(line))
        sentence_like_count = sum(
            1 for line in lines
            if len(line) >= 30 and line.endswith(cls._SENTENCE_ENDINGS)
        )
        return {
            "lines": float(len(lines)),
            "fragment_ratio": fragment_count / len(lines),
            "sentence_like_ratio": sentence_like_count / len(lines),
        }

    @staticmethod
    def _is_layout_text_degraded(metrics: Dict[str, float]) -> bool:
        return (
            metrics["lines"] >= 200
            and metrics["fragment_ratio"] >= 0.45
            and metrics["sentence_like_ratio"] <= 0.12
        )

    @classmethod
    def _extract_pdf_with_markitdown(cls, file_path: str) -> str:
        from markitdown import MarkItDown

        converter = MarkItDown()
        result = converter.convert(file_path)

        for attr in ("text_content", "markdown", "text", "content"):
            value = cls._as_text(getattr(result, attr, None))
            if value and value.strip():
                return value

        value = cls._as_text(result)
        if value and value.strip():
            return value

        raise ValueError("markitdown 返回了空结果")

    @classmethod
    def _extract_pdf_with_docling(cls, file_path: str) -> str:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        conversion = converter.convert(file_path)
        document = getattr(conversion, "document", conversion)

        for method_name in ("export_to_markdown", "export_to_text"):
            method = getattr(document, method_name, None)
            if callable(method):
                value = cls._as_text(method())
                if value and value.strip():
                    return value

        for attr in ("markdown", "text", "content"):
            value = cls._as_text(getattr(document, attr, None))
            if value and value.strip():
                return value

        value = cls._as_text(document)
        if value and value.strip():
            return value

        raise ValueError("docling 返回了空结果")

    @staticmethod
    def _as_text(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return None
    
    async def _extract_html(self, file_path: str) -> str:
        """提取 HTML 文本"""
        try:
            from bs4 import BeautifulSoup
            
            with open(file_path, 'r', encoding='utf-8') as f:
                soup = BeautifulSoup(f.read(), 'html.parser')
                
                # 移除脚本和样式
                for script in soup(['script', 'style']):
                    script.decompose()
                
                return soup.get_text(separator='\n', strip=True)
        except ImportError:
            # 简单的正则提取
            with open(file_path, 'r', encoding='utf-8') as f:
                html = f.read()
            
            # 移除标签
            text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text)
            return text.strip()
    
    def chunk_text(self, text: str) -> List[Tuple[str, int, int]]:
        """分割文本为多个块"""
        return self.splitter.split_text(text)
    
    async def embed_chunks(self, chunks: List[str], embedding_svc=None) -> List[List[float]]:
        """为文本块生成嵌入向量
        
        Args:
            chunks: 文本块列表
            embedding_svc: 可选的 EmbeddingService 实例。为 None 时使用全局默认实例。
        """
        svc = embedding_svc or embedding_service
        return await svc.embed_texts(chunks)
    
    def compute_hash(self, content: str) -> str:
        """计算内容哈希"""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def estimate_tokens(self, text: str) -> int:
        """
        估算 token 数量 — 委托给 token_utils（更精确的中英文加权估算）
        """
        try:
            from app.services.smart_chunking.token_utils import estimate_tokens
            return estimate_tokens(text)
        except ImportError:
            # 回退到旧的简单估算
            chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
            other_chars = len(text) - chinese_chars
            chinese_tokens = chinese_chars / 1.5
            other_tokens = other_chars / 4
            return int(chinese_tokens + other_tokens)
    
    @staticmethod
    def get_file_type(filename: str) -> str:
        """获取文件类型"""
        ext = os.path.splitext(filename)[1].lower().replace('.', '')
        return ext if ext else 'txt'


# 全局实例
document_processor = DocumentProcessor()


def get_document_processor(chunk_size: int = 500, chunk_overlap: int = 50) -> DocumentProcessor:
    """获取文档处理器实例"""
    return DocumentProcessor(chunk_size, chunk_overlap)
