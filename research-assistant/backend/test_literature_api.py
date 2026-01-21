#!/usr/bin/env python3
"""
文献管理模块 API 自动化测试脚本

使用方法:
    python test_literature_api.py [--base-url http://localhost:8000]
    
前置条件:
    1. 后端服务已启动
    2. 数据库迁移已完成
"""

import requests
import json
import sys
import time
from typing import Optional

# 配置
BASE_URL = "http://localhost:8000"
TEST_USER = {
    "email": "literature_test@example.com",
    "username": "lit_tester",
    "password": "test123456"
}

# 颜色输出
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    END = '\033[0m'

def print_success(msg):
    print(f"{Colors.GREEN}✓ {msg}{Colors.END}")

def print_error(msg):
    print(f"{Colors.RED}✗ {msg}{Colors.END}")

def print_info(msg):
    print(f"{Colors.BLUE}ℹ {msg}{Colors.END}")

def print_warning(msg):
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.END}")


class LiteratureAPITester:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip('/')
        self.token: Optional[str] = None
        self.paper_id: Optional[int] = None
        self.collection_id: Optional[int] = None
        self.test_results = []
    
    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """发送请求"""
        url = f"{self.base_url}{endpoint}"
        headers = kwargs.pop('headers', {})
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        # 设置较长的超时（外部 API 可能较慢）
        timeout = kwargs.pop('timeout', 60)
        return requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    
    def _test(self, name: str, func):
        """运行单个测试"""
        print(f"\n{'='*50}")
        print(f"测试: {name}")
        print('='*50)
        try:
            result = func()
            if result:
                print_success(f"{name} - 通过")
                self.test_results.append((name, True, None))
            else:
                print_error(f"{name} - 失败")
                self.test_results.append((name, False, "返回 False"))
        except Exception as e:
            print_error(f"{name} - 异常: {e}")
            self.test_results.append((name, False, str(e)))
    
    # ========== 认证测试 ==========
    
    def test_register_or_login(self) -> bool:
        """注册或登录测试用户"""
        # 尝试登录
        resp = self._request('POST', '/api/auth/login', json={
            "email": TEST_USER["email"],
            "password": TEST_USER["password"]
        })
        
        if resp.status_code == 200:
            self.token = resp.json()['access_token']
            print_info("使用已存在的测试用户登录")
            return True
        
        # 注册新用户
        resp = self._request('POST', '/api/auth/register', json=TEST_USER)
        if resp.status_code == 200:
            self.token = resp.json()['access_token']
            print_info("创建新测试用户")
            return True
        
        print_error(f"认证失败: {resp.text}")
        return False
    
    # ========== 初始化测试 ==========
    
    def test_init_literature(self) -> bool:
        """初始化文献模块"""
        resp = self._request('POST', '/api/literature/init')
        print_info(f"响应: {resp.json()}")
        return resp.status_code == 200
    
    # ========== 搜索测试 ==========
    
    def test_search_semantic_scholar(self) -> bool:
        """测试 Semantic Scholar 搜索"""
        print_info("正在连接 Semantic Scholar API（可能需要较长时间）...")
        try:
            resp = self._request('GET', '/api/literature/search', params={
                'query': 'transformer attention',
                'source': 'semantic_scholar',
                'limit': 3
            }, timeout=120)  # 2分钟超时
            
            if resp.status_code != 200:
                print_error(f"搜索失败: {resp.text}")
                return False
            
            data = resp.json()
            
            # 检查是否有错误
            if 'error' in data:
                print_warning(f"API 返回错误: {data['error']}")
                return False
            
            print_info(f"找到 {data.get('total', 0)} 篇论文，返回 {len(data.get('papers', []))} 篇")
            
            if data.get('papers'):
                paper = data['papers'][0]
                print_info(f"第一篇: {paper['title'][:60]}...")
                print_info(f"  引用数: {paper.get('citation_count', 0)}")
                # 保存用于后续测试
                self._search_result = paper
            
            return len(data.get('papers', [])) > 0
            
        except requests.exceptions.Timeout:
            print_error("请求超时 - Semantic Scholar API 可能无法访问")
            print_warning("提示: 检查 Docker 容器网络，或者稍后重试")
            return False
        except requests.exceptions.RequestException as e:
            print_error(f"网络错误: {e}")
            return False
    
    def test_search_arxiv(self) -> bool:
        """测试 arXiv 搜索"""
        print_info("正在连接 arXiv API...")
        try:
            resp = self._request('GET', '/api/literature/search', params={
                'query': 'large language model',
                'source': 'arxiv',
                'limit': 3
            }, timeout=120)
            
            if resp.status_code != 200:
                print_error(f"搜索失败: {resp.text}")
                return False
            
            data = resp.json()
            
            if 'error' in data:
                print_warning(f"API 返回错误: {data['error']}")
                return False
            
            print_info(f"找到 {len(data.get('papers', []))} 篇 arXiv 论文")
            
            if data.get('papers'):
                paper = data['papers'][0]
                print_info(f"第一篇: {paper['title'][:60]}...")
                print_info(f"  arXiv ID: {paper.get('arxiv_id', 'N/A')}")
                # 如果 S2 搜索失败，用 arXiv 结果
                if not hasattr(self, '_search_result'):
                    self._search_result = paper
            
            return True
            
        except requests.exceptions.Timeout:
            print_error("请求超时 - arXiv API 可能无法访问")
            return False
        except requests.exceptions.RequestException as e:
            print_error(f"网络错误: {e}")
            return False
    
    def test_search_history(self) -> bool:
        """测试搜索历史"""
        resp = self._request('GET', '/api/literature/search/history', params={'limit': 5})
        
        if resp.status_code != 200:
            return False
        
        history = resp.json()
        print_info(f"搜索历史记录数: {len(history)}")
        return True
    
    # ========== 论文管理测试 ==========
    
    def test_save_paper(self) -> bool:
        """测试保存论文"""
        if not hasattr(self, '_search_result'):
            print_warning("没有搜索结果，跳过保存测试")
            # 尝试从现有论文中获取 ID
            papers_resp = self._request('GET', '/api/literature/papers')
            if papers_resp.status_code == 200:
                papers = papers_resp.json()
                if papers:
                    self.paper_id = papers[0]['id']
                    print_info(f"使用现有论文 ID: {self.paper_id}")
            return True
        
        paper = self._search_result
        resp = self._request('POST', '/api/literature/papers', json={
            'source': paper['source'],
            'external_id': paper['external_id'],
            'title': paper['title'],
            'abstract': paper.get('abstract'),
            'authors': paper.get('authors', []),
            'year': paper.get('year'),
            'venue': paper.get('venue'),
            'citation_count': paper.get('citation_count', 0),
            'url': paper.get('url'),
            'pdf_url': paper.get('pdf_url'),
            'arxiv_id': paper.get('arxiv_id'),
            'doi': paper.get('doi'),
            'fields_of_study': paper.get('fields_of_study', [])
        })
        
        if resp.status_code == 200:
            self.paper_id = resp.json()['id']
            print_info(f"论文已保存，ID: {self.paper_id}")
            return True
        elif resp.status_code == 400 and '已存在' in resp.text:
            print_warning("论文已存在")
            # 获取现有论文
            papers_resp = self._request('GET', '/api/literature/papers')
            if papers_resp.status_code == 200:
                papers = papers_resp.json()
                if papers:
                    self.paper_id = papers[0]['id']
                    print_info(f"使用现有论文 ID: {self.paper_id}")
            return True
        
        print_error(f"保存失败: {resp.text}")
        return False
    
    def test_get_papers(self) -> bool:
        """测试获取论文列表"""
        resp = self._request('GET', '/api/literature/papers')
        
        if resp.status_code != 200:
            return False
        
        papers = resp.json()
        print_info(f"论文总数: {len(papers)}")
        return True
    
    def test_get_paper_detail(self) -> bool:
        """测试获取论文详情"""
        if not self.paper_id:
            print_warning("没有论文ID，跳过")
            return True
        
        resp = self._request('GET', f'/api/literature/papers/{self.paper_id}')
        
        if resp.status_code != 200:
            return False
        
        paper = resp.json()
        print_info(f"论文标题: {paper['title'][:50]}...")
        print_info(f"收藏夹: {paper['collection_ids']}")
        return True
    
    def test_update_paper(self) -> bool:
        """测试更新论文"""
        if not self.paper_id:
            print_warning("没有论文ID，跳过")
            return True
        
        resp = self._request('PATCH', f'/api/literature/papers/{self.paper_id}', json={
            'notes': '这是自动化测试添加的笔记 - ' + time.strftime('%Y-%m-%d %H:%M:%S'),
            'rating': 4,
            'is_read': True,
            'tags': ['test', 'automated']
        })
        
        if resp.status_code != 200:
            print_error(f"更新失败: {resp.text}")
            return False
        
        paper = resp.json()
        print_info(f"已更新 - 评分: {paper['rating']}, 已读: {paper['is_read']}")
        return True
    
    # ========== 收藏夹测试 ==========
    
    def test_get_collections(self) -> bool:
        """测试获取收藏夹"""
        resp = self._request('GET', '/api/literature/collections')
        
        if resp.status_code != 200:
            return False
        
        collections = resp.json()
        print_info(f"收藏夹数量: {len(collections)}")
        for c in collections:
            print_info(f"  - {c['name']} ({c['paper_count']} 篇)")
        return True
    
    def test_create_collection(self) -> bool:
        """测试创建收藏夹"""
        resp = self._request('POST', '/api/literature/collections', json={
            'name': f'测试收藏夹-{int(time.time())}',
            'description': '自动化测试创建',
            'color': '#8b5cf6'
        })
        
        if resp.status_code != 200:
            print_error(f"创建失败: {resp.text}")
            return False
        
        self.collection_id = resp.json()['id']
        print_info(f"收藏夹已创建，ID: {self.collection_id}")
        return True
    
    def test_add_paper_to_collection(self) -> bool:
        """测试添加论文到收藏夹"""
        if not self.paper_id or not self.collection_id:
            print_warning("缺少论文或收藏夹ID，跳过")
            return True
        
        resp = self._request('POST', '/api/literature/collections/add-paper', json={
            'paper_id': self.paper_id,
            'collection_ids': [self.collection_id]
        })
        
        if resp.status_code != 200:
            print_error(f"添加失败: {resp.text}")
            return False
        
        print_info("论文已添加到收藏夹")
        return True
    
    def test_remove_paper_from_collection(self) -> bool:
        """测试从收藏夹移除论文"""
        if not self.paper_id or not self.collection_id:
            print_warning("缺少论文或收藏夹ID，跳过")
            return True
        
        resp = self._request('POST', '/api/literature/collections/remove-paper', json={
            'paper_id': self.paper_id,
            'collection_id': self.collection_id
        })
        
        if resp.status_code != 200:
            print_error(f"移除失败: {resp.text}")
            return False
        
        print_info("论文已从收藏夹移除")
        return True
    
    # ========== 引用图谱测试 ==========
    
    def test_citation_graph(self) -> bool:
        """测试引用图谱"""
        if not self.paper_id:
            print_warning("没有论文ID，跳过")
            return True
        
        resp = self._request('GET', f'/api/literature/graph/{self.paper_id}', params={
            'max_nodes': 10
        })
        
        if resp.status_code == 400:
            print_warning("论文没有 Semantic Scholar ID，无法获取图谱")
            return True
        
        if resp.status_code != 200:
            print_error(f"获取图谱失败: {resp.text}")
            return False
        
        graph = resp.json()
        print_info(f"图谱节点: {len(graph['nodes'])}")
        print_info(f"图谱边: {len(graph['edges'])}")
        print_info(f"中心节点: {graph['center_id'][:20]}...")
        return True
    
    # ========== 清理测试 ==========
    
    def test_delete_collection(self) -> bool:
        """测试删除收藏夹"""
        if not self.collection_id:
            print_warning("没有收藏夹ID，跳过")
            return True
        
        resp = self._request('DELETE', f'/api/literature/collections/{self.collection_id}')
        
        if resp.status_code != 200:
            print_error(f"删除失败: {resp.text}")
            return False
        
        print_info("收藏夹已删除")
        return True
    
    # ========== 运行所有测试 ==========
    
    def run_all_tests(self):
        """运行所有测试"""
        print("\n" + "="*60)
        print("文献管理模块 API 测试")
        print("="*60)
        print(f"目标服务器: {self.base_url}")
        
        # 认证
        self._test("用户认证", self.test_register_or_login)
        if not self.token:
            print_error("认证失败，终止测试")
            return
        
        # 初始化
        self._test("初始化文献模块", self.test_init_literature)
        
        # 搜索
        self._test("Semantic Scholar 搜索", self.test_search_semantic_scholar)
        self._test("arXiv 搜索", self.test_search_arxiv)
        self._test("搜索历史", self.test_search_history)
        
        # 论文管理
        self._test("保存论文", self.test_save_paper)
        self._test("获取论文列表", self.test_get_papers)
        self._test("获取论文详情", self.test_get_paper_detail)
        self._test("更新论文", self.test_update_paper)
        
        # 收藏夹
        self._test("获取收藏夹", self.test_get_collections)
        self._test("创建收藏夹", self.test_create_collection)
        self._test("添加论文到收藏夹", self.test_add_paper_to_collection)
        self._test("从收藏夹移除论文", self.test_remove_paper_from_collection)
        
        # 引用图谱
        self._test("引用图谱", self.test_citation_graph)
        
        # 清理
        self._test("删除收藏夹", self.test_delete_collection)
        
        # 汇总
        self._print_summary()
    
    def _print_summary(self):
        """打印测试汇总"""
        print("\n" + "="*60)
        print("测试汇总")
        print("="*60)
        
        passed = sum(1 for _, result, _ in self.test_results if result)
        failed = len(self.test_results) - passed
        
        for name, result, error in self.test_results:
            status = f"{Colors.GREEN}通过{Colors.END}" if result else f"{Colors.RED}失败{Colors.END}"
            print(f"  {status} - {name}")
            if error:
                print(f"       {Colors.YELLOW}错误: {error}{Colors.END}")
        
        print()
        print(f"总计: {len(self.test_results)} 个测试")
        print(f"  {Colors.GREEN}通过: {passed}{Colors.END}")
        print(f"  {Colors.RED}失败: {failed}{Colors.END}")
        
        if failed == 0:
            print(f"\n{Colors.GREEN}🎉 所有测试通过！{Colors.END}")
        else:
            print(f"\n{Colors.RED}⚠ 有 {failed} 个测试失败{Colors.END}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description='文献管理模块 API 测试')
    parser.add_argument('--base-url', default=BASE_URL, help='API 基础 URL')
    args = parser.parse_args()
    
    tester = LiteratureAPITester(args.base_url)
    tester.run_all_tests()


if __name__ == '__main__':
    main()
