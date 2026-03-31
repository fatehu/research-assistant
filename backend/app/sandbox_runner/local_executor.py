"""
CodeLab sandboxed executor.

Runs user code in a dedicated subprocess per notebook kernel to avoid
blocking/compromising the API process.
"""
from __future__ import annotations

import ast
import json
import queue
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from loguru import logger


POLICY_DENIED_IMPORTS = {
    "os",
    "subprocess",
    "socket",
    "pathlib",
    "shutil",
    "tempfile",
    "requests",
    "httpx",
    "urllib",
    "aiohttp",
    "ftplib",
    "telnetlib",
    "paramiko",
    "multiprocessing",
    "threading",
    "ctypes",
    "importlib",
    "pickle",
    "marshal",
}

POLICY_DENIED_CALLS = {
    "__import__",
    "eval",
    "exec",
    "open",
    "compile",
    "input",
    "breakpoint",
    "globals",
    "locals",
    "vars",
}

POLICY_DENIED_ATTRS = {
    "system",
    "popen",
    "spawn",
    "fork",
    "execv",
    "execve",
    "run",
    "kill",
}

ALLOWED_IMPORT_ROOTS = {
    "math",
    "random",
    "statistics",
    "datetime",
    "time",
    "warnings",
    "json",
    "re",
    "collections",
    "itertools",
    "functools",
    "typing",
    "decimal",
    "fractions",
    "numpy",
    "pandas",
    "matplotlib",
    "sklearn",
    "torch",
    "seaborn",
    "joblib",
}


@dataclass
class PolicyViolation:
    code: str
    message: str


def validate_code_policy(code: str) -> Optional[PolicyViolation]:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            else:
                if node.module:
                    modules = [node.module]
            for module in modules:
                root = str(module).split(".")[0]
                if root in POLICY_DENIED_IMPORTS:
                    return PolicyViolation(
                        code="forbidden_import",
                        message=f"禁止导入模块: {root}",
                    )
                if root not in ALLOWED_IMPORT_ROOTS:
                    return PolicyViolation(
                        code="forbidden_import",
                        message=f"不在白名单内的模块: {root}",
                    )

        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                call_name = str(node.func.id)
                if call_name in POLICY_DENIED_CALLS:
                    return PolicyViolation(
                        code="forbidden_call",
                        message=f"禁止调用函数: {call_name}",
                    )
            if isinstance(node.func, ast.Attribute):
                attr = str(node.func.attr)
                if attr in POLICY_DENIED_ATTRS:
                    return PolicyViolation(
                        code="forbidden_call",
                        message=f"禁止调用危险属性: {attr}",
                    )

    return None


_WORKER_CODE = r"""
import ast
import base64
import io
import json
import os
import traceback
import time
from contextlib import redirect_stdout, redirect_stderr

_ORIGINAL_IMPORT = __import__

POLICY_DENIED_IMPORTS = {
    "os", "subprocess", "socket", "pathlib", "shutil", "tempfile", "requests",
    "httpx", "urllib", "aiohttp", "ftplib", "telnetlib", "paramiko",
    "multiprocessing", "threading", "ctypes", "importlib", "pickle", "marshal"
}
POLICY_DENIED_CALLS = {"__import__", "eval", "exec", "open", "compile", "input", "breakpoint", "globals", "locals", "vars"}
POLICY_DENIED_ATTRS = {"system", "popen", "spawn", "fork", "execv", "execve", "run", "kill"}

ALLOWED_IMPORT_ROOTS = {
    "math", "random", "statistics", "datetime", "time", "warnings", "json", "re", "collections",
    "itertools", "functools", "typing", "decimal", "fractions", "numpy",
    "pandas", "matplotlib", "sklearn", "torch", "seaborn", "joblib"
}


def _policy_check(code: str):
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            else:
                if node.module:
                    modules = [node.module]
            for module in modules:
                root = str(module).split(".")[0]
                if root in POLICY_DENIED_IMPORTS:
                    return ("forbidden_import", f"禁止导入模块: {root}")
                if root not in ALLOWED_IMPORT_ROOTS:
                    return ("forbidden_import", f"不在白名单内的模块: {root}")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in POLICY_DENIED_CALLS:
                return ("forbidden_call", f"禁止调用函数: {node.func.id}")
            if isinstance(node.func, ast.Attribute) and node.func.attr in POLICY_DENIED_ATTRS:
                return ("forbidden_call", f"禁止调用危险属性: {node.func.attr}")
    return None


def _blocked_open(*args, **kwargs):
    raise PermissionError("沙箱禁止文件系统访问")


def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = str(name).split(".")[0]
    if root in POLICY_DENIED_IMPORTS or root not in ALLOWED_IMPORT_ROOTS:
        raise ImportError(f"沙箱禁止导入模块: {name}")
    return _ORIGINAL_IMPORT(name, globals, locals, fromlist, level)


def _safe_builtins():
    builtins_map = __builtins__ if isinstance(__builtins__, dict) else __builtins__.__dict__
    allowed = [
        "abs", "all", "any", "bool", "dict", "enumerate", "float", "int", "len",
        "list", "max", "min", "pow", "print", "range", "round", "set", "slice",
        "sorted", "str", "sum", "tuple", "zip", "map", "filter", "format",
        "getattr", "hasattr", "isinstance", "issubclass", "type", "Exception",
        "ValueError", "TypeError", "KeyError", "IndexError", "RuntimeError"
    ]
    safe = {name: builtins_map[name] for name in allowed if name in builtins_map}
    safe["__import__"] = _safe_import
    safe["open"] = _blocked_open
    return safe


namespace = {"__builtins__": _safe_builtins()}
execution_count = 0
last_used_at = time.time()
_WORKSPACE_DIR = ""
_WORKSPACE_FILES = []
_WORKSPACE_FILE_PATHS = {}


def _resolve_uploaded_file(name: str):
    raw_name = str(name or "").strip()
    if not raw_name:
        raise FileNotFoundError("文件名不能为空")
    candidate = _WORKSPACE_FILE_PATHS.get(raw_name)
    if candidate and os.path.isfile(candidate):
        return candidate
    if _WORKSPACE_DIR:
        normalized = os.path.abspath(os.path.join(_WORKSPACE_DIR, raw_name))
        workspace_root = os.path.abspath(_WORKSPACE_DIR)
        if normalized.startswith(workspace_root) and os.path.isfile(normalized):
            return normalized
    raise FileNotFoundError(f"找不到上传文件: {raw_name}")


def _apply_workspace(workspace):
    global _WORKSPACE_DIR, _WORKSPACE_FILES, _WORKSPACE_FILE_PATHS
    workspace = workspace if isinstance(workspace, dict) else {}
    directory = str(workspace.get("directory") or "").strip()
    files = list(workspace.get("file_names") or [])
    file_paths = dict(workspace.get("file_paths") or {})

    if directory:
        os.makedirs(directory, exist_ok=True)
        try:
            os.chdir(directory)
        except Exception:
            pass

    _WORKSPACE_DIR = directory
    _WORKSPACE_FILES = [str(item) for item in files if str(item or "").strip()]
    _WORKSPACE_FILE_PATHS = {
        str(key): str(value)
        for key, value in file_paths.items()
        if str(key or "").strip() and str(value or "").strip()
    }

    def list_uploaded_files():
        return list(_WORKSPACE_FILES)

    def uploaded_file_path(name: str):
        return _resolve_uploaded_file(name)

    def read_uploaded_text(name: str, encoding: str = "utf-8"):
        path = _resolve_uploaded_file(name)
        with open(path, "r", encoding=encoding) as handle:
            return handle.read()

    namespace["NOTEBOOK_FILES_DIR"] = _WORKSPACE_DIR
    namespace["NOTEBOOK_FILES"] = list(_WORKSPACE_FILES)
    namespace["NOTEBOOK_FILE_PATHS"] = dict(_WORKSPACE_FILE_PATHS)
    namespace["list_uploaded_files"] = list_uploaded_files
    namespace["uploaded_file_path"] = uploaded_file_path
    namespace["read_uploaded_text"] = read_uploaded_text


def _capture_plot(ns, outputs):
    try:
        import matplotlib
        matplotlib.use("Agg")
        try:
            matplotlib.rcParams["font.sans-serif"] = [
                "WenQuanYi Zen Hei",
                "Noto Sans CJK JP",
                "Noto Serif CJK JP",
                "SimHei",
                "Microsoft YaHei",
                "Arial Unicode MS",
                "DejaVu Sans",
            ]
            matplotlib.rcParams["axes.unicode_minus"] = False
        except Exception:
            pass
        import matplotlib.pyplot as plt
        if plt.get_fignums():
            buf = io.BytesIO()
            plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
            buf.seek(0)
            outputs.append({
                "output_type": "display_data",
                "content": "data:image/png;base64," + base64.b64encode(buf.read()).decode("utf-8"),
                "mime_type": "image/png",
            })
            plt.close("all")
    except Exception:
        pass


def _init_namespace():
    init_code = '''
import math
import random
import time
import datetime
import json
import re
import collections
import itertools
import functools
import warnings
try:
    import numpy as np
except Exception:
    pass
try:
    import pandas as pd
except Exception:
    pass
try:
    import matplotlib
    matplotlib.use('Agg')
    matplotlib.rcParams['font.sans-serif'] = [
        'WenQuanYi Zen Hei',
        'Noto Sans CJK JP',
        'Noto Serif CJK JP',
        'SimHei',
        'Microsoft YaHei',
        'Arial Unicode MS',
        'DejaVu Sans',
    ]
    matplotlib.rcParams['axes.unicode_minus'] = False
    warnings.filterwarnings('ignore', message='Glyph .* missing from current font.')
    import matplotlib.pyplot as plt
except Exception:
    pass
'''
    try:
        exec(init_code, namespace)
    except Exception:
        pass


def _format_value(value):
    try:
        if hasattr(value, "to_string") and hasattr(value, "shape"):
            if hasattr(value, "head"):
                return value.head(50).to_string()
            return value.to_string()
        return repr(value)
    except Exception:
        return str(value)


def _variable_snapshot():
    vars_map = {}
    previews = {}
    for name, value in namespace.items():
        if name.startswith("_"):
            continue
        if callable(value) or isinstance(value, type):
            continue
        try:
            vars_map[name] = type(value).__name__
        except Exception:
            vars_map[name] = "unknown"
        try:
            if hasattr(value, "shape"):
                previews[name] = f"shape={getattr(value, 'shape', None)}"
            elif hasattr(value, "__len__") and not isinstance(value, str):
                previews[name] = f"len={len(value)}"
            else:
                text = repr(value)
                previews[name] = text[:160] + ("..." if len(text) > 160 else "")
        except Exception:
            previews[name] = "preview_unavailable"
    return vars_map, previews


def _execute_code(code: str):
    global execution_count
    execution_count += 1
    outputs = []
    success = True
    start = time.time()

    policy = _policy_check(code)
    if policy:
        p_code, p_msg = policy
        return {
            "success": False,
            "outputs": [{
                "output_type": "error",
                "content": {"ename": "PolicyViolationError", "evalue": p_msg, "traceback": []},
            }],
            "execution_count": execution_count,
            "execution_time_ms": int((time.time() - start) * 1000),
            "terminated_reason": "policy_violation",
            "policy_violation_code": p_code,
            "variables": _variable_snapshot()[0],
            "variable_previews": _variable_snapshot()[1],
        }

    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()
    try:
        lines = code.strip().split("\n")
        last_line = lines[-1].strip() if lines else ""
        last_expr_value = None
        main_code = code

        try:
            if last_line and not any(
                last_line.startswith(kw)
                for kw in ["import ", "from ", "def ", "class ", "if ", "for ", "while ", "try:", "with ", "return ", "raise ", "pass", "break", "continue", "#", "@"]
            ):
                if "=" in last_line and not any(op in last_line for op in ["==", "!=", "<=", ">=", "+=", "-=", "*=", "/="]):
                    pass
                else:
                    compile(last_line, "<string>", "eval")
                    main_code = "\n".join(lines[:-1]) if len(lines) > 1 else ""
        except SyntaxError:
            pass

        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            if main_code.strip():
                exec(main_code, namespace)
            if main_code != code and last_line:
                try:
                    last_expr_value = eval(last_line, namespace)
                except Exception:
                    exec(last_line, namespace)
            elif not main_code.strip() and last_line:
                try:
                    last_expr_value = eval(code, namespace)
                except Exception:
                    exec(code, namespace)

        stdout_text = stdout_capture.getvalue()
        if stdout_text:
            outputs.append({
                "output_type": "stream",
                "content": stdout_text.rstrip("\n"),
                "mime_type": "text/plain",
            })

        _capture_plot(namespace, outputs)

        if last_expr_value is not None:
            outputs.append({
                "output_type": "execute_result",
                "content": _format_value(last_expr_value),
                "mime_type": "text/plain",
            })

        stderr_text = stderr_capture.getvalue()
        if stderr_text:
            filtered = [l for l in stderr_text.split("\n") if l and not l.startswith("WARNING")]
            if filtered:
                outputs.append({
                    "output_type": "stream",
                    "content": "\n".join(filtered),
                    "mime_type": "text/stderr",
                })

    except Exception as exc:
        success = False
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        outputs.append({
            "output_type": "error",
            "content": {"ename": type(exc).__name__, "evalue": str(exc), "traceback": tb},
        })
    finally:
        stdout_capture.close()
        stderr_capture.close()

    vars_map, previews = _variable_snapshot()
    return {
        "success": success,
        "outputs": outputs,
        "execution_count": execution_count,
        "execution_time_ms": int((time.time() - start) * 1000),
        "terminated_reason": "none",
        "policy_violation_code": None,
        "variables": vars_map,
        "variable_previews": previews,
    }


def _handle(req):
    global namespace, execution_count, last_used_at
    cmd = req.get("cmd")
    _apply_workspace(req.get("workspace"))
    if cmd == "execute":
        last_used_at = time.time()
        return _execute_code(req.get("code", ""))
    if cmd == "variables":
        vars_map, previews = _variable_snapshot()
        return {"variables": vars_map, "variable_previews": previews, "execution_count": execution_count}
    if cmd == "reset":
        namespace = {"__builtins__": _safe_builtins()}
        execution_count = 0
        _init_namespace()
        vars_map, previews = _variable_snapshot()
        return {"success": True, "variables": vars_map, "variable_previews": previews, "execution_count": execution_count}
    return {"error": f"unknown_cmd:{cmd}"}


def _print_response(req_id, payload):
    sys.stdout.write(json.dumps({"request_id": req_id, "payload": payload}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


import sys
_init_namespace()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
        req_id = req.get("request_id")
        payload = _handle(req)
        _print_response(req_id, payload)
    except Exception as exc:
        _print_response(req.get("request_id") if isinstance(req, dict) else None, {
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })
"""


class _WorkerProcess:
    def __init__(self) -> None:
        self.process: Optional[subprocess.Popen[str]] = None
        self._stdout_queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._stderr_thread: Optional[threading.Thread] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._start()

    def _start(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", _WORKER_CODE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _pump_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
                if isinstance(payload, dict):
                    self._stdout_queue.put(payload)
            except Exception:
                logger.debug(f"[CodeLabWorker] 非JSON输出: {text[:200]}")

    def _pump_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            text = line.rstrip()
            if text:
                logger.warning(f"[CodeLabWorker] stderr: {text}")

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def close(self) -> None:
        proc = self.process
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
        except Exception:
            logger.debug("[CodeLabWorker] close异常", exc_info=True)
        finally:
            self.process = None

    def restart(self) -> None:
        self.close()
        self._start()

    def call(self, cmd: Dict[str, Any], timeout_seconds: float) -> Dict[str, Any]:
        with self._lock:
            if not self.is_alive():
                self.restart()
            if self.process is None or self.process.stdin is None:
                raise RuntimeError("worker_not_ready")

            request_id = str(uuid.uuid4())
            payload = dict(cmd)
            payload["request_id"] = request_id
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

            deadline = time.time() + timeout_seconds
            while time.time() < deadline:
                remaining = max(0.01, deadline - time.time())
                try:
                    item = self._stdout_queue.get(timeout=remaining)
                except queue.Empty:
                    continue
                if str(item.get("request_id")) == request_id:
                    response_payload = item.get("payload")
                    if isinstance(response_payload, dict):
                        return response_payload
                    raise RuntimeError("invalid_worker_payload")
            raise TimeoutError("worker_timeout")


class CodeLabExecutor:
    def __init__(self, notebook_id: str, hard_timeout_seconds: int) -> None:
        self.notebook_id = notebook_id
        self.hard_timeout_seconds = max(1, int(hard_timeout_seconds))
        self._worker = _WorkerProcess()
        self._last_variables: Dict[str, str] = {}
        self._last_variable_previews: Dict[str, str] = {}
        self._last_execution_count: int = 0
        self._last_workspace_context: Optional[Dict[str, Any]] = None

    def close(self) -> None:
        self._worker.close()

    def reset(self, workspace_context: Optional[Dict[str, Any]] = None) -> None:
        if workspace_context is not None:
            self._last_workspace_context = dict(workspace_context)
        effective_workspace = workspace_context if workspace_context is not None else self._last_workspace_context
        try:
            payload = self._worker.call({"cmd": "reset", "workspace": effective_workspace}, timeout_seconds=3)
            self._last_variables = dict(payload.get("variables", {}) or {})
            self._last_variable_previews = dict(payload.get("variable_previews", {}) or {})
            self._last_execution_count = int(payload.get("execution_count", 0) or 0)
        except Exception:
            logger.warning(f"[CodeLabExecutor] reset失败 notebook_id={self.notebook_id}")
            self._worker.restart()
            self._last_variables = {}
            self._last_variable_previews = {}
            self._last_execution_count = 0

    def get_variables(self, workspace_context: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        if workspace_context is not None:
            self._last_workspace_context = dict(workspace_context)
        effective_workspace = workspace_context if workspace_context is not None else self._last_workspace_context
        try:
            payload = self._worker.call({"cmd": "variables", "workspace": effective_workspace}, timeout_seconds=2)
            self._last_variables = dict(payload.get("variables", {}) or {})
            self._last_variable_previews = dict(payload.get("variable_previews", {}) or {})
            self._last_execution_count = int(payload.get("execution_count", 0) or self._last_execution_count)
        except Exception:
            logger.warning(f"[CodeLabExecutor] get_variables失败 notebook_id={self.notebook_id}")
        return dict(self._last_variables)

    def get_variable_preview(self, name: str) -> Optional[str]:
        if not name:
            return None
        if name not in self._last_variable_previews:
            self.get_variables()
        return self._last_variable_previews.get(name)

    def has_variable(self, name: str) -> bool:
        if name in self._last_variables:
            return True
        variables = self.get_variables()
        return name in variables

    def execute(
        self,
        code: str,
        timeout_seconds: int,
        workspace_context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if workspace_context is not None:
            self._last_workspace_context = dict(workspace_context)
        effective_workspace = workspace_context if workspace_context is not None else self._last_workspace_context
        policy = validate_code_policy(code)
        if policy is not None:
            return {
                "success": False,
                "outputs": [
                    {
                        "output_type": "error",
                        "content": {
                            "ename": "PolicyViolationError",
                            "evalue": policy.message,
                            "traceback": [],
                        },
                    }
                ],
                "execution_count": self._last_execution_count,
                "execution_time_ms": 0,
                "terminated_reason": "policy_violation",
                "policy_violation_code": policy.code,
                "variables": dict(self._last_variables),
                "variable_previews": dict(self._last_variable_previews),
            }

        timeout_value = max(1, min(int(timeout_seconds or 1), self.hard_timeout_seconds))
        started = time.time()
        try:
            payload = self._worker.call(
                {"cmd": "execute", "code": code, "workspace": effective_workspace},
                timeout_seconds=float(timeout_value),
            )
            self._last_variables = dict(payload.get("variables", {}) or {})
            self._last_variable_previews = dict(payload.get("variable_previews", {}) or {})
            self._last_execution_count = int(payload.get("execution_count", self._last_execution_count) or 0)
            if "terminated_reason" not in payload:
                payload["terminated_reason"] = "none"
            if "policy_violation_code" not in payload:
                payload["policy_violation_code"] = None
            return payload
        except TimeoutError:
            logger.warning(
                f"[CodeLabExecutor] 执行超时，强制终止并重启 worker notebook_id={self.notebook_id}, timeout={timeout_value}s"
            )
            self._worker.restart()
            return {
                "success": False,
                "outputs": [
                    {
                        "output_type": "error",
                        "content": {
                            "ename": "TimeoutError",
                            "evalue": f"执行超时（>{timeout_value}s）",
                            "traceback": [],
                        },
                    }
                ],
                "execution_count": self._last_execution_count,
                "execution_time_ms": int((time.time() - started) * 1000),
                "terminated_reason": "timeout",
                "policy_violation_code": None,
                "variables": dict(self._last_variables),
                "variable_previews": dict(self._last_variable_previews),
            }
        except Exception as exc:
            logger.error(
                f"[CodeLabExecutor] 执行异常 notebook_id={self.notebook_id}: {exc}\n{traceback.format_exc()}"
            )
            return {
                "success": False,
                "outputs": [
                    {
                        "output_type": "error",
                        "content": {
                            "ename": type(exc).__name__,
                            "evalue": str(exc),
                            "traceback": [],
                        },
                    }
                ],
                "execution_count": self._last_execution_count,
                "execution_time_ms": int((time.time() - started) * 1000),
                "terminated_reason": "none",
                "policy_violation_code": None,
                "variables": dict(self._last_variables),
                "variable_previews": dict(self._last_variable_previews),
            }
