"""
文档处理服务 - 解析、分片、embedding
"""
import hashlib
import os
import re
from datetime import datetime
from typing import List, Tuple, Optional
from loguru import logger

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
    
    def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50):
        self.splitter = TextSplitter(chunk_size, chunk_overlap)
    
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
        try:
            import pypdf
            
            text_parts = []
            with open(file_path, 'rb') as f:
                reader = pypdf.PdfReader(f)
                for page in reader.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
            
            return '\n\n'.join(text_parts)
        except ImportError:
            logger.warning("pypdf 未安装，尝试使用 pdfplumber")
            try:
                import pdfplumber
                
                text_parts = []
                with pdfplumber.open(file_path) as pdf:
                    for page in pdf.pages:
                        page_text = page.extract_text()
                        if page_text:
                            text_parts.append(page_text)
                
                return '\n\n'.join(text_parts)
            except ImportError:
                raise ValueError("需要安装 pypdf 或 pdfplumber 来处理 PDF 文件")
    
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
    
    async def embed_chunks(self, chunks: List[str]) -> List[List[float]]:
        """为文本块生成嵌入向量"""
        return await embedding_service.embed_texts(chunks)
    
    def compute_hash(self, content: str) -> str:
        """计算内容哈希"""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def estimate_tokens(self, text: str) -> int:
        """估算 token 数量"""
        # 简单估算：中文约 1.5 字符/token，英文约 4 字符/token
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
