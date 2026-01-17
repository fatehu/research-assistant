#!/usr/bin/env python3
"""
测试 Serper API 连接
使用方法: python test_serper.py YOUR_API_KEY
"""
import sys
import requests

def test_serper_api(api_key: str):
    """测试 Serper API"""
    print(f"[测试] API Key 长度: {len(api_key)}")
    print(f"[测试] API Key 前6位: {api_key[:6]}...")
    
    try:
        response = requests.post(
            "https://google.serper.dev/search",
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json"
            },
            json={
                "q": "人工智能",
                "num": 3,
                "gl": "cn",
                "hl": "zh-cn"
            },
            timeout=15
        )
        
        print(f"[测试] HTTP 状态码: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"[测试] ✅ 成功! 返回数据键: {list(data.keys())}")
            
            if "organic" in data:
                print(f"[测试] 搜索结果数量: {len(data['organic'])}")
                for i, item in enumerate(data['organic'][:3], 1):
                    print(f"  {i}. {item.get('title', 'N/A')}")
            return True
        else:
            print(f"[测试] ❌ 失败! 响应内容: {response.text[:500]}")
            return False
            
    except Exception as e:
        print(f"[测试] ❌ 异常: {type(e).__name__}: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python test_serper.py YOUR_SERPER_API_KEY")
        print("或者在容器内: docker-compose exec backend python test_serper.py $SERPER_API_KEY")
        sys.exit(1)
    
    api_key = sys.argv[1]
    success = test_serper_api(api_key)
    sys.exit(0 if success else 1)
