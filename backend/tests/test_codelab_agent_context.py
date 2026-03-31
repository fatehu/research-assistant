import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.api import codelab


agent_module = codelab.codelab_agent_routes


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
    assert "不要再创建一个重复的修复版 cell" in payload["tool_hints"][0]
    assert "notebook_cell(action='get_one', cell_index=2)" in payload["tool_hints"][1]
    assert any("uploaded_file_path('iris.csv')" in item for item in payload["tool_hints"])
    assert any("不要导入 os 去枚举目录" in item for item in payload["tool_hints"])
    assert any("df = pd.read_csv('iris.csv')" in item for item in payload["tool_hints"])
    assert any("不要一开始就写多层 try/except" in item for item in payload["tool_hints"])
    assert any("不要把它写成字符串" in item for item in payload["tool_hints"])


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
        "variables": {"df": "DataFrame shape=(150, 5)"},
        "tool_hints": [
            "修复最近报错时，优先直接覆盖 Cell 1#1，不要再创建一个重复的修复版 cell。",
            "当前焦点可先查 notebook_cell(action='get_one', cell_index=2)。",
            "需要确认 DataFrame、模型或数组状态时，再用 notebook_variables。",
            "当前工作区已有 2 个上传文件；读文件时优先直接用相对路径，或用 uploaded_file_path('iris.csv') / read_uploaded_text(...)。",
        ],
        "focus": {
            "active_cell": {
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
    assert "阶段: 已有数据加载/构造步骤" in without_variables
    assert "焦点: 当前单元格=Cell 2#2[data_loading]" in without_variables
    assert "工具策略: 修复最近报错时，优先直接覆盖 Cell 1#1，不要再创建一个重复的修复版 cell。" in without_variables
    assert "工作区: 2 个上传文件（iris.csv, notes.txt）" in without_variables
    assert "变量快照:" not in without_variables
    assert "未授权，只能给建议" in without_variables

    with_variables = agent_module._render_notebook_system_context(
        context_payload,
        include_context=True,
        include_variables=True,
        user_authorized=True,
    )
    assert "变量快照: df=DataFrame shape=(150, 5)" in with_variables
    assert "已授权，可直接操作 Notebook" in with_variables
