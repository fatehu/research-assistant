#!/usr/bin/env python3
"""
CodeLab API 测试脚本

使用方法:
    # 确保后端服务正在运行
    python test_codelab_full.py

    # 指定 API 地址
    python test_codelab_full.py --api-url http://localhost:8000
"""

import argparse
import json
import sys
import time
from typing import Optional
import requests
from datetime import datetime

# 配置
DEFAULT_API_URL = "http://localhost:8000"
TEST_EMAIL = "test@example.com"
TEST_PASSWORD = "test123456"
TEST_USERNAME = "testuser"


class Colors:
    """终端颜色"""
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'
    BOLD = '\033[1m'


def log_info(msg: str):
    print(f"{Colors.BLUE}ℹ️  {msg}{Colors.RESET}")


def log_success(msg: str):
    print(f"{Colors.GREEN}✅ {msg}{Colors.RESET}")


def log_error(msg: str):
    print(f"{Colors.RED}❌ {msg}{Colors.RESET}")


def log_warning(msg: str):
    print(f"{Colors.YELLOW}⚠️  {msg}{Colors.RESET}")


def log_test(name: str):
    print(f"\n{Colors.CYAN}{Colors.BOLD}🧪 测试: {name}{Colors.RESET}")


class CodeLabTester:
    def __init__(self, api_url: str):
        self.api_url = api_url.rstrip('/')
        self.token: Optional[str] = None
        self.session = requests.Session()
        self.notebook_id: Optional[str] = None
        self.results = {"passed": 0, "failed": 0, "tests": []}

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        """发送 HTTP 请求"""
        url = f"{self.api_url}{endpoint}"
        headers = kwargs.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return self.session.request(method, url, headers=headers, **kwargs)

    def _record_result(self, name: str, passed: bool, detail: str = ""):
        """记录测试结果"""
        self.results["tests"].append({
            "name": name,
            "passed": passed,
            "detail": detail,
            "time": datetime.now().isoformat()
        })
        if passed:
            self.results["passed"] += 1
            log_success(f"{name}")
        else:
            self.results["failed"] += 1
            log_error(f"{name}: {detail}")

    # ============== 认证测试 ==============

    def test_health(self) -> bool:
        """测试健康检查"""
        log_test("健康检查")
        try:
            resp = self._request("GET", "/health")
            if resp.status_code == 200:
                self._record_result("健康检查", True)
                return True
            else:
                self._record_result("健康检查", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("健康检查", False, str(e))
            return False

    def test_register_or_login(self) -> bool:
        """注册或登录测试用户"""
        log_test("用户认证")
        
        # 先尝试登录
        try:
            resp = self._request("POST", "/api/auth/login", json={
                "email": TEST_EMAIL,
                "password": TEST_PASSWORD
            })
            if resp.status_code == 200:
                data = resp.json()
                self.token = data["access_token"]
                self._record_result("用户登录", True)
                return True
        except:
            pass

        # 登录失败则注册
        try:
            resp = self._request("POST", "/api/auth/register", json={
                "email": TEST_EMAIL,
                "username": TEST_USERNAME,
                "password": TEST_PASSWORD
            })
            if resp.status_code in [200, 201]:
                data = resp.json()
                self.token = data["access_token"]
                self._record_result("用户注册", True)
                return True
            else:
                self._record_result("用户注册", False, f"状态码: {resp.status_code}, 响应: {resp.text[:200]}")
                return False
        except Exception as e:
            self._record_result("用户认证", False, str(e))
            return False

    # ============== Notebook 测试 ==============

    def test_list_notebooks(self) -> bool:
        """测试获取 Notebook 列表"""
        log_test("获取 Notebook 列表")
        try:
            resp = self._request("GET", "/api/codelab/notebooks")
            if resp.status_code == 200:
                data = resp.json()
                self._record_result("获取 Notebook 列表", True, f"共 {len(data)} 个")
                return True
            else:
                self._record_result("获取 Notebook 列表", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("获取 Notebook 列表", False, str(e))
            return False

    def test_create_notebook(self) -> bool:
        """测试创建 Notebook"""
        log_test("创建 Notebook")
        try:
            resp = self._request("POST", "/api/codelab/notebooks", json={
                "title": f"测试 Notebook - {datetime.now().strftime('%H:%M:%S')}",
                "description": "自动化测试创建"
            })
            if resp.status_code in [200, 201]:
                data = resp.json()
                self.notebook_id = data["id"]
                self._record_result("创建 Notebook", True, f"ID: {self.notebook_id}")
                return True
            else:
                self._record_result("创建 Notebook", False, f"状态码: {resp.status_code}, 响应: {resp.text[:200]}")
                return False
        except Exception as e:
            self._record_result("创建 Notebook", False, str(e))
            return False

    def test_get_notebook(self) -> bool:
        """测试获取 Notebook 详情"""
        log_test("获取 Notebook 详情")
        if not self.notebook_id:
            self._record_result("获取 Notebook 详情", False, "无可用的 Notebook ID")
            return False
        try:
            resp = self._request("GET", f"/api/codelab/notebooks/{self.notebook_id}")
            if resp.status_code == 200:
                data = resp.json()
                cell_count = len(data.get("cells", []))
                self._record_result("获取 Notebook 详情", True, f"包含 {cell_count} 个单元格")
                return True
            else:
                self._record_result("获取 Notebook 详情", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("获取 Notebook 详情", False, str(e))
            return False

    # ============== 代码执行测试 ==============

    def test_execute_simple_code(self) -> bool:
        """测试简单代码执行"""
        log_test("简单代码执行")
        if not self.notebook_id:
            self._record_result("简单代码执行", False, "无可用的 Notebook ID")
            return False
        try:
            code = 'print("Hello, CodeLab!")\n1 + 1'
            resp = self._request("POST", f"/api/codelab/notebooks/{self.notebook_id}/execute", json={
                "code": code,
                "timeout": 10
            })
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    self._record_result("简单代码执行", True, f"执行时间: {data.get('execution_time_ms')}ms")
                    return True
                else:
                    outputs = data.get("outputs", [])
                    error_msg = ""
                    for o in outputs:
                        if o.get("output_type") == "error":
                            error_msg = str(o.get("content", {}))
                    self._record_result("简单代码执行", False, f"执行失败: {error_msg[:100]}")
                    return False
            else:
                self._record_result("简单代码执行", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("简单代码执行", False, str(e))
            return False

    def test_execute_numpy(self) -> bool:
        """测试 NumPy 代码执行"""
        log_test("NumPy 代码执行")
        if not self.notebook_id:
            self._record_result("NumPy 代码执行", False, "无可用的 Notebook ID")
            return False
        try:
            code = '''
import numpy as np
arr = np.array([1, 2, 3, 4, 5])
print(f"数组: {arr}")
print(f"均值: {np.mean(arr)}")
print(f"标准差: {np.std(arr):.4f}")
arr.sum()
'''
            resp = self._request("POST", f"/api/codelab/notebooks/{self.notebook_id}/execute", json={
                "code": code,
                "timeout": 15
            })
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    self._record_result("NumPy 代码执行", True, f"执行时间: {data.get('execution_time_ms')}ms")
                    return True
                else:
                    self._record_result("NumPy 代码执行", False, "执行失败")
                    return False
            else:
                self._record_result("NumPy 代码执行", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("NumPy 代码执行", False, str(e))
            return False

    def test_execute_pandas(self) -> bool:
        """测试 Pandas 代码执行"""
        log_test("Pandas 代码执行")
        if not self.notebook_id:
            self._record_result("Pandas 代码执行", False, "无可用的 Notebook ID")
            return False
        try:
            code = '''
import pandas as pd
import numpy as np

df = pd.DataFrame({
    'Name': ['Alice', 'Bob', 'Charlie'],
    'Age': [25, 30, 35],
    'Score': [85.5, 90.0, 78.5]
})
print(df.to_string())
print(f"\\n平均年龄: {df['Age'].mean()}")
df.describe()
'''
            resp = self._request("POST", f"/api/codelab/notebooks/{self.notebook_id}/execute", json={
                "code": code,
                "timeout": 15
            })
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    self._record_result("Pandas 代码执行", True, f"执行时间: {data.get('execution_time_ms')}ms")
                    return True
                else:
                    self._record_result("Pandas 代码执行", False, "执行失败")
                    return False
            else:
                self._record_result("Pandas 代码执行", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("Pandas 代码执行", False, str(e))
            return False

    def test_execute_matplotlib(self) -> bool:
        """测试 Matplotlib 绘图"""
        log_test("Matplotlib 绘图")
        if not self.notebook_id:
            self._record_result("Matplotlib 绘图", False, "无可用的 Notebook ID")
            return False
        try:
            code = '''
import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0, 10, 100)
y = np.sin(x)

plt.figure(figsize=(8, 4))
plt.plot(x, y, 'b-', linewidth=2)
plt.title('Sine Wave')
plt.xlabel('x')
plt.ylabel('sin(x)')
plt.grid(True)
plt.show()
print("图表已生成")
'''
            resp = self._request("POST", f"/api/codelab/notebooks/{self.notebook_id}/execute", json={
                "code": code,
                "timeout": 20
            })
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    # 检查是否有图片输出
                    outputs = data.get("outputs", [])
                    has_image = any(o.get("output_type") == "display_data" for o in outputs)
                    if has_image:
                        self._record_result("Matplotlib 绘图", True, "成功生成图表")
                    else:
                        self._record_result("Matplotlib 绘图", True, "执行成功但未检测到图片输出")
                    return True
                else:
                    self._record_result("Matplotlib 绘图", False, "执行失败")
                    return False
            else:
                self._record_result("Matplotlib 绘图", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("Matplotlib 绘图", False, str(e))
            return False

    def test_execute_error_handling(self) -> bool:
        """测试错误处理"""
        log_test("错误处理")
        if not self.notebook_id:
            self._record_result("错误处理", False, "无可用的 Notebook ID")
            return False
        try:
            code = '''
# 这是一个会产生错误的代码
x = 1 / 0
'''
            resp = self._request("POST", f"/api/codelab/notebooks/{self.notebook_id}/execute", json={
                "code": code,
                "timeout": 10
            })
            if resp.status_code == 200:
                data = resp.json()
                # 预期执行失败
                if not data.get("success"):
                    outputs = data.get("outputs", [])
                    has_error = any(o.get("output_type") == "error" for o in outputs)
                    if has_error:
                        self._record_result("错误处理", True, "正确捕获了除零错误")
                        return True
                self._record_result("错误处理", False, "未能正确捕获错误")
                return False
            else:
                self._record_result("错误处理", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("错误处理", False, str(e))
            return False

    def test_execute_timeout(self) -> bool:
        """测试超时处理"""
        log_test("超时处理")
        if not self.notebook_id:
            self._record_result("超时处理", False, "无可用的 Notebook ID")
            return False
        try:
            code = '''
import time
time.sleep(10)  # 睡眠 10 秒
print("完成")
'''
            start = time.time()
            resp = self._request("POST", f"/api/codelab/notebooks/{self.notebook_id}/execute", json={
                "code": code,
                "timeout": 3  # 设置 3 秒超时
            })
            elapsed = time.time() - start
            
            if resp.status_code == 200:
                data = resp.json()
                # 预期超时
                if not data.get("success"):
                    outputs = data.get("outputs", [])
                    is_timeout = any(
                        o.get("output_type") == "error" and "TimeoutError" in str(o.get("content", {}))
                        for o in outputs
                    )
                    if is_timeout:
                        self._record_result("超时处理", True, f"正确处理超时，耗时 {elapsed:.1f}s")
                        return True
                self._record_result("超时处理", False, "未能正确处理超时")
                return False
            else:
                self._record_result("超时处理", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("超时处理", False, str(e))
            return False

    # ============== Notebook 操作测试 ==============

    def test_update_notebook(self) -> bool:
        """测试更新 Notebook"""
        log_test("更新 Notebook")
        if not self.notebook_id:
            self._record_result("更新 Notebook", False, "无可用的 Notebook ID")
            return False
        try:
            new_title = f"更新后的标题 - {datetime.now().strftime('%H:%M:%S')}"
            resp = self._request("PATCH", f"/api/codelab/notebooks/{self.notebook_id}", json={
                "title": new_title
            })
            if resp.status_code == 200:
                data = resp.json()
                if data.get("title") == new_title:
                    self._record_result("更新 Notebook", True)
                    return True
                else:
                    self._record_result("更新 Notebook", False, "标题未更新")
                    return False
            else:
                self._record_result("更新 Notebook", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("更新 Notebook", False, str(e))
            return False

    def test_add_cell(self) -> bool:
        """测试添加单元格"""
        log_test("添加单元格")
        if not self.notebook_id:
            self._record_result("添加单元格", False, "无可用的 Notebook ID")
            return False
        try:
            resp = self._request("POST", f"/api/codelab/notebooks/{self.notebook_id}/cells", params={
                "cell_type": "code"
            })
            if resp.status_code == 200:
                data = resp.json()
                if data.get("id"):
                    self._record_result("添加单元格", True, f"新单元格 ID: {data['id'][:8]}...")
                    return True
                else:
                    self._record_result("添加单元格", False, "响应中缺少单元格 ID")
                    return False
            else:
                self._record_result("添加单元格", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("添加单元格", False, str(e))
            return False

    def test_run_all_cells(self) -> bool:
        """测试运行所有单元格"""
        log_test("运行所有单元格")
        if not self.notebook_id:
            self._record_result("运行所有单元格", False, "无可用的 Notebook ID")
            return False
        try:
            resp = self._request("POST", f"/api/codelab/notebooks/{self.notebook_id}/run-all")
            if resp.status_code == 200:
                data = resp.json()
                self._record_result("运行所有单元格", True, data.get("message", ""))
                return True
            else:
                self._record_result("运行所有单元格", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("运行所有单元格", False, str(e))
            return False

    def test_delete_notebook(self) -> bool:
        """测试删除 Notebook"""
        log_test("删除 Notebook")
        if not self.notebook_id:
            self._record_result("删除 Notebook", False, "无可用的 Notebook ID")
            return False
        try:
            resp = self._request("DELETE", f"/api/codelab/notebooks/{self.notebook_id}")
            if resp.status_code == 200:
                self._record_result("删除 Notebook", True)
                self.notebook_id = None
                return True
            else:
                self._record_result("删除 Notebook", False, f"状态码: {resp.status_code}")
                return False
        except Exception as e:
            self._record_result("删除 Notebook", False, str(e))
            return False

    # ============== 运行所有测试 ==============

    def run_all_tests(self):
        """运行所有测试"""
        print(f"\n{'='*60}")
        print(f"{Colors.BOLD}CodeLab API 完整测试{Colors.RESET}")
        print(f"API URL: {self.api_url}")
        print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")

        # 基础测试
        if not self.test_health():
            log_error("健康检查失败，请确保后端服务正在运行")
            return

        if not self.test_register_or_login():
            log_error("认证失败，无法继续测试")
            return

        # Notebook 基础操作
        self.test_list_notebooks()
        self.test_create_notebook()
        self.test_get_notebook()

        # 代码执行测试
        self.test_execute_simple_code()
        self.test_execute_numpy()
        self.test_execute_pandas()
        self.test_execute_matplotlib()
        self.test_execute_error_handling()
        self.test_execute_timeout()

        # Notebook 操作测试
        self.test_update_notebook()
        self.test_add_cell()
        self.test_run_all_cells()
        self.test_delete_notebook()

        # 打印汇总
        print(f"\n{'='*60}")
        print(f"{Colors.BOLD}测试结果汇总{Colors.RESET}")
        print(f"{'='*60}")
        print(f"✅ 通过: {Colors.GREEN}{self.results['passed']}{Colors.RESET}")
        print(f"❌ 失败: {Colors.RED}{self.results['failed']}{Colors.RESET}")
        total = self.results['passed'] + self.results['failed']
        if total > 0:
            rate = (self.results['passed'] / total) * 100
            print(f"📊 通过率: {rate:.1f}%")
        print(f"{'='*60}\n")

        # 保存结果到文件
        with open("test_results.json", "w") as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)
        log_info("详细结果已保存到 test_results.json")


def main():
    parser = argparse.ArgumentParser(description="CodeLab API 测试脚本")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="API 基础 URL")
    args = parser.parse_args()

    tester = CodeLabTester(args.api_url)
    tester.run_all_tests()

    # 返回退出码
    sys.exit(0 if tester.results["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
