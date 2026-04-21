#!/usr/bin/env python3
"""Render short seed prompts for paper reproduction stages."""

from __future__ import annotations

import argparse
import json


def _build_prompt(stage: str, paper_id: int, project_id: int | None, goal: str | None, preferred_draft_id: str | None) -> str:
    header = [f"paper_id={paper_id}"]
    if project_id is not None:
        header.append(f"project_id={project_id}")
    if goal:
        header.append(f"goal={goal}")
    if preferred_draft_id:
        header.append(f"preferred_draft_id={preferred_draft_id}")
    prefix = "\n".join(header)

    templates = {
        "planning": [
            "请使用 paper-reproduction skill，继续 planning / intake 阶段。",
            "先调用 paper_research_status 判断现有 Project / workspace / intake 状态。",
            "只有在 workspace 或 structured intake 缺失、损坏、或被明确要求刷新时，才调用 paper_research_prepare。",
            "本轮只输出 planning result，不要执行训练，不要创建 run draft。",
        ],
        "implementation_prep": [
            "请使用 paper-reproduction skill，继续 implementation-prep 阶段。",
            "先读当前状态，优先复用已归档 planning / repo 证据。",
            "在生成或修订 implementation_spec 之前，先调用 paper_research_inspect_runtime，把 runtime candidates 和 runtime_worker.environment 当成方案输入条件，而不是等 execution 再看。",
            "implementation_spec 必须反映当前机器/worker 的真实环境约束，例如可用运行方式、已安装关键包、缺失包、可用命令、repo root/cwd 约束。",
            "如果 implementation_spec 已存在，优先读回再决定是复用还是局部修订。",
            "如果 repo 已经 materialize，必须先按 repo 真实文件校验 dataset 和 entrypoint；不要把仓库里已存在的 Dataset/* 文件继续写成 missing/needs download。",
            "如果 paper_research_inspect_runtime 已返回 runtime candidates，就不要再写 runtime_unknown 这一类 blocker。",
            "不要让 intake 只基于论文内容生成抽象方案；implementation_spec 需要同时吸收论文事实、repo/data 事实、和 runtime/environment 事实。",
            "如果证据不足以形成 grounded implementation_spec，就停止并列出 blockers。",
        ],
        "run_drafts": [
            "请使用 paper-reproduction skill，继续 run-drafts 阶段。",
            "必须先读取 implementation_spec，再决定是否创建或修订 drafts/run_drafts.json。",
            "只生成 grounded drafts，不要执行训练，不要假装 baseline 已成功。",
            "run_drafts 必须严格使用当前 schema：每个 draft 使用 id/kind/title/objective/entrypoint{type,path_or_hint}/depends_on/data_requirements/env_requirements/params/expected_outputs/blockers/evidence_files/grounding_notes。",
            "repo 文件 entrypoint 一律写成 entrypoint.type=repo_script，并把 repo-relative 文件名写进 entrypoint.path_or_hint，例如 seq2seq.py。",
            "evidence_files 一律写 canonical archived paths，例如 repo/source/seq2seq.py 或 specs/implementation_spec.json。",
            "不要再使用旧字段名或旧类型：draft_id、label、description、goal、changes、entrypoint.path、python_script。",
        ],
        "execution": [
            "请使用 paper-reproduction skill，继续 execution 阶段。",
            "先读取 run_drafts 并选择一个具体 draft；如果 smoke 已成功，不要重复 smoke，优先推进 baseline_repro。",
            "execution 阶段不是重新做大范围 repo 发现。implementation_spec.json 和 run_drafts.json 是当前真值；execution 新发现只应用来修整这两份真值，然后继续。",
            "如果 preferred_draft_id 或选中的 execution_id 已有归档 spec/result，必须先读回；已有结果足够回答时不要新建或重跑。",
            "如果 preferred_draft_id=baseline_repro，而旧 baseline 失败早于一个新的 data_prep 成功结果，且旧失败是坏 HDF5 或 schema mismatch，不要把那个旧 baseline 当成最终结果；它已经过时，必须基于新的 data_prep 继续推进。",
            "data_prep 成功后，先确认准备好的 artifact 已经存在且可被 baseline loader 使用，然后立刻写一个 fresh baseline_repro execution_spec 并 start_execution。",
            "然后调用 paper_research_inspect_runtime，检查 runtime candidates 和 runtime_worker.environment。",
            "repo-backed baseline 不要只看通用 environment 摘要。必须先读取 README、requirements/pyproject/environment/setup 这类依赖文件（如果存在），再读取选中的 entrypoint 脚本，并按需要读取少量本地模块来理解 imports。",
            "定位代码片段时，不要对同一个 repo 文件反复把 max_chars 从 3000 增加到 10000。先用 paper_research_search_repo 找命中行，再用 paper_research_read_repo_file(line_start,line_end) 读取局部范围。",
            "如果 execution 或 repo 证据暴露了新的 grounded 事实，例如 Dataset 已存在、runtime 已可用、缺 numpyencoder、正确 argv 已确认，先修订 implementation_spec 或 run_drafts，再生成下一次 execution_spec。",
            "一旦真值文件已经更新，不要继续重复读取大段 README、目录树或脚本尾部；只读取解决当前矛盾所需的最小文件。",
            "依赖判断以 repo 证据为准，不要让固定默认 ML 包清单替代 repo 自己声明的依赖。",
            "如果从 repo 证据推断出具体依赖缺失，先给出明确 blocker，或写一个只处理这些缺失包的 env_setup；不要直接启动 baseline 再让它因缺包失败。",
            "如果需要做 package probe，用 scripts/check_runtime_environment.py 并显式传入从 repo 推断出来的 --require 包名，不要再做泛化的默认探测。",
            "如果启动的是 env_setup 或 data_prep，这只是主任务的前置补救步骤，不是最终回答；要在同一轮里继续读取其结果，并在完成后回到 baseline 主线。",
            "必要时参考 references/runtime-environment.md 和 scripts/render_execution_spec.py 生成 execution_spec 骨架。",
            "execution_spec 里优先保留 README 或官方仓库给出的原始命令和官方 URL；不要为了测活再发明小工具。",
            "如果需要填写 preflight_checks，必须写成对象数组，例如 [{\"name\":\"check_python\",\"required\":true,\"status\":\"passed\"}]；不要写成 {\"check_python\": true} 这种 map。",
            "runtime 会在 start_execution 前自动对 command/external_dependencies 中的官方下载链接做 preflight。",
            "如果 preflight 因 required official external dependency 失败，不要停在“链接失效”。必须进入一次官方来源恢复流程。",
            "恢复顺序固定为：先读 `repo_reference.json`；如果其中给出了 `repo_history_candidates_file`，立刻读取这份历史候选文件，从 commit diff 里的旧官方 URL 中寻找同文件名 candidate；只有历史候选为空、不可用、或全部被 runtime preflight 否掉时，才退到 web_search / web_scrape。",
            "repo history 候选优先于公网搜索，因为它仍然属于官方 repo 证据。不要拉多个版本 repo；只使用当前 repo 的历史候选文件。",
            "当历史候选里已经出现同名 artifact URL 时，不要继续搜索，直接把这个 candidate official fallback 写入新的 execution_spec 并重试 start_execution。",
            "只有在 repo history 没给出候选时，才使用 web_search。先用精确文件名 + repo/project 名；如果只返回当前仓库主页或当前失效 URL，再改成精确文件名 + org/lab 名，或加 site:official-domain。",
            "如果搜索结果含糊但看起来来自当前官方 repo 页面、项目页或同组织页面，就立刻对那个页面做 web_scrape，直接读取当前页面正文和链接，再决定是否重写 execution_spec。",
            "如果 web_search 的官方结果摘要里直接出现可下载 URL，只要文件名完全一致且域名仍然明显属于同一官方组织/实验室，就可以把它当成 candidate official fallback，再写入新的 execution_spec 交给 runtime 重新 preflight。",
            "恢复流程只做一轮。先历史候选，后公网搜索；找到 candidate 就立刻重写 execution_spec 并重试 start_execution。不要连续做宽泛搜索。",
            "如果本轮只是 baseline_repro，并且用户明确要求复现/运行，可以直接启动 baseline。",
            "只有在 baseline 确实被环境阻塞时才创建 env_check / env_setup；如果 repo 已经有数据文件、inspect_runtime 也返回了可用 candidate，就不要降级成环境检查。",
            "如果必须做 environment check，不要手写复杂的 python -c 一行脚本；使用 skills/paper-reproduction/scripts/check_runtime_environment.py 对 runtime-worker 做检查。",
            "当 paper_research_start_execution 返回 running/pending 时，只有真正的 baseline/tuning/compare 训练任务才立刻向用户回报并结束本轮；env_setup 或 data_prep 这类前置补救任务要继续读取结果并回到主线。",
            "写入 execution_spec 后才能启动 execution，并在回答前读回 execution 结果/日志。",
            "读取 execution 结果必须调用 paper_research_read_execution；读取 execution spec 必须调用 paper_research_read_execution_spec；不要用 paper_research_read_artifact 读取 executions/* 或 executions 目录。",
        ],
        "tuning": [
            "请使用 paper-reproduction skill，继续 first_tuning / compare 阶段。",
            "先调用 paper_research_status，然后读取 implementation_spec 与已完成 baseline execution。",
            "先根据 baseline、implementation_spec、repo 证据做现状分析，再从 implementation_spec.tuning_plan 或 experiment_spec.optimization_candidates 中整理 2-4 个 grounded tuning 选项。",
            "先用 paper_research_search_repo 找 CLI/config/notebook 参数入口，再决定读哪些 repo 文件。",
            "优先复用现有 CLI/config/notebook 参数；只有参数写死时，才用 execution_spec.generated_files 生成 execution-scoped variant script，且不要覆盖原 repo 文件。generated_files 每项至少包含 relative_path 和 content。",
            "默认不要直接写 execution_spec 或 start_execution。",
            "先向用户输出：当前 baseline 现状、推荐的 first_tuning、以及每个候选的变更点、依据、风险、预计成本。",
            "只有当用户明确说执行/启动/运行某个 tuning 选项，或 goal 明确包含执行意图时，才写 execution_spec 并 start_execution。",
            "当 execution 进入 running/pending 时，立即回报 execution_id 和状态并结束本轮。",
            "如果 tuning 已完成，再读取 baseline 与 tuning execution，比较共享指标，输出 improved/regressed/inconclusive。",
        ],
    }
    lines = templates.get(stage, [f"Unknown stage: {stage}"])
    return f"{prefix}\n" + "\n".join(lines)


build_prompt = _build_prompt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=["planning", "implementation_prep", "run_drafts", "execution", "tuning"])
    parser.add_argument("--paper-id", type=int, required=True)
    parser.add_argument("--project-id", type=int)
    parser.add_argument("--goal")
    parser.add_argument("--preferred-draft-id")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    prompt = _build_prompt(
        stage=args.stage,
        paper_id=args.paper_id,
        project_id=args.project_id,
        goal=args.goal,
        preferred_draft_id=args.preferred_draft_id,
    )
    if args.as_json:
        print(json.dumps({"stage": args.stage, "prompt": prompt}, ensure_ascii=False))
    else:
        print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
