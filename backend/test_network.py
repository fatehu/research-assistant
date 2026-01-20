#!/usr/bin/env python3
"""
网络诊断脚本 - 测试 Docker 容器内的 API 连接

使用方法:
    # 在 Docker 容器内运行
    docker-compose exec backend python test_network.py
    
    # 或者本地运行
    python test_network.py
"""

import time
import sys

def test_connection(name: str, url: str, timeout: int = 30):
    """测试单个连接"""
    print(f"\n{'='*50}")
    print(f"测试: {name}")
    print(f"URL: {url}")
    print('='*50)
    
    try:
        import httpx
        
        start = time.time()
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url)
            elapsed = time.time() - start
            
            print(f"✓ 连接成功")
            print(f"  状态码: {response.status_code}")
            print(f"  响应时间: {elapsed:.2f}s")
            print(f"  响应大小: {len(response.content)} bytes")
            return True
            
    except httpx.TimeoutException:
        print(f"✗ 连接超时 ({timeout}s)")
        return False
    except Exception as e:
        print(f"✗ 连接失败: {e}")
        return False


def main():
    print("\n" + "="*60)
    print("Docker 容器网络诊断")
    print("="*60)
    
    results = []
    
    # 测试基本网络
    results.append(("Google", test_connection(
        "Google (基本网络测试)",
        "https://www.google.com",
        timeout=10
    )))
    
    # 测试 Semantic Scholar API
    results.append(("Semantic Scholar", test_connection(
        "Semantic Scholar API",
        "https://api.semanticscholar.org/graph/v1/paper/search?query=test&limit=1&fields=title",
        timeout=30
    )))
    
    # 测试 arXiv API
    results.append(("arXiv", test_connection(
        "arXiv API",
        "http://export.arxiv.org/api/query?search_query=all:test&max_results=1",
        timeout=30
    )))
    
    # 汇总
    print("\n" + "="*60)
    print("诊断结果汇总")
    print("="*60)
    
    all_passed = True
    for name, passed in results:
        status = "✓ 正常" if passed else "✗ 失败"
        print(f"  {name}: {status}")
        if not passed:
            all_passed = False
    
    print()
    if all_passed:
        print("✓ 所有连接正常")
    else:
        print("⚠ 部分连接失败")
        print("\n可能的解决方案:")
        print("  1. 检查 Docker 网络配置")
        print("  2. 检查防火墙设置")
        print("  3. 如果在中国大陆，可能需要配置代理")
        print("  4. 检查 DNS 解析")
    
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
