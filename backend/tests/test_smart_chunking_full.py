"""
智能分块系统 - 全面测试套件 (增强版)

运行: docker-compose exec backend pytest tests/test_smart_chunking_full.py -v --tb=short
快速: docker-compose exec backend pytest tests/test_smart_chunking_full.py -v -k "not semantic" --tb=short
覆盖: docker-compose exec backend pytest tests/test_smart_chunking_full.py --cov=app.services.smart_chunking_service -v
"""

import pytest
import time
from typing import List, Dict, Any


# ============== Fixtures ==============

@pytest.fixture
def service():
    from app.services.smart_chunking_service import SmartChunkingService
    return SmartChunkingService()

@pytest.fixture
def ChunkConfig():
    from app.services.smart_chunking_service import ChunkConfig as CC
    return CC

@pytest.fixture
def ChunkingStrategy():
    from app.services.smart_chunking_service import ChunkingStrategy as CS
    return CS

@pytest.fixture
def ChunkLevel():
    from app.services.smart_chunking_service import ChunkLevel as CL
    return CL

@pytest.fixture
def AcademicDetector():
    from app.services.smart_chunking_service import AcademicStructureDetector
    return AcademicStructureDetector

@pytest.fixture
def HChunker():
    from app.services.smart_chunking_service import HierarchicalChunker
    return HierarchicalChunker

@pytest.fixture
def get_preset():
    from app.services.smart_chunking_service import get_preset_config
    return get_preset_config

@pytest.fixture
def short_text():
    return "人工智能是计算机科学的一个分支。它致力于开发能够模拟人类智能的系统。"

@pytest.fixture
def long_text():
    return "深度学习在自然语言处理领域的应用研究\n\n自然语言处理（NLP）是人工智能领域的重要研究方向。近年来，随着深度学习技术的快速发展，NLP领域取得了显著突破。\n\n传统的NLP方法主要依赖于手工特征工程和统计模型。这些方法在特定任务上取得了一定效果，但泛化能力有限。\n\n深度学习方法通过多层神经网络自动学习特征表示，极大地提升了模型的性能。从RNN到LSTM，再到Transformer，模型不断演进。\n\nTransformer架构的提出标志着NLP进入了新纪元。BERT、GPT等预训练模型取得了突破性进展。\n\n预训练语言模型通过在大规模语料上进行自监督学习，获得了强大的语言理解能力。这些模型可以通过微调适应各种下游任务。\n\n未来研究方向包括多模态融合、低资源学习、可解释性等。"

@pytest.fixture
def academic_text():
    return "# 摘要\n本研究提出了一种基于深度学习的文档智能分块方法。\n\n# 1. 引言\n文档分块是信息检索系统的关键预处理步骤 [1]。\n\n# 2. 相关工作\n## 2.1 传统分块方法\nLangChain提供了基于字符数的分块 [2]。\n\n## 2.2 语义分块\n基于句子嵌入的语义分块方法 [3]。\n\n# 3. 方法\n## 3.1 语义边界检测\n滑动窗口算法。\n\n## 3.2 层级构建\n段落/章节/文档三级结构。\n\n# 4. 实验结果\nPrecision@5提升了15% [4]。\n\n# 5. 结论\n本方法有效提升了检索准确性。\n\n# 参考文献\n[1] Vaswani A, et al. 2017.\n[2] LangChain. 2024.\n[3] Smith J. 2024.\n[4] Lewis P. 2020."


# ============== 1. 学术结构检测器 ==============

class TestAcademicDetector:
    def test_abstract_en(self, AcademicDetector):
        assert AcademicDetector.detect_section_type("# Abstract\ntext") == "abstract"
        assert AcademicDetector.detect_section_type("## ABSTRACT\ntext") == "abstract"

    def test_abstract_zh(self, AcademicDetector):
        assert AcademicDetector.detect_section_type("# 摘要\ntext") == "abstract"

    def test_introduction(self, AcademicDetector):
        assert AcademicDetector.detect_section_type("# Introduction\nt") == "introduction"
        assert AcademicDetector.detect_section_type("# 1. 引言\nt") == "introduction"

    def test_methodology(self, AcademicDetector):
        assert AcademicDetector.detect_section_type("# Methodology\nt") == "methodology"
        assert AcademicDetector.detect_section_type("# Methods\nt") == "methodology"

    def test_conclusion(self, AcademicDetector):
        assert AcademicDetector.detect_section_type("# Conclusion\nt") == "conclusion"
        assert AcademicDetector.detect_section_type("# 结论\nt") == "conclusion"

    def test_references(self, AcademicDetector):
        assert AcademicDetector.detect_section_type("# References\nt") == "references"
        assert AcademicDetector.detect_section_type("# 参考文献\nt") == "references"

    def test_non_academic(self, AcademicDetector):
        assert AcademicDetector.detect_section_type("# News\nt") is None
        assert AcademicDetector.detect_section_type("hello") is None

    def test_citations_numeric(self, AcademicDetector):
        assert AcademicDetector.has_citations("Finding [1].") is True
        assert AcademicDetector.has_citations("[1, 2, 3]") is True

    def test_citations_author(self, AcademicDetector):
        assert AcademicDetector.has_citations("Smith (2020) found") is True

    def test_no_citations(self, AcademicDetector):
        assert AcademicDetector.has_citations("No refs here.") is False

    def test_extract_citations(self, AcademicDetector):
        c = AcademicDetector.extract_citations("See [1] and [2, 3].")
        assert len(c) > 0

    def test_extract_title(self, AcademicDetector):
        assert AcademicDetector.extract_section_title("# Title\nc") == "Title"
        assert AcademicDetector.extract_section_title("## Sub\nc") == "Sub"


# ============== 2. 层级分块器 ==============

class TestHierarchicalChunker:
    def test_section_boundaries(self, HChunker, ChunkConfig):
        c = HChunker(ChunkConfig())
        text = "# S1\nC1\n# S2\nC2\n# S3\nC3"
        b = c._detect_section_boundaries(text)
        assert len(b) == 3
        for t, s, e, st in b:
            assert e > s

    def test_no_sections(self, HChunker, ChunkConfig):
        c = HChunker(ChunkConfig())
        b = c._detect_section_boundaries("Plain text only.")
        assert len(b) == 0

    def test_merge_sections(self, HChunker, ChunkConfig):
        c = HChunker(ChunkConfig())
        chunks = [("C%d" % i, i*10, (i+1)*10) for i in range(6)]
        sections = c._merge_to_sections(chunks, 3)
        assert len(sections) == 2
        for s in sections:
            assert s.metadata.level.value == "section"

    def test_document_chunk(self, HChunker, ChunkConfig):
        c = HChunker(ChunkConfig())
        d = c._create_document_chunk("A" * 3000)
        assert d.metadata.level.value == "document"
        assert len(d.content) <= 1500

    def test_parent_child_link(self, HChunker, ChunkConfig, ChunkLevel):
        from app.services.smart_chunking_service import SmartChunk, ChunkMetadata
        c = HChunker(ChunkConfig())
        parent = SmartChunk(id="p", content="P", start_char=0, end_char=100, metadata=ChunkMetadata(level=ChunkLevel.SECTION))
        child = SmartChunk(id="c", content="C", start_char=10, end_char=50, metadata=ChunkMetadata(level=ChunkLevel.PARAGRAPH))
        outside = SmartChunk(id="o", content="O", start_char=200, end_char=300, metadata=ChunkMetadata(level=ChunkLevel.PARAGRAPH))
        c._link_parent_child([parent], [child, outside])
        assert "c" in parent.metadata.child_ids
        assert "o" not in parent.metadata.child_ids
        assert child.metadata.parent_id == "p"

    def test_chunk_id_deterministic(self, HChunker):
        id1 = HChunker._generate_chunk_id("Hello", 0)
        id2 = HChunker._generate_chunk_id("Hello", 0)
        assert id1 == id2
        assert len(id1) == 12


# ============== 3. 服务基础功能 ==============

class TestServiceBasics:
    def test_init(self, service):
        assert service is not None

    def test_preprocess(self, service):
        r = service._preprocess_text("  A\r\nB\r\n\r\n\r\n\r\nC  ")
        assert "\r" not in r
        assert "\n\n\n" not in r

    def test_sentences_zh(self, service):
        s = service._split_to_sentences("第一句。第二句！第三句？")
        assert len(s) >= 2

    def test_sentences_en(self, service):
        s = service._split_to_sentences("First. Second! Third?")
        assert len(s) >= 2

    def test_detect_academic_true(self, service, academic_text):
        assert service._detect_academic_document(academic_text) is True

    def test_detect_academic_false(self, service, long_text):
        assert service._detect_academic_document(long_text) is False

    def test_empty_result(self, service):
        r = service._empty_result()
        assert r["chunks"] == []

    def test_stats(self, service, ChunkLevel):
        from app.services.smart_chunking_service import SmartChunk, ChunkMetadata
        chunks = [
            SmartChunk(id="1", content="A"*100, start_char=0, end_char=100, metadata=ChunkMetadata(level=ChunkLevel.PARAGRAPH)),
            SmartChunk(id="2", content="B"*200, start_char=100, end_char=300, metadata=ChunkMetadata(level=ChunkLevel.PARAGRAPH, has_citations=True)),
        ]
        s = service._calculate_stats(chunks, "X"*300)
        assert s["total_chunks"] == 2
        assert s["avg_chunk_size"] == 150
        assert s["chunks_with_citations"] == 1


# ============== 4. 固定分块 ==============

class TestFixedChunking:
    def test_basic(self, service, ChunkConfig, ChunkingStrategy, long_text):
        r = service.chunk(long_text, ChunkConfig(strategy=ChunkingStrategy.FIXED, base_chunk_size=200))
        assert len(r.chunks) > 1
        assert r.strategy == "fixed"

    def test_short(self, service, ChunkConfig, ChunkingStrategy, short_text):
        r = service.chunk(short_text, ChunkConfig(strategy=ChunkingStrategy.FIXED))
        assert len(r.chunks) == 1

    def test_empty(self, service, ChunkConfig, ChunkingStrategy):
        r = service.chunk("", ChunkConfig(strategy=ChunkingStrategy.FIXED))
        assert len(r.chunks) == 0

    def test_stats_valid(self, service, ChunkConfig, ChunkingStrategy, long_text):
        r = service.chunk(long_text, ChunkConfig(strategy=ChunkingStrategy.FIXED, base_chunk_size=200))
        assert r.stats["total_chunks"] == len(r.chunks)
        assert r.stats["min_chunk_size"] <= r.stats["max_chunk_size"]


# ============== 5. 文档分析 ==============

class TestDocumentAnalysis:
    def test_academic(self, service, academic_text):
        a = service.analyze_document(academic_text)
        assert a["is_academic"] is True
        assert a["recommended_strategy"] == "academic"
        assert len(a["detected_sections"]) > 0

    def test_non_academic(self, service, long_text):
        a = service.analyze_document(long_text)
        assert a["is_academic"] is False

    def test_empty(self, service):
        a = service.analyze_document("")
        assert a["is_academic"] is False

    def test_language(self, service, academic_text):
        a = service.analyze_document(academic_text)
        assert a["language"] == "zh"


# ============== 6. 预设 ==============

class TestPresets:
    def test_all_exist(self, get_preset):
        for n in ["default", "fast", "precise", "academic", "deep"]:
            assert get_preset(n) is not None

    def test_invalid(self, get_preset):
        c = get_preset("nonexistent")
        d = get_preset("default")
        assert c.strategy == d.strategy

    def test_fast(self, get_preset, ChunkingStrategy):
        c = get_preset("fast")
        assert c.strategy == ChunkingStrategy.FIXED

    def test_academic(self, get_preset, ChunkingStrategy):
        c = get_preset("academic")
        assert c.strategy == ChunkingStrategy.ACADEMIC
        assert c.detect_academic_structure is True

    def test_deep(self, get_preset, ChunkLevel):
        c = get_preset("deep")
        assert ChunkLevel.DOCUMENT in c.hierarchy_levels


# ============== 7. Schema 验证 ==============

class TestSchemas:
    def test_config_defaults(self):
        from app.schemas.chunking import ChunkingConfigCreate
        c = ChunkingConfigCreate()
        assert c.strategy.value == "hybrid"
        assert c.base_chunk_size == 500

    def test_config_validation(self):
        from app.schemas.chunking import ChunkingConfigCreate
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ChunkingConfigCreate(base_chunk_size=50)
        with pytest.raises(ValidationError):
            ChunkingConfigCreate(semantic_threshold=1.5)

    def test_request_validation(self):
        from app.schemas.chunking import DocumentChunkRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            DocumentChunkRequest(text="")
        r = DocumentChunkRequest(text="Hello")
        assert r.file_type == "txt"

    def test_presets_data(self):
        from app.schemas.chunking import PRESET_DESCRIPTIONS
        assert len(PRESET_DESCRIPTIONS) == 5

    def test_enums(self):
        from app.schemas.chunking import ChunkingStrategyEnum, ChunkLevelEnum, ChunkingPresetEnum
        assert len(ChunkingStrategyEnum) == 5
        assert len(ChunkLevelEnum) == 3
        assert len(ChunkingPresetEnum) == 5


# ============== 8. 边界条件 ==============

class TestEdgeCases:
    def test_single_char(self, service, ChunkConfig, ChunkingStrategy):
        r = service.chunk("A", ChunkConfig(strategy=ChunkingStrategy.FIXED))
        assert len(r.chunks) == 1

    def test_only_newlines(self, service, ChunkConfig, ChunkingStrategy):
        r = service.chunk("\n\n\n", ChunkConfig(strategy=ChunkingStrategy.FIXED))
        assert len(r.chunks) == 0

    def test_unicode(self, service, ChunkConfig, ChunkingStrategy):
        r = service.chunk("中文。English. E=mc²", ChunkConfig(strategy=ChunkingStrategy.FIXED))
        assert len(r.chunks) > 0

    def test_very_long(self, service, ChunkConfig, ChunkingStrategy):
        r = service.chunk("A" * 10000, ChunkConfig(strategy=ChunkingStrategy.FIXED, base_chunk_size=500))
        assert len(r.chunks) >= 10


# ============== 9. 性能 ==============

class TestPerformance:
    def test_fixed_speed(self, service, ChunkConfig, ChunkingStrategy, long_text):
        c = ChunkConfig(strategy=ChunkingStrategy.FIXED)
        start = time.time()
        for _ in range(50):
            service.chunk(long_text, c)
        assert time.time() - start < 5.0

    def test_large_doc(self, service, ChunkConfig, ChunkingStrategy):
        text = "测试文本。" * 2000
        c = ChunkConfig(strategy=ChunkingStrategy.FIXED, base_chunk_size=500)
        start = time.time()
        r = service.chunk(text, c)
        assert time.time() - start < 5.0
        assert len(r.chunks) > 10


# ============== 10. 模型字段 ==============

class TestModels:
    def test_chunk_fields(self):
        from app.models.knowledge import DocumentChunk
        for f in ['chunk_level', 'section_type', 'section_title', 'parent_chunk_id', 'has_citations', 'semantic_score']:
            assert hasattr(DocumentChunk, f)

    def test_kb_property(self):
        from app.models.knowledge import KnowledgeBase
        assert hasattr(KnowledgeBase, 'chunking_config')

    def test_chunk_level_enum(self):
        from app.models.knowledge import ChunkLevel
        assert ChunkLevel.PARAGRAPH.value == "paragraph"


# ============== 11. 便捷函数 ==============

class TestConvenience:
    def test_get_preset(self):
        from app.services.smart_chunking_service import get_preset_config
        assert get_preset_config("academic").detect_academic_structure is True

    def test_global_instance(self):
        from app.services.smart_chunking_service import smart_chunking_service
        assert smart_chunking_service is not None

    def test_section_type_property(self, ChunkLevel):
        from app.services.smart_chunking_service import SmartChunk, ChunkMetadata
        c = SmartChunk(id="t", content="H", start_char=0, end_char=1, metadata=ChunkMetadata(level=ChunkLevel.PARAGRAPH, section_type="intro"))
        assert c.section_type == "intro"
        c2 = SmartChunk(id="t2", content="H", start_char=0, end_char=1, metadata=ChunkMetadata(level=ChunkLevel.PARAGRAPH))
        assert c2.section_type is None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
