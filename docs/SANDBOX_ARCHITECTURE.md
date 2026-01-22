# CodeLab 沙箱架构说明

## 当前实现方式

### 概述

当前 CodeLab 模块使用 **简单进程沙箱** 来执行用户代码，通过 Python 的 `asyncio.create_subprocess_exec` 在后端容器内直接执行代码。

### 执行流程

```
用户代码 → 临时文件 → 子进程执行 → 捕获输出 → 返回结果
```

### 核心代码位置

```
backend/app/api/codelab.py
├── PYTHON_PRELUDE    # 执行前注入的代码（matplotlib配置、输出捕获等）
├── PYTHON_EPILOGUE   # 执行后的结果收集代码
└── execute_python_code()  # 主执行函数
```

### 安全措施

| 措施 | 实现状态 | 说明 |
|------|---------|------|
| 执行超时 | ✅ 已实现 | 默认 30 秒超时 |
| 进程隔离 | ✅ 基础实现 | 使用子进程执行 |
| 容器隔离 | ✅ Docker | 代码在 Docker 容器内运行 |
| 资源限制 | ❌ 未实现 | 无 CPU/内存限制 |
| 网络隔离 | ❌ 未实现 | 可访问网络 |
| 文件系统隔离 | ❌ 未实现 | 可访问容器文件系统 |
| 系统调用限制 | ❌ 未实现 | 无 seccomp 配置 |

## ⚠️ 安全警告

**当前实现不适合生产环境的多用户场景！** 恶意代码可能：

1. 读取容器内的敏感文件（环境变量、配置等）
2. 消耗大量 CPU/内存导致服务不可用
3. 发起网络请求
4. 影响其他用户的代码执行

## 增强方案

### 方案一：Docker-in-Docker 隔离

为每个代码执行请求创建独立的 Docker 容器：

```yaml
# docker-compose.yml 新增配置
services:
  codelab-executor:
    image: python:3.11-slim
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 512M
    network_mode: none  # 禁用网络
    read_only: true     # 只读文件系统
    security_opt:
      - no-new-privileges:true
```

**优点**：完全隔离，安全性高
**缺点**：启动延迟大，资源消耗高

### 方案二：使用 Pyodide (WebAssembly)

在浏览器端使用 Pyodide 执行 Python 代码：

```javascript
// 前端实现
import { loadPyodide } from "pyodide";

const pyodide = await loadPyodide();
await pyodide.loadPackage(["numpy", "pandas", "matplotlib"]);
const result = pyodide.runPython(code);
```

**优点**：完全隔离，零服务器负载
**缺点**：包支持有限，大数据处理性能差

### 方案三：gVisor/Firecracker 微虚拟机

使用 gVisor 或 Firecracker 提供内核级隔离：

```bash
# 使用 gVisor runsc 运行
docker run --runtime=runsc python:3.11-slim python script.py
```

**优点**：接近原生性能，安全性极高
**缺点**：部署复杂，需要特定内核支持

### 方案四：RestrictedPython + 资源限制

使用 RestrictedPython 库限制可执行的代码：

```python
from RestrictedPython import compile_restricted
from RestrictedPython.Guards import safe_builtins

code = compile_restricted(user_code, '<string>', 'exec')
exec(code, {'__builtins__': safe_builtins})
```

**优点**：实现简单，轻量级
**缺点**：可能误杀合法代码，安全性不如容器方案

## 推荐方案

### 开发/测试环境
继续使用当前方案，但需确保：
- 仅限可信用户使用
- 定期重启后端容器
- 监控资源使用

### 生产环境（单用户/小规模）
采用 **方案四 + 资源限制**：
1. 安装 RestrictedPython
2. 设置进程级 CPU/内存限制
3. 使用 tmpfs 挂载临时目录

### 生产环境（多用户）
采用 **方案一 Docker-in-Docker**：
1. 预热容器池减少启动延迟
2. 严格的资源配额
3. 网络完全隔离

## 资源限制配置示例

### 使用 resource 模块（Linux）

```python
import resource

def set_limits():
    # 限制 CPU 时间为 5 秒
    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    # 限制内存为 256MB
    resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
    # 限制进程数
    resource.setrlimit(resource.RLIMIT_NPROC, (10, 10))
```

### Docker Compose 资源限制

```yaml
services:
  backend:
    deploy:
      resources:
        limits:
          cpus: '2'
          memory: 2G
        reservations:
          cpus: '0.5'
          memory: 512M
```

## 监控与告警

建议添加以下监控：

1. **代码执行时间监控** - 识别慢查询
2. **资源使用监控** - CPU、内存峰值
3. **异常代码检测** - 记录可疑模式
4. **用户行为分析** - 识别滥用行为

## 相关文件

- `backend/app/api/codelab.py` - 代码执行核心
- `backend/Dockerfile` - 后端容器配置
- `docker-compose.yml` - 服务编排配置
