"""
智能分块服务使用示例

展示如何使用智能分块服务的各种功能
"""
import asyncio
from typing import Dict, Any


# 示例文档 - 学术论文风格
SAMPLE_ACADEMIC_TEXT = """
# 摘要

本研究探讨了深度学习在自然语言处理中的应用。通过对Transformer架构的改进，
我们提出了一种新的注意力机制，在多个基准测试中取得了显著的性能提升。

# 1. 引言

自然语言处理（NLP）是人工智能领域的重要研究方向。近年来，深度学习技术
的发展极大地推动了NLP的进步。特别是Transformer架构的提出[1]，彻底改变了
序列建模的范式。

然而，现有的注意力机制仍然存在一些局限性。首先，计算复杂度与序列长度
呈二次关系[2]；其次，对于长距离依赖的建模能力有限。

# 2. 相关工作

Vaswani等人[1]首次提出了Transformer架构，使用自注意力机制替代了循环神经网络。
后续研究主要集中在以下几个方向：

- 稀疏注意力机制[3]
- 线性复杂度的注意力[4]
- 相对位置编码[5]

# 3. 方法

## 3.1 问题定义

给定输入序列 X = (x_1, x_2, ..., x_n)，我们的目标是学习一个映射函数 f，
使得输出序列 Y = f(X) 能够准确捕捉序列中的语义信息。

## 3.2 提出的注意力机制

我们提出的改进注意力机制包含以下创新点：
1. 动态稀疏模式学习
2. 层次化注意力聚合
3. 位置感知的缩放因子

# 4. 实验

## 4.1 数据集

我们在以下数据集上进行了实验：
- GLUE基准测试
- SQuAD问答数据集
- WMT翻译数据集

## 4.2 实验结果

实验结果表明，我们的方法在所有测试数据集上都取得了最优性能。
具体来说，在GLUE测试集上，平均得分提升了2.3个百分点。

# 5. 结论

本文提出了一种改进的注意力机制，通过动态稀疏模式和层次化聚合，
有效地提升了Transformer模型的性能。未来工作将探索该方法在
更多应用场景中的潜力。

# 参考文献

[1] Vaswani, A., et al. "Attention is all you need." NIPS 2017.
[2] Kitaev, N., et al. "Reformer: The efficient transformer." ICLR 2020.
[3] Child, R., et al. "Generating long sequences with sparse transformers." 2019.
[4] Katharopoulos, A., et al. "Transformers are RNNs." ICML 2020.
[5] Shaw, P., et al. "Self-attention with relative position representations." 2018.
"""

# 示例文档 - 普通文本
SAMPLE_GENERAL_TEXT = """
人工智能的发展历程可以追溯到20世纪50年代。当时，科学家们开始思考机器是否
能够像人类一样思考和学习。艾伦·图灵提出了著名的"图灵测试"，成为衡量机器
智能的重要标准。

在接下来的几十年里，人工智能经历了多次起伏。60年代和70年代是早期的繁荣期，
研究者们对AI的未来充满乐观。然而，由于计算能力的限制和算法的不成熟，
AI很快进入了被称为"AI寒冬"的低谷期。

进入21世纪，随着计算能力的飞速提升和大数据的出现，深度学习技术取得了
突破性进展。2012年，AlexNet在ImageNet竞赛中的惊人表现标志着深度学习
时代的到来。

如今，人工智能已经渗透到我们生活的方方面面。从智能手机上的语音助手，
到自动驾驶汽车，再到医疗诊断系统，AI正在改变着世界。未来，随着技术的
不断进步，人工智能将会发挥更加重要的作用。
"""


async def demo_basic_chunking():
    """演示基本分块功能"""
    print("=" * 60)
    print("演示 1: 基本分块")
    print("=" * 60)
    
    from app.services.smart_chunking_service import (
        SmartChunkingService,
        ChunkConfig,
        ChunkingStrategy
    )
    
    service = SmartChunkingService()
    
    # 使用默认配置（混合策略）
    result = await service.chunk_document(SAMPLE_GENERAL_TEXT)
    
    print(f"使用策略: {result['strategy']}")
    print(f"分块数量: {len(result['chunks'])}")
    print(f"统计信息: {result['stats']}")
    print("\n前两个分块:")
    for i, chunk in enumerate(result['chunks'][:2]):
        print(f"\n--- 分块 {i+1} ---")
        print(f"内容: {chunk.content[:100]}...")
        print(f"长度: {len(chunk.content)} 字符")


async def demo_semantic_chunking():
    """演示语义分块"""
    print("\n" + "=" * 60)
    print("演示 2: 语义分块")
    print("=" * 60)
    
    from app.services.smart_chunking_service import (
        SmartChunkingService,
        ChunkConfig,
        ChunkingStrategy
    )
    
    service = SmartChunkingService()
    
    config = ChunkConfig(
        strategy=ChunkingStrategy.SEMANTIC,
        semantic_threshold=0.7,  # 较低的阈值，产生更多分块
        min_semantic_chunk=100,
        max_semantic_chunk=800
    )
    
    result = await service.chunk_document(SAMPLE_GENERAL_TEXT, config)
    
    print(f"语义分块数量: {len(result['chunks'])}")
    print(f"平均块大小: {result['stats'].get('avg_chunk_size', 0)} 字符")
    

async def demo_academic_chunking():
    """演示学术论文分块"""
    print("\n" + "=" * 60)
    print("演示 3: 学术论文分块")
    print("=" * 60)
    
    from app.services.smart_chunking_service import (
        SmartChunkingService,
        ChunkConfig,
        ChunkingStrategy
    )
    
    service = SmartChunkingService()
    
    config = ChunkConfig(
        strategy=ChunkingStrategy.ACADEMIC,
        detect_academic_structure=True,
        preserve_citations=True
    )
    
    result = await service.chunk_document(SAMPLE_ACADEMIC_TEXT, config)
    
    print(f"分块数量: {len(result['chunks'])}")
    print(f"检测到学术结构: {result['metadata'].get('is_academic', False)}")
    print(f"识别的章节: {result['metadata'].get('detected_sections', [])}")
    
    print("\n按章节类型统计:")
    section_counts = {}
    for chunk in result['chunks']:
        section_type = chunk.metadata.section_type or "未分类"
        section_counts[section_type] = section_counts.get(section_type, 0) + 1
    
    for section, count in section_counts.items():
        print(f"  - {section}: {count} 个分块")


async def demo_hierarchical_chunking():
    """演示层级分块"""
    print("\n" + "=" * 60)
    print("演示 4: 层级分块")
    print("=" * 60)
    
    from app.services.smart_chunking_service import (
        SmartChunkingService,
        ChunkConfig,
        ChunkingStrategy,
        ChunkLevel
    )
    
    service = SmartChunkingService()
    
    config = ChunkConfig(
        strategy=ChunkingStrategy.HIERARCHICAL,
        enable_hierarchical=True,
        hierarchy_levels=[
            ChunkLevel.PARAGRAPH,
            ChunkLevel.SECTION,
            ChunkLevel.DOCUMENT
        ]
    )
    
    result = await service.chunk_document(SAMPLE_ACADEMIC_TEXT, config)
    
    print(f"主分块数量: {len(result['chunks'])}")
    
    if result.get('hierarchy'):
        print("\n层级结构:")
        for level, chunks in result['hierarchy'].items():
            print(f"  - {level}: {len(chunks)} 个分块")


async def demo_preset_configs():
    """演示预设配置"""
    print("\n" + "=" * 60)
    print("演示 5: 预设配置")
    print("=" * 60)
    
    from app.services.smart_chunking_service import get_preset_config
    
    presets = ['default', 'fast', 'precise', 'academic', 'deep']
    
    for preset in presets:
        config = get_preset_config(preset)
        print(f"\n{preset}:")
        print(f"  策略: {config.strategy.value}")
        print(f"  基础块大小: {config.base_chunk_size}")
        print(f"  启用层级: {config.enable_hierarchical}")
        print(f"  语义阈值: {config.semantic_threshold}")


async def demo_document_analysis():
    """演示文档分析"""
    print("\n" + "=" * 60)
    print("演示 6: 文档分析")
    print("=" * 60)
    
    from app.services.smart_chunking_service import (
        SmartChunkingService,
        AcademicStructureDetector
    )
    
    service = SmartChunkingService()
    
    # 分析学术文档
    print("\n学术文档分析:")
    is_academic = service._detect_academic_document(SAMPLE_ACADEMIC_TEXT)
    has_citations = AcademicStructureDetector.has_citations(SAMPLE_ACADEMIC_TEXT)
    print(f"  是否学术文档: {is_academic}")
    print(f"  是否包含引用: {has_citations}")
    
    # 分析普通文档
    print("\n普通文档分析:")
    is_academic = service._detect_academic_document(SAMPLE_GENERAL_TEXT)
    has_citations = AcademicStructureDetector.has_citations(SAMPLE_GENERAL_TEXT)
    print(f"  是否学术文档: {is_academic}")
    print(f"  是否包含引用: {has_citations}")


async def demo_comparison():
    """演示策略比较"""
    print("\n" + "=" * 60)
    print("演示 7: 策略比较")
    print("=" * 60)
    
    from app.services.smart_chunking_service import (
        SmartChunkingService,
        ChunkConfig,
        ChunkingStrategy
    )
    
    service = SmartChunkingService()
    text = SAMPLE_GENERAL_TEXT
    
    strategies = [
        ("固定分块", ChunkConfig(strategy=ChunkingStrategy.FIXED)),
        ("语义分块", ChunkConfig(strategy=ChunkingStrategy.SEMANTIC)),
        ("混合策略", ChunkConfig(strategy=ChunkingStrategy.HYBRID)),
    ]
    
    print(f"\n对 {len(text)} 字符的文档进行分块比较:\n")
    print(f"{'策略':<12} {'分块数':<8} {'平均大小':<10} {'最小':<8} {'最大':<8}")
    print("-" * 50)
    
    for name, config in strategies:
        result = await service.chunk_document(text, config)
        stats = result['stats']
        
        print(f"{name:<12} {stats.get('total_chunks', 0):<8} "
              f"{stats.get('avg_chunk_size', 0):<10} "
              f"{stats.get('min_chunk_size', 0):<8} "
              f"{stats.get('max_chunk_size', 0):<8}")


async def main():
    """主函数"""
    print("智能分块服务演示")
    print("=" * 60)
    
    # 注意：以下演示需要在正确配置的环境中运行
    # 包括：数据库连接、嵌入服务 API Key 等
    
    try:
        await demo_basic_chunking()
        await demo_semantic_chunking()
        await demo_academic_chunking()
        await demo_hierarchical_chunking()
        await demo_preset_configs()
        await demo_document_analysis()
        await demo_comparison()
        
    except ImportError as e:
        print(f"\n导入错误: {e}")
        print("请确保在项目环境中运行此脚本")
    except Exception as e:
        print(f"\n运行错误: {e}")
        print("请检查环境配置（数据库、API Key等）")
    
    print("\n" + "=" * 60)
    print("演示完成!")


if __name__ == "__main__":
    asyncio.run(main())
