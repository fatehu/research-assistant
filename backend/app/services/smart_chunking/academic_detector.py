"""
智能分块 - 学术文档结构检测器

识别学术论文的章节结构（摘要、引言、方法、结论等）和引用标记。
纯正则匹配，无外部依赖。
"""
import re
from typing import List, Optional


class AcademicStructureDetector:
    """学术文档结构检测器"""

    # 常见的学术论文章节模式（中英文）
    SECTION_PATTERNS = {
        'abstract': [
            r'^#{1,2}\s*(摘要|Abstract|ABSTRACT)\s*$',
            r'^(摘要|Abstract|ABSTRACT)\s*[:：]?\s*$',
        ],
        'introduction': [
            r'^#{1,2}\s*(\d+\.?\s*)?(引言|介绍|Introduction|INTRODUCTION)\s*$',
            r'^(\d+\.?\s*)?(引言|介绍|Introduction)\s*[:：]?\s*$',
        ],
        'related_work': [
            r'^#{1,2}\s*(\d+\.?\s*)?(相关工作|Related Work|RELATED WORK|Literature Review)\s*$',
        ],
        'methodology': [
            r'^#{1,2}\s*(\d+\.?\s*)?(方法|方法论|Methodology|Method|Methods|METHODOLOGY)\s*$',
            r'^#{1,2}\s*(\d+\.?\s*)?(研究方法|Research Method)\s*$',
        ],
        'experiment': [
            r'^#{1,2}\s*(\d+\.?\s*)?(实验|Experiment|Experiments|EXPERIMENTS)\s*$',
        ],
        'results': [
            r'^#{1,2}\s*(\d+\.?\s*)?(结果|Results|RESULTS|Findings)\s*$',
            r'^#{1,2}\s*(\d+\.?\s*)?(结果与讨论|Results and Discussion)\s*$',
        ],
        'discussion': [
            r'^#{1,2}\s*(\d+\.?\s*)?(讨论|Discussion|DISCUSSION)\s*$',
        ],
        'conclusion': [
            r'^#{1,2}\s*(\d+\.?\s*)?(结论|Conclusion|Conclusions|CONCLUSION)\s*$',
            r'^#{1,2}\s*(\d+\.?\s*)?(总结|Summary)\s*$',
        ],
        'references': [
            r'^#{1,2}\s*(参考文献|References|REFERENCES|Bibliography)\s*$',
        ],
        'appendix': [
            r'^#{1,2}\s*(附录|Appendix|APPENDIX)\s*$',
        ],
    }

    # 引用模式
    CITATION_PATTERNS = [
        r'\[(\d+(?:,\s*\d+)*)\]',           # [1], [1, 2, 3]
        r'\(([A-Z][a-z]+(?:\s+(?:et\s+al\.?|and|&)\s+)?[A-Z][a-z]+,?\s*\d{4})\)',  # (Author, 2020)
        r'([A-Z][a-z]+(?:\s+et\s+al\.?)?)\s*\((\d{4})\)',  # Author (2020)
    ]

    @classmethod
    def detect_section_type(cls, text: str) -> Optional[str]:
        """检测章节类型"""
        first_line = text.split('\n')[0].strip()

        for section_type, patterns in cls.SECTION_PATTERNS.items():
            for pattern in patterns:
                if re.match(pattern, first_line, re.IGNORECASE):
                    return section_type

        return None

    @classmethod
    def extract_section_title(cls, text: str) -> Optional[str]:
        """提取章节标题"""
        lines = text.split('\n')
        for line in lines[:3]:  # 只检查前3行
            line = line.strip()
            # 检测 Markdown 标题
            if line.startswith('#'):
                return re.sub(r'^#+\s*', '', line)
            # 检测数字编号标题
            if re.match(r'^(\d+\.)+\d*\.?\s+\S', line):
                return line
        return None

    @classmethod
    def has_citations(cls, text: str) -> bool:
        """检测是否包含引用"""
        for pattern in cls.CITATION_PATTERNS:
            if re.search(pattern, text):
                return True
        return False

    @classmethod
    def extract_citations(cls, text: str) -> List[str]:
        """提取引用"""
        citations = []
        for pattern in cls.CITATION_PATTERNS:
            matches = re.findall(pattern, text)
            if matches:
                if isinstance(matches[0], str):
                    citations.extend(matches)
                else:
                    for m in matches:
                        if isinstance(m, tuple):
                            citations.extend(list(m))
                        else:
                            citations.append(m)
        return list(set(citations))
