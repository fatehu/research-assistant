#!/usr/bin/env python3
"""
预下载嵌入模型脚本

在首次启动服务前运行此脚本，可提前下载模型到缓存目录，
避免首次请求时的长时间等待。

用法:
  # 下载默认模型 (BAAI/bge-m3)
  python scripts/download_embedding_model.py

  # 下载指定模型
  python scripts/download_embedding_model.py --model allenai/specter2

  # 指定缓存目录
  python scripts/download_embedding_model.py --cache-dir /path/to/cache

  # 测试模型
  python scripts/download_embedding_model.py --test
"""
import argparse
import sys
import time


def main():
    parser = argparse.ArgumentParser(description="预下载嵌入模型")
    parser.add_argument(
        "--model",
        default="BAAI/bge-m3",
        help="模型名称 (默认: BAAI/bge-m3)",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="模型缓存目录",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="下载后运行简单测试",
    )
    args = parser.parse_args()

    print(f"📦 准备下载嵌入模型: {args.model}")
    print(f"   缓存目录: {args.cache_dir or '默认'}")
    print()

    try:
        from sentence_transformers import SentenceTransformer

        print("⬇️  正在下载模型 (首次下载可能需要几分钟)...")
        start = time.time()

        model = SentenceTransformer(
            args.model,
            cache_folder=args.cache_dir,
            trust_remote_code=True,
        )

        elapsed = time.time() - start
        dim = model.get_sentence_embedding_dimension()
        print(f"✅ 模型下载完成! 耗时 {elapsed:.1f}s")
        print(f"   模型: {args.model}")
        print(f"   维度: {dim}")

        if args.test:
            print()
            print("🧪 运行测试...")

            test_texts = [
                "基于深度学习的目标检测算法研究",
                "Deep learning based object detection algorithms",
                "使用卫星遥感数据监测水华变化",
                "Monitoring algal bloom changes using satellite remote sensing data",
            ]

            embeddings = model.encode(
                test_texts,
                normalize_embeddings=True,
                show_progress_bar=False,
            )

            import numpy as np

            print(f"   编码 {len(test_texts)} 条文本, 输出维度: {embeddings.shape}")
            print()

            # 打印相似度矩阵
            print("   相似度矩阵:")
            for i in range(len(test_texts)):
                for j in range(len(test_texts)):
                    sim = float(np.dot(embeddings[i], embeddings[j]))
                    print(f"   {sim:.3f}", end="")
                print(f"  ← {test_texts[i][:30]}...")
            print()
            print("✅ 测试通过!")

    except ImportError:
        print("❌ 请先安装依赖: pip install sentence-transformers torch")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 下载失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
