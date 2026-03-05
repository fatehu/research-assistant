"""
鏂囨。澶勭悊鏈嶅姟 - 瑙ｆ瀽銆佸垎鐗囥€乪mbedding

V3 鍙樻洿:
  - estimate_tokens 濮旀墭缁?smart_chunking.token_utils锛堟洿绮剧‘鐨勪腑鑻辨枃浼扮畻锛?
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
    """Text splitter."""

    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
    
    def split_text(self, text: str) -> List[Tuple[str, int, int]]:
        """
        鍒嗗壊鏂囨湰
        杩斿洖: [(chunk_text, start_char, end_char), ...]
        """
        if not text:
            return []
        
        # 娓呯悊鏂囨湰
        text = self._clean_text(text)
        
        if len(text) <= self.chunk_size:
            return [(text, 0, len(text))]
        
        chunks = []
        start = 0
        
        while start < len(text):
            # 璁＄畻缁撴潫浣嶇疆
            end = start + self.chunk_size
            
            if end >= len(text):
                # 鏈€鍚庝竴鍧?
                chunk = text[start:]
                if chunk.strip():
                    chunks.append((chunk, start, len(text)))
                break
            
            # 灏濊瘯鍦ㄥ彞瀛愯竟鐣屽垎鍓?
            end = self._find_sentence_boundary(text, start, end)
            
            chunk = text[start:end]
            if chunk.strip():
                chunks.append((chunk, start, end))
            
            # 璁＄畻涓嬩竴鍧楃殑璧峰浣嶇疆锛堣€冭檻閲嶅彔锛?
            start = end - self.chunk_overlap
            if start <= chunks[-1][1] if chunks else 0:
                start = end
        
        return chunks
    
    def _clean_text(self, text: str) -> str:
        """娓呯悊鏂囨湰"""
        # 缁熶竴鎹㈣绗?
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        # 绉婚櫎澶氫綑绌虹櫧
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r' {2,}', ' ', text)
        return text.strip()
    
    def _find_sentence_boundary(self, text: str, start: int, end: int) -> int:
        """鎵惧埌鍙ュ瓙杈圭晫"""
        # 浼樺厛鍦ㄦ钀借竟鐣屽垎鍓?
        last_para = text.rfind('\n\n', start, end)
        if last_para > start + self.chunk_size // 2:
            return last_para + 2
        
        # 鐒跺悗鍦ㄥ彞瀛愯竟鐣屽垎鍓?
        sentence_endings = ['. ', '! ', '? ', '; ']
        best_pos = end
        
        for ending in sentence_endings:
            pos = text.rfind(ending, start, end)
            if pos > start + self.chunk_size // 2:
                if pos + len(ending) < best_pos:
                    best_pos = pos + len(ending)
                break
        
        # 鏈€鍚庡湪鎹㈣澶勫垎鍓?
        if best_pos == end:
            last_newline = text.rfind('\n', start, end)
            if last_newline > start + self.chunk_size // 2:
                return last_newline + 1
        
        return best_pos


class DocumentProcessor:
    """Document processor."""
    
    SUPPORTED_TYPES = {
        'txt': 'text/plain',
        'md': 'text/markdown',
        'markdown': 'text/markdown',
        'pdf': 'application/pdf',
        'html': 'text/html',
        'htm': 'text/html',
    }

    _CAPTION_PREFIXES = ("figure", "fig", "table", "tab")
    _SENTENCE_ENDINGS = (".", "!", "?", ";")
    
    _PYPDF_HEADER_RATIO = 0.08
    _PYPDF_FOOTER_RATIO = 0.06
    _BACK_MATTER_HEADING = re.compile(
        r"^(references|bibliography|acknowledg(?:e)?ments?|supplementary(?: materials?)?|appendix)\b",
        re.IGNORECASE,
    )
    _REFERENCE_LINE = re.compile(
        r"^(\[\d+\]|\d+\.)\s+.+|.+\b(?:doi|pmid|arxiv)\b.+",
        re.IGNORECASE,
    )

    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        self.splitter = TextSplitter(chunk_size, chunk_overlap)
        self.last_pdf_extractor: Optional[str] = None
    
    async def extract_text(self, file_path: str, file_type: str) -> str:
        """浠庢枃浠朵腑鎻愬彇鏂囨湰"""
        file_type = file_type.lower().replace('.', '')
        
        if file_type in ['txt', 'md', 'markdown']:
            return await self._extract_text_file(file_path)
        elif file_type == 'pdf':
            return await self._extract_pdf(file_path)
        elif file_type in ['html', 'htm']:
            return await self._extract_html(file_path)
        else:
            raise ValueError(f"涓嶆敮鎸佺殑鏂囦欢绫诲瀷: {file_type}")
    
    async def _extract_text_file(self, file_path: str) -> str:
        """Extract text from plain text/markdown files."""
        encodings = ['utf-8', 'gbk', 'gb2312', 'latin-1']
        
        for encoding in encodings:
            try:
                with open(file_path, 'r', encoding=encoding) as f:
                    return f.read()
            except UnicodeDecodeError:
                continue
        
        raise ValueError("鏃犳硶瑙ｇ爜鏂囦欢")
    
    async def _extract_pdf(self, file_path: str) -> str:
        """鎻愬彇 PDF 鏂囨湰"""
        self.last_pdf_extractor = None

        # 浼樺厛浣跨敤 layout-aware 瑙ｆ瀽鍣紝鍑忓皯鍥捐〃 OCR 纰庣墖鍣０
        layout_text = self._extract_pdf_with_layout_parser(file_path)
        if layout_text:
            return layout_text

        try:
            import pypdf

            normalized = self._extract_pdf_with_pypdf_clean(file_path)
            if normalized:
                self.last_pdf_extractor = "pypdf"
                return normalized
            logger.warning("pypdf 鎻愬彇缁撴灉涓虹┖锛屽皾璇曚娇鐢?pdfplumber")
        except ImportError:
            logger.warning("pypdf 鏈畨瑁咃紝灏濊瘯浣跨敤 pdfplumber")
        except Exception as e:
            logger.warning(f"pypdf 鎻愬彇澶辫触锛屽皾璇曚娇鐢?pdfplumber: {e}")

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
            raise ValueError("pypdf or pdfplumber is required to parse PDF files")

        raise ValueError("PDF text extraction failed: no usable content")

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
                logger.info(f"{parser_name} 鏈畨瑁咃紝璺宠繃 layout 瑙ｆ瀽")
                continue
            except Exception as e:
                logger.warning(f"{parser_name} layout 瑙ｆ瀽澶辫触: {e}")
                continue

            normalized = self._normalize_extracted_text(raw_text)
            if self._is_layout_text_usable(normalized):
                quality = self._layout_quality_metrics(normalized)
                if self._is_layout_text_degraded(quality):
                    logger.warning(
                        f"{parser_name} 缁撴灉鐤戜技纰庣墖鍖栵紝璺宠繃 layout 鏂囨湰骞跺皾璇曞洖閫€: "
                        f"fragment={quality['fragment_ratio']:.2f}, "
                        f"sentence_like={quality['sentence_like_ratio']:.2f}, "
                        f"lines={quality['lines']}"
                    )
                    continue

                self.last_pdf_extractor = parser_name
                logger.info(f"PDF 浣跨敤 {parser_name} 瀹屾垚 layout 瑙ｆ瀽")
                return normalized

            logger.warning(
                f"{parser_name} extracted text too short, skip (chars={len(normalized)})"
            )

        return None

    @staticmethod
    def _resolve_layout_parser_order(parser_preference: str) -> List[str]:
        if parser_preference == "markitdown":
            return ["markitdown"]
        if parser_preference == "docling":
            return ["docling"]
        if parser_preference not in {"auto", "markitdown", "docling", "none"}:
            logger.warning(f"鏈煡 pdf_layout_parser={parser_preference}锛屽洖閫€鍒?auto")
        return ["markitdown", "docling"]

    @staticmethod
    def _normalize_extracted_text(text: Optional[str]) -> str:
        if not text:
            return ""
        normalized = text.replace('\r\n', '\n').replace('\r', '\n')
        normalized = re.sub(r'\n{3,}', '\n\n', normalized)
        return normalized.strip()

    def _extract_pdf_with_pypdf_clean(self, file_path: str) -> str:
        import pypdf

        pages_lines: list[list[str]] = []
        with open(file_path, 'rb') as f:
            reader = pypdf.PdfReader(f)
            for page in reader.pages:
                page_text = self._extract_page_text_with_vertical_crop(page)
                lines = self._normalize_pdf_lines(page_text)
                lines = self._merge_hyphenated_lines(lines)
                pages_lines.append(lines)

        pages_lines = self._drop_repeated_edge_lines(pages_lines)
        pages_lines = self._drop_back_matter_lines(pages_lines)
        merged = "\n\n".join("\n".join(lines) for lines in pages_lines if lines)
        return self._normalize_extracted_text(merged)

    def _extract_page_text_with_vertical_crop(self, page: Any) -> str:
        page_height = 0.0
        try:
            page_height = float(getattr(page.mediabox, "height", 0.0) or 0.0)
        except Exception:
            page_height = 0.0

        if page_height <= 0:
            return str(page.extract_text() or "")

        top_y = page_height * (1.0 - float(self._PYPDF_HEADER_RATIO))
        bottom_y = page_height * float(self._PYPDF_FOOTER_RATIO)
        fragments: list[str] = []

        def _visitor(text: str, cm, tm, font_dict, font_size):  # type: ignore[no-untyped-def]
            if not text:
                return
            y = 0.0
            try:
                y = float(tm[5] or 0.0)
            except Exception:
                y = 0.0
            if bottom_y <= y <= top_y:
                fragments.append(str(text))

        try:
            page.extract_text(visitor_text=_visitor)
        except Exception:
            return str(page.extract_text() or "")

        if not fragments:
            return str(page.extract_text() or "")
        return "".join(fragments)

    @staticmethod
    def _normalize_pdf_lines(text: str) -> list[str]:
        raw = str(text or "").replace('\r\n', '\n').replace('\r', '\n')
        lines: list[str] = []
        for line in raw.split('\n'):
            cleaned = re.sub(r'\s+', ' ', line).strip()
            if not cleaned:
                continue
            if re.fullmatch(r"(?:page\s*)?\d+(?:\s*/\s*\d+)?", cleaned, re.IGNORECASE):
                continue
            lines.append(cleaned)
        return lines

    @staticmethod
    def _merge_hyphenated_lines(lines: list[str]) -> list[str]:
        if not lines:
            return []
        output: list[str] = []
        i = 0
        while i < len(lines):
            current = lines[i]
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                if current.endswith('-') and nxt and nxt[0].islower():
                    output.append(current[:-1] + nxt)
                    i += 2
                    continue
            output.append(current)
            i += 1
        return output

    @staticmethod
    def _drop_repeated_edge_lines(pages_lines: list[list[str]]) -> list[list[str]]:
        if len(pages_lines) < 3:
            return pages_lines

        hit: dict[str, int] = {}
        for lines in pages_lines:
            if not lines:
                continue
            edge = lines[:2] + lines[-2:]
            seen: set[str] = set()
            for line in edge:
                key = line.lower()
                if key in seen:
                    continue
                seen.add(key)
                hit[key] = hit.get(key, 0) + 1

        threshold = max(2, int(len(pages_lines) * 0.6))
        repeated = {k for k, v in hit.items() if v >= threshold and len(k) <= 140}
        if not repeated:
            return pages_lines

        cleaned_pages: list[list[str]] = []
        for lines in pages_lines:
            cleaned_pages.append([line for line in lines if line.lower() not in repeated])
        return cleaned_pages

    def _drop_back_matter_lines(self, pages_lines: list[list[str]]) -> list[list[str]]:
        flattened: list[tuple[int, str]] = []
        for page_idx, lines in enumerate(pages_lines):
            for line in lines:
                flattened.append((page_idx, line))
        if not flattened:
            return pages_lines

        heading_idx = -1
        for idx, (_, line) in enumerate(flattened):
            if self._BACK_MATTER_HEADING.match(line):
                heading_idx = idx
                break

        output = [list(lines) for lines in pages_lines]
        if heading_idx >= 0 and heading_idx >= int(len(flattened) * 0.45):
            start_page = flattened[heading_idx][0]
            start_line = flattened[heading_idx][1]
            for page_idx in range(start_page, len(output)):
                new_lines: list[str] = []
                for line in output[page_idx]:
                    if page_idx == start_page and line == start_line:
                        continue
                    if self._is_reference_like_line(line):
                        continue
                    new_lines.append(line)
                output[page_idx] = new_lines
            return output

        for page_idx, lines in enumerate(output):
            output[page_idx] = [line for line in lines if not self._is_reference_like_line(line)]
        return output

    def _is_reference_like_line(self, line: str) -> bool:
        s = str(line or "").strip()
        if not s:
            return False
        if self._REFERENCE_LINE.match(s):
            return True
        if re.search(r"\b(et al\.?|vol\.|pp\.|journal|proceedings)\b", s, flags=re.IGNORECASE):
            if re.search(r"\b(19|20)\d{2}\b", s):
                return True
        if ("https://" in s.lower() or "http://" in s.lower()) and re.search(
            r"\b(doi|pmid|arxiv|available)\b",
            s,
            flags=re.IGNORECASE,
        ):
            return True
        return False

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

        raise ValueError("markitdown 杩斿洖浜嗙┖缁撴灉")

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

        raise ValueError("docling 杩斿洖浜嗙┖缁撴灉")

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
        """鎻愬彇 HTML 鏂囨湰"""
        try:
            from bs4 import BeautifulSoup
            
            with open(file_path, 'r', encoding='utf-8') as f:
                soup = BeautifulSoup(f.read(), 'html.parser')
                
                # 绉婚櫎鑴氭湰鍜屾牱寮?
                for script in soup(['script', 'style']):
                    script.decompose()
                
                return soup.get_text(separator='\n', strip=True)
        except ImportError:
            # 绠€鍗曠殑姝ｅ垯鎻愬彇
            with open(file_path, 'r', encoding='utf-8') as f:
                html = f.read()
            
            # 绉婚櫎鏍囩
            text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
            text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
            text = re.sub(r'<[^>]+>', ' ', text)
            text = re.sub(r'\s+', ' ', text)
            return text.strip()
    
    def chunk_text(self, text: str) -> List[Tuple[str, int, int]]:
        """鍒嗗壊鏂囨湰涓哄涓潡"""
        return self.splitter.split_text(text)
    
    async def embed_chunks(self, chunks: List[str], embedding_svc=None) -> List[List[float]]:
        """涓烘枃鏈潡鐢熸垚宓屽叆鍚戦噺
        
        Args:
            chunks: 鏂囨湰鍧楀垪琛?
            embedding_svc: 鍙€夌殑 EmbeddingService 瀹炰緥銆備负 None 鏃朵娇鐢ㄥ叏灞€榛樿瀹炰緥銆?
        """
        svc = embedding_svc or embedding_service
        return await svc.embed_texts(chunks)
    
    def compute_hash(self, content: str) -> str:
        """璁＄畻鍐呭鍝堝笇"""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def estimate_tokens(self, text: str) -> int:
        """
        浼扮畻 token 鏁伴噺 鈥?濮旀墭缁?token_utils锛堟洿绮剧‘鐨勪腑鑻辨枃鍔犳潈浼扮畻锛?
        """
        try:
            from app.services.smart_chunking.token_utils import estimate_tokens
            return estimate_tokens(text)
        except ImportError:
            # 鍥為€€鍒版棫鐨勭畝鍗曚及绠?
            chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
            other_chars = len(text) - chinese_chars
            chinese_tokens = chinese_chars / 1.5
            other_tokens = other_chars / 4
            return int(chinese_tokens + other_tokens)
    
    @staticmethod
    def get_file_type(filename: str) -> str:
        """鑾峰彇鏂囦欢绫诲瀷"""
        ext = os.path.splitext(filename)[1].lower().replace('.', '')
        return ext if ext else 'txt'


# 鍏ㄥ眬瀹炰緥
document_processor = DocumentProcessor()


def get_document_processor(chunk_size: int = 500, chunk_overlap: int = 50) -> DocumentProcessor:
    """Create a document processor instance."""
    return DocumentProcessor(chunk_size, chunk_overlap)
