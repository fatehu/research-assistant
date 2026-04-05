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
    assert "优先传 cell_id" in payload["tool_hints"][0]
    assert "cell_id='cell-active'" in payload["tool_hints"][1]
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
            "修复最近报错时，优先直接覆盖 Cell 1#1（cell_id=cell-1），并优先传 cell_id，不要再创建一个重复的修复版 cell。",
            "当前焦点可先查 notebook_cell(action='get_one', cell_id='cell-2')。",
            "需要确认 DataFrame、模型或数组状态时，再用 notebook_variables。",
            "当前工作区已有 2 个上传文件；读文件时优先直接用相对路径，或用 uploaded_file_path('iris.csv') / read_uploaded_text(...)。",
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
    assert "阶段: 已有数据加载/构造步骤" in without_variables
    assert "焦点: 当前单元格=Cell 2#2[data_loading]" in without_variables
    assert "工具策略: 修复最近报错时，优先直接覆盖 Cell 1#1（cell_id=cell-1），并优先传 cell_id，不要再创建一个重复的修复版 cell。" in without_variables
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
    assert any("不要建议直接执行 Cell 1#1" in item for item in payload["tool_hints"])

    system_context = agent_module._render_notebook_system_context(
        payload,
        include_context=True,
        include_variables=False,
        user_authorized=False,
    )
    assert "风险: Cell 1#1 包含沙箱禁用导入 os" in system_context


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


def test_build_codelab_shortcut_answer_blocks_direct_run_of_risky_cell():
    context_payload = {
        "sandbox_risky_cells": [
            {
                "label": "Cell 2",
                "cell_index": 1,
                "blocked_imports": ["os"],
            }
        ],
        "workspace": {
            "files": [{"name": "car_parts_final.csv"}],
        },
        "focus": {
            "recent_error": {
                "label": "Cell 8",
                "error_summary": "NameError: name 'ml_results' is not defined",
            }
        },
    }

    answer = agent_module._build_codelab_shortcut_answer(
        "请只根据当前 notebook 和已上传文件，判断下一步应该运行哪个 cell；不要联网，不要修改 notebook。",
        context_payload,
    )

    assert "当前不建议直接运行任何现有 cell" in answer
    assert "应先修复 Cell 2" in answer
    assert "car_parts_final.csv" in answer
    assert "Cell 8" in answer


def test_build_codelab_shortcut_answer_prioritizes_fix_question():
    context_payload = {
        "sandbox_risky_cells": [
            {
                "label": "Cell 2",
                "cell_index": 1,
                "blocked_imports": ["os"],
            }
        ],
        "workspace": {
            "files": [{"name": "car_parts_final.csv"}],
        },
        "focus": {
            "recent_error": {
                "label": "Cell 8",
                "error_summary": "NameError: name 'ml_results' is not defined",
            }
        },
    }

    answer = agent_module._build_codelab_shortcut_answer(
        "当前 notebook 哪个 cell 需要优先修复？不要修改 notebook，只回答结论。",
        context_payload,
    )

    assert "优先修复 Cell 2" in answer
    assert "沙箱禁用导入" in answer
    assert "Cell 8" in answer


def test_build_codelab_shortcut_answer_diagnoses_notebook_state_gap():
    context_payload = {
        "cell_count": 1,
        "code_cell_count": 1,
        "execution_count": 0,
        "recent_outputs": [],
        "variables": {},
        "workspace": {
            "files": [{"name": "car_parts_final.csv"}],
        },
        "history_health": {
            "trailing_user_messages": 1,
            "assistant_code_responses": 1,
        },
    }

    answer = agent_module._build_codelab_shortcut_answer(
        "这个 notebook 当前暴露了什么系统问题？只基于当前 notebook 状态回答。",
        context_payload,
    )

    assert "execution_count=0" in answer
    assert "上传文件已经在工作区里" in answer
    assert "聊天结论和 Notebook 状态是脱节的" in answer
    assert "未闭合的用户请求" in answer
