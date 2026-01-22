"""
代码实验室 API 测试脚本
运行方式: python test_codelab_api.py

测试前请确保:
1. 后端服务已启动: uvicorn app.main:app --reload
2. 已登录并获取 token
"""
import requests
import json
import sys

BASE_URL = "http://localhost:8000"

# 用户凭证 - 修改为你的测试账号
TEST_EMAIL = "test@example.com"
TEST_PASSWORD = "test123456"


def get_token():
    """获取认证 token"""
    try:
        resp = requests.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": TEST_EMAIL, "password": TEST_PASSWORD}
        )
        if resp.status_code == 200:
            return resp.json()["access_token"]
        else:
            print(f"❌ 登录失败: {resp.text}")
            print("请先注册测试账号或修改脚本中的凭证")
            return None
    except Exception as e:
        print(f"❌ 连接失败: {e}")
        print("请确保后端服务已启动")
        return None


class CodelabTester:
    def __init__(self, token):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        self.notebook_id = None
        self.cell_id = None
        self.passed = 0
        self.failed = 0

    def test(self, name, func):
        """运行单个测试"""
        try:
            func()
            print(f"  ✅ {name}")
            self.passed += 1
        except AssertionError as e:
            print(f"  ❌ {name}: {e}")
            self.failed += 1
        except Exception as e:
            print(f"  ❌ {name}: 异常 - {e}")
            self.failed += 1

    def test_create_notebook(self):
        """测试创建 Notebook"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/notebooks",
            headers=self.headers,
            json={"title": "API 测试 Notebook", "description": "自动化测试"}
        )
        assert resp.status_code == 200, f"状态码错误: {resp.status_code}"
        data = resp.json()
        assert "id" in data, "响应缺少 id"
        assert data["title"] == "API 测试 Notebook", "标题不匹配"
        assert len(data["cells"]) > 0, "应该有初始单元格"
        
        self.notebook_id = data["id"]
        self.cell_id = data["cells"][0]["id"]

    def test_list_notebooks(self):
        """测试获取列表"""
        resp = requests.get(
            f"{BASE_URL}/api/codelab/notebooks",
            headers=self.headers
        )
        assert resp.status_code == 200, f"状态码错误: {resp.status_code}"
        data = resp.json()
        assert isinstance(data, list), "应返回列表"
        assert any(n["id"] == self.notebook_id for n in data), "列表应包含新建的 Notebook"

    def test_get_notebook(self):
        """测试获取详情"""
        resp = requests.get(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}",
            headers=self.headers
        )
        assert resp.status_code == 200, f"状态码错误: {resp.status_code}"
        data = resp.json()
        assert data["id"] == self.notebook_id, "ID 不匹配"

    def test_update_notebook(self):
        """测试更新 Notebook"""
        resp = requests.patch(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}",
            headers=self.headers,
            json={"title": "更新后的标题"}
        )
        assert resp.status_code == 200, f"状态码错误: {resp.status_code}"
        data = resp.json()
        assert data["title"] == "更新后的标题", "标题未更新"

    def test_execute_print(self):
        """测试打印输出"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}/execute",
            headers=self.headers,
            json={
                "code": "print('Hello, CodeLab!')",
                "cell_id": self.cell_id,
                "timeout": 30
            }
        )
        assert resp.status_code == 200, f"状态码错误: {resp.status_code}"
        data = resp.json()
        assert data["success"] == True, f"执行失败: {data}"
        assert data["execution_count"] >= 1, "执行计数应 >= 1"
        
        # 检查输出
        has_output = any(
            "Hello, CodeLab!" in str(o.get("content", "")) 
            for o in data["outputs"]
        )
        assert has_output, "输出中应包含打印内容"

    def test_execute_expression(self):
        """测试表达式求值"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}/execute",
            headers=self.headers,
            json={"code": "1 + 2 + 3", "cell_id": self.cell_id}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] == True
        
        # 检查是否有执行结果
        has_result = any(
            o.get("output_type") == "execute_result" and "6" in str(o.get("content", ""))
            for o in data["outputs"]
        )
        assert has_result, "应有表达式求值结果"

    def test_execute_numpy(self):
        """测试 NumPy"""
        code = """
import numpy as np
arr = np.array([1, 2, 3, 4, 5])
print(f"数组: {arr}")
print(f"均值: {arr.mean()}")
print(f"求和: {arr.sum()}")
"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}/execute",
            headers=self.headers,
            json={"code": code, "cell_id": self.cell_id}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] == True, f"NumPy 执行失败: {data.get('outputs')}"

    def test_execute_pandas(self):
        """测试 Pandas"""
        code = """
import pandas as pd
df = pd.DataFrame({
    'name': ['Alice', 'Bob', 'Charlie'],
    'age': [25, 30, 35],
    'score': [85, 90, 88]
})
print(df.to_string())
print(f"平均年龄: {df['age'].mean()}")
"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}/execute",
            headers=self.headers,
            json={"code": code, "cell_id": self.cell_id}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] == True, f"Pandas 执行失败: {data.get('outputs')}"

    def test_execute_matplotlib(self):
        """测试 Matplotlib 图表"""
        code = """
import matplotlib.pyplot as plt
import numpy as np

x = np.linspace(0, 2 * np.pi, 100)
plt.figure(figsize=(8, 4))
plt.plot(x, np.sin(x), 'b-', label='sin(x)')
plt.plot(x, np.cos(x), 'r--', label='cos(x)')
plt.xlabel('x')
plt.ylabel('y')
plt.title('Trigonometric Functions')
plt.legend()
plt.grid(True, alpha=0.3)
plt.show()
"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}/execute",
            headers=self.headers,
            json={"code": code, "cell_id": self.cell_id}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] == True, f"Matplotlib 执行失败: {data.get('outputs')}"
        
        # 检查是否有图表输出
        has_image = any(
            o.get("mime_type") == "image/png" or 
            (o.get("output_type") == "display_data" and "base64" in str(o.get("content", "")))
            for o in data["outputs"]
        )
        assert has_image, "应该有图表输出"

    def test_execute_error(self):
        """测试错误处理"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}/execute",
            headers=self.headers,
            json={"code": "undefined_variable_xyz", "cell_id": self.cell_id}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] == False, "执行错误代码应返回 success=False"
        
        has_error = any(o.get("output_type") == "error" for o in data["outputs"])
        assert has_error, "应有错误输出"

    def test_execute_syntax_error(self):
        """测试语法错误"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}/execute",
            headers=self.headers,
            json={"code": "if True\n  print('missing colon')", "cell_id": self.cell_id}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] == False, "语法错误应返回 success=False"

    def test_timeout(self):
        """测试超时处理"""
        code = """
import time
time.sleep(10)
print("This should not print")
"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}/execute",
            headers=self.headers,
            json={"code": code, "cell_id": self.cell_id, "timeout": 2}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] == False, "超时应返回 success=False"

    def test_add_cell(self):
        """测试添加单元格"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}/cells",
            headers=self.headers,
            params={"cell_type": "code"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "id" in data, "应返回新单元格"
        assert data["cell_type"] == "code", "类型应为 code"

    def test_add_markdown_cell(self):
        """测试添加 Markdown 单元格"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}/cells",
            headers=self.headers,
            params={"cell_type": "markdown"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["cell_type"] == "markdown", "类型应为 markdown"

    def test_direct_execute(self):
        """测试直接执行代码（不保存）"""
        resp = requests.post(
            f"{BASE_URL}/api/codelab/execute",
            headers=self.headers,
            json={"code": "print('Direct execution')", "timeout": 10}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] == True

    def test_delete_notebook(self):
        """测试删除 Notebook"""
        resp = requests.delete(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}",
            headers=self.headers
        )
        assert resp.status_code == 200
        
        # 验证已删除
        resp = requests.get(
            f"{BASE_URL}/api/codelab/notebooks/{self.notebook_id}",
            headers=self.headers
        )
        assert resp.status_code == 404, "删除后应返回 404"

    def run_all(self):
        """运行所有测试"""
        print("\n🧪 代码实验室 API 测试\n")
        print("=" * 50)
        
        print("\n📁 Notebook 管理测试:")
        self.test("创建 Notebook", self.test_create_notebook)
        self.test("获取 Notebook 列表", self.test_list_notebooks)
        self.test("获取 Notebook 详情", self.test_get_notebook)
        self.test("更新 Notebook", self.test_update_notebook)
        
        print("\n⚡ 代码执行测试:")
        self.test("打印输出", self.test_execute_print)
        self.test("表达式求值", self.test_execute_expression)
        self.test("NumPy 计算", self.test_execute_numpy)
        self.test("Pandas 数据处理", self.test_execute_pandas)
        self.test("Matplotlib 图表", self.test_execute_matplotlib)
        
        print("\n🔴 错误处理测试:")
        self.test("运行时错误", self.test_execute_error)
        self.test("语法错误", self.test_execute_syntax_error)
        self.test("执行超时", self.test_timeout)
        
        print("\n📝 单元格管理测试:")
        self.test("添加代码单元格", self.test_add_cell)
        self.test("添加 Markdown 单元格", self.test_add_markdown_cell)
        
        print("\n🔧 其他测试:")
        self.test("直接执行代码", self.test_direct_execute)
        self.test("删除 Notebook", self.test_delete_notebook)
        
        print("\n" + "=" * 50)
        print(f"\n📊 测试结果: {self.passed} 通过, {self.failed} 失败")
        
        if self.failed == 0:
            print("🎉 所有测试通过!")
            return 0
        else:
            print("⚠️  部分测试失败，请检查")
            return 1


def main():
    print("🔐 正在获取认证 Token...")
    token = get_token()
    
    if not token:
        print("\n提示: 如果没有测试账号，请先注册:")
        print(f'  curl -X POST {BASE_URL}/api/auth/register \\')
        print(f'    -H "Content-Type: application/json" \\')
        print(f'    -d \'{{"email": "{TEST_EMAIL}", "username": "tester", "password": "{TEST_PASSWORD}"}}\'')
        sys.exit(1)
    
    print("✅ Token 获取成功\n")
    
    tester = CodelabTester(token)
    exit_code = tester.run_all()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
