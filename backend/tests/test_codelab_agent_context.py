import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import codelab
from app.services.react_agent import AgentCore


agent_module = codelab.codelab_agent_routes


def test_codelab_runtime_context_is_promoted_to_top_level_system_prompt():
    agent = AgentCore.__new__(AgentCore)
    agent.tools = SimpleNamespace(list_tools=lambda: [], get_tools_description=lambda: "")
    agent.runtime_context = SimpleNamespace(channel="codelab_agent")
    agent._routing_decision = None
    agent._last_tool_selection = {}
    agent._active_chat_preferences = {}
    agent._active_rag_overrides = {}
    agent._active_channel_system_context = (
        "You are the CodeLab notebook agent.\n"
        "Hard constraints:\n"
        "1. Do not import os."
    )

    prompt = agent._build_system_prompt([], function_calling=True)

    assert "## CodeLab / Notebook Runtime Context" in prompt
    assert "You are the CodeLab notebook agent." in prompt
    assert "Do not import os." in prompt


def test_build_notebook_agent_context_prefers_active_cell_and_tracks_recent_error():
    notebook = {
        "id": "nb-1",
        "title": "Iris Demo",
        "execution_count": 4,
        "cells": [
            {
                "id": "cell-import",
                "cell_type": "code",
                "source": "import pandas as pd\nimport seaborn as sns",
                "outputs": [],
                "execution_count": 1,
            },
            {
                "id": "cell-active",
                "cell_type": "code",
                "source": "df = pd.read_csv('iris.csv')\ndf.head()",
                "outputs": [{"output_type": "execute_result", "content": "5 rows x 5 columns"}],
                "execution_count": 2,
            },
            {
                "id": "cell-error",
                "cell_type": "code",
                "source": "joblib.dump(model, 'model.joblib')",
                "outputs": [
                    {
                        "output_type": "error",
                        "content": {
                            "ename": "PolicyViolationError",
                            "evalue": "不在白名单内的模块: joblib",
                            "traceback": [],
                        },
                    }
                ],
                "execution_count": 3,
            },
        ],
    }
    history = {
        "messages": [
            {"role": "user", "content": "先加载鸢尾花数据集"},
            {"role": "assistant", "content": "已经整理出数据加载思路"},
            {"role": "user", "content": "继续做特征分析"},
            {"role": "assistant", "content": "建议先看列分布和标签情况"},
            {"role": "user", "content": "为什么保存模型时报错？"},
        ],
        "summary_cache": {
            "recent_limit": 4,
            "message_count": 5,
            "summary": "缓存摘要: 用户正在处理模型保存报错",
            "updated_at": "2026-03-30T00:00:00",
        },
    }

    payload = agent_module._build_notebook_agent_context(
        "nb-1",
        notebook,
        include_variables=False,
        active_cell_id="cell-active",
        active_cell_index=None,
        history=history,
        user_authorized=False,
        workspace={
            "directory": "/app/uploads/codelab/notebooks/3/nb-1",
            "display_path": "uploads/codelab/notebooks/3/nb-1",
            "file_count": 2,
            "file_names": ["iris.csv", "notes.txt"],
            "file_paths": {
                "iris.csv": "/app/uploads/codelab/notebooks/3/nb-1/iris.csv",
                "notes.txt": "/app/uploads/codelab/notebooks/3/nb-1/notes.txt",
            },
            "files": [
                {"name": "iris.csv", "runtime_path": "/app/uploads/codelab/notebooks/3/nb-1/iris.csv"},
                {"name": "notes.txt", "runtime_path": "/app/uploads/codelab/notebooks/3/nb-1/notes.txt"},
            ],
        },
    )

    assert payload["focus"]["active_cell"]["cell_id"] == "cell-active"
    assert payload["focus"]["recent_error"]["cell_id"] == "cell-error"
    assert "joblib" in payload["focus"]["recent_error"]["error_summary"]
    assert "当前更适合优先处理最近报错" in payload["stage_summary"]
    assert payload["history_summary"] == "缓存摘要: 用户正在处理模型保存报错"
    assert payload["notebook_state_digest"]["notebook_id"] == "nb-1"
    assert "Iris Demo" in payload["notebook_state_digest"]["summary"]
    assert payload["task_memory"]["current_goal"] == "为什么保存模型时报错？"
    assert payload["task_memory"]["open_request"] == "为什么保存模型时报错？"
    assert payload["task_memory"]["constraints"] == ["先加载鸢尾花数据集"]
    assert len(payload["task_memory"]["recent_turns"]) == 2
    assert payload["recent_history_messages"] == payload["task_memory"]["recent_turns"]
    assert any(entry["kind"] == "recent_error" for entry in payload["action_ledger"])
    assert any(entry["kind"] == "workspace" for entry in payload["action_ledger"])
    assert "update Cell 3#3 directly with cell_id=cell-error" in payload["tool_hints"][0]
    assert "cell_id='cell-active'" in payload["tool_hints"][1]
    assert not any("uploaded_file_path('iris.csv')" in item for item in payload["tool_hints"])
    assert not any("Never import os/pathlib/glob" in item for item in payload["tool_hints"])
    assert not any("df = pd.read_csv(uploaded_file_path(files[0]))" in item for item in payload["tool_hints"])
    assert not any("not a string literal" in item for item in payload["tool_hints"])


def test_render_notebook_system_context_respects_context_and_variable_switches():
    context_payload = {
        "notebook_id": "nb-2",
        "notebook_title": "Demo",
        "cell_count": 2,
        "code_cell_count": 1,
        "execution_count": 1,
        "code_summary": "共 1 个代码单元格；已有数据加载/构造步骤",
        "stage_summary": "已有数据加载/构造步骤；最近执行结果可作为下一步依据",
        "history_summary": "更早用户目标: 先完成数据检查",
        "task_memory": {
            "summary": "当前目标: 检查 iris.csv 的读取结果；限制条件: 不要执行 notebook",
        },
        "action_ledger": [
            {
                "kind": "recent_output",
                "label": "Cell 2#2",
                "detail": "5 rows x 5 columns",
            }
        ],
        "notebook_state_digest": {
            "summary": "Demo (ID=nb-2, cells=2, code=1, exec=1) | 已有数据加载/构造步骤；最近执行结果可作为下一步依据",
        },
        "variables": {"df": "DataFrame shape=(150, 5)"},
        "tool_hints": [
            "When fixing the latest error, update Cell 1#1 directly with cell_id=cell-1; do not create a duplicate fix cell unless the user explicitly asks for a new cell.",
            "If the active cell matters, inspect it with notebook_cell(action='get_one', cell_id='cell-2').",
            "Use notebook_variables only when you need existing runtime variables.",
        ],
        "focus": {
            "active_cell": {
                "cell_id": "cell-2",
                "label": "Cell 2",
                "cell_index": 1,
                "kind": "data_loading",
                "source_excerpt": "df = pd.read_csv('iris.csv')",
            },
            "recent_error": None,
            "recent_output": None,
            "recent_executed": None,
        },
        "workspace": {
            "directory": "/app/uploads/codelab/notebooks/3/nb-2",
            "display_path": "uploads/codelab/notebooks/3/nb-2",
            "file_count": 2,
            "files": [
                {"name": "iris.csv"},
                {"name": "notes.txt"},
            ],
        },
    }

    without_variables = agent_module._render_notebook_system_context(
        context_payload,
        include_context=True,
        include_variables=False,
        user_authorized=False,
    )
    assert "Final answer language: Chinese" in without_variables
    assert "Stage: 已有数据加载/构造步骤" in without_variables
    assert "Hard constraints:" in without_variables
    assert "Do not discover upload paths with import os, pathlib, glob, shutil, subprocess, or sys." in without_variables
    assert "list_uploaded_files()" in without_variables
    assert "NOTEBOOK_FILE_PATHS" in without_variables
    assert "Notebook state digest: Demo (ID=nb-2, cells=2, code=1, exec=1)" in without_variables
    assert "Focus: active_cell=Cell 2#2[data_loading]" in without_variables
    assert "Task memory: 当前目标: 检查 iris.csv 的读取结果；限制条件: 不要执行 notebook" in without_variables
    assert "Recent notebook evidence: Cell 2#2: 5 rows x 5 columns" in without_variables
    assert "Tool strategy: When fixing the latest error, update Cell 1#1 directly with cell_id=cell-1" in without_variables
    assert "Workspace files: 2 uploaded file(s) (iris.csv, notes.txt)" in without_variables
    assert "File access contract: use list_uploaded_files(), uploaded_file_path(name), or read_uploaded_text(name)" in without_variables
    assert "Preferred first validation: files = list_uploaded_files(); df = pd.read_csv(uploaded_file_path('iris.csv'))" in without_variables
    assert "Literal rule: uploaded_file_path('iris.csv') is a Python function call" in without_variables
    assert "Variable snapshot:" not in without_variables
    assert "Authorization: not granted" in without_variables

    with_variables = agent_module._render_notebook_system_context(
        context_payload,
        include_context=True,
        include_variables=True,
        user_authorized=True,
    )
    assert "Variable snapshot: df=DataFrame shape=(150, 5)" in with_variables
    assert "Authorization: granted" in with_variables


def test_build_notebook_agent_context_marks_sandbox_risky_cells():
    notebook = {
        "id": "nb-risk",
        "title": "Risky Demo",
        "execution_count": 2,
        "cells": [
            {
                "id": "cell-risk",
                "cell_type": "code",
                "source": "import os\nimport pandas as pd\ndf = pd.read_csv('iris.csv')",
                "outputs": [],
                "execution_count": None,
            },
            {
                "id": "cell-next",
                "cell_type": "code",
                "source": "print(df.shape)",
                "outputs": [],
                "execution_count": None,
            },
        ],
    }

    payload = agent_module._build_notebook_agent_context(
        "nb-risk",
        notebook,
        include_variables=False,
        active_cell_id=None,
        active_cell_index=0,
        history={"messages": []},
        user_authorized=False,
        workspace={"file_count": 1, "file_names": ["iris.csv"], "files": [{"name": "iris.csv"}]},
    )

    risky_cells = payload["sandbox_risky_cells"]
    assert len(risky_cells) == 1
    assert risky_cells[0]["cell_id"] == "cell-risk"
    assert risky_cells[0]["blocked_imports"] == ["os"]
    assert any("Do not execute Cell 1#1 as-is" in item for item in payload["tool_hints"])

    system_context = agent_module._render_notebook_system_context(
        payload,
        include_context=True,
        include_variables=False,
        user_authorized=False,
    )
    assert "Risk: Cell 1#1 imports sandbox-blocked module(s): os" in system_context


def test_build_notebook_agent_context_tracks_history_gap():
    notebook = {
        "id": "nb-gap",
        "title": "Gap Demo",
        "execution_count": 0,
        "cells": [
            {
                "id": "cell-default",
                "cell_type": "code",
                "source": "import pandas as pd\nprint('hello')",
                "outputs": [],
                "execution_count": None,
            }
        ],
    }
    history = {
        "messages": [
            {"role": "user", "content": "利用上传的文件做机器学习案例"},
            {"role": "assistant", "content": "```python\nprint('plan only')\n```"},
            {"role": "user", "content": "你直接做一个案例出来"},
        ]
    }

    payload = agent_module._build_notebook_agent_context(
        "nb-gap",
        notebook,
        include_variables=False,
        active_cell_id=None,
        active_cell_index=None,
        history=history,
        user_authorized=False,
        workspace={"file_count": 1, "file_names": ["car_parts_final.csv"], "files": [{"name": "car_parts_final.csv"}]},
    )

    assert payload["history_health"]["trailing_user_messages"] == 1
    assert payload["history_health"]["assistant_code_responses"] == 1
    assert "历史中存在未完成的上一轮请求" in payload["stage_summary"]


def test_code_kind_does_not_treat_imported_ml_helpers_as_completed_pipeline():
    source = """# 启动内核并查看上传的文件
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

print("准备查看上传的文件...")
"""

    assert agent_module._summarize_code_kind(source, "code") == "imports"
