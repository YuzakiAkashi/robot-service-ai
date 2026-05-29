#!/usr/bin/env python3
"""
机器人内部售后 AI 助手 MVP。

用于只读索引机器人项目、审查和分类学生问题、匹配 FAQ，
并生成可交给代码模型继续排查的项目 Debug 提示。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from support_llm import (
    CODEX_BIN_ENV_NAME,
    CODEX_DEFAULT_TIMEOUT_SECONDS,
    LLM_DEFAULT_CONFIG,
    call_codex_cli,
    call_llm_classification_layer,
    call_llm_review_layer,
    first_layer_passed,
    layer_llm_config,
    load_llm_configs,
    make_codex_debug_prompt,
    make_debug_prompt,
)
from support_utils import (
    append_history,
    as_bool,
    extract_terms,
    load_json,
    normalize,
    now_iso,
    read_text_safe,
    stable_hash,
    write_json,
)


SCHEMA_VERSION = 1

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "build",
    "devel",
    "install",
    "log",
    "logs",
    "bag",
    "bags",
    "rosbag",
    "rosbags",
    "dist",
    "target",
}

TEXT_EXTENSIONS = {
    "",
    ".action",
    ".bash",
    ".c",
    ".cc",
    ".cfg",
    ".cmake",
    ".conf",
    ".cpp",
    ".csv",
    ".h",
    ".hpp",
    ".ini",
    ".json",
    ".launch",
    ".log",
    ".lua",
    ".md",
    ".msg",
    ".param",
    ".py",
    ".rviz",
    ".service",
    ".sh",
    ".srv",
    ".toml",
    ".txt",
    ".urdf",
    ".xacro",
    ".xml",
    ".yaml",
    ".yml",
}

IMPORTANT_NAMES = {
    "readme.md",
    "package.xml",
    "cmakelists.txt",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "manifest.xml",
}


def is_text_file(path: Path) -> bool:
    """判断文件是否应作为可读文本纳入索引。"""
    if path.name in {"Dockerfile", "Makefile"}:
        return True
    return path.suffix.lower() in TEXT_EXTENSIONS


def file_kind(rel_path: str) -> str:
    """根据相对路径粗略判断索引文件的项目类型。"""
    rel = rel_path.replace("\\", "/").lower()
    name = Path(rel).name
    suffix = Path(rel).suffix
    if name == "package.xml":
        return "ros_package_manifest"
    if name == "cmakelists.txt":
        return "build_config"
    if "/launch/" in f"/{rel}" or suffix == ".launch":
        return "ros_launch"
    if "/config/" in f"/{rel}" or suffix in {".yaml", ".yml", ".param"}:
        return "config"
    if suffix in {".urdf", ".xacro"}:
        return "robot_description"
    if suffix in {".msg", ".srv", ".action"}:
        return "ros_interface"
    if suffix in {".py", ".cpp", ".hpp", ".h", ".c", ".cc"}:
        return "source_code"
    if suffix in {".md", ".txt"}:
        return "document"
    if suffix in {".log"}:
        return "log"
    return "text"


def file_priority(rel_path: str) -> int:
    """计算文件在项目级检索中的优先级分数。"""
    rel = rel_path.replace("\\", "/").lower()
    name = Path(rel).name
    score = 0
    if name in IMPORTANT_NAMES:
        score += 30
    if "/launch/" in f"/{rel}":
        score += 25
    if "/config/" in f"/{rel}":
        score += 20
    if "/src/" in f"/{rel}" or "/scripts/" in f"/{rel}":
        score += 10
    if name.endswith((".launch", ".yaml", ".yml", ".urdf", ".xacro")):
        score += 10
    return score


def should_skip_dir(path: Path) -> bool:
    """判断相对目录路径是否包含需要忽略的目录名。"""
    parts = {part.lower() for part in path.parts}
    return bool(parts & IGNORED_DIRS)


def build_index(project_root: Path, name: str | None, max_file_bytes: int) -> dict[str, Any]:
    """扫描项目目录，生成只读的可检索文件索引。"""
    root = project_root.resolve()
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"项目路径不存在或不是文件夹: {root}")

    files: list[dict[str, Any]] = []
    skipped_large = 0
    skipped_binary = 0

    for path in root.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        if should_skip_dir(rel.parent):
            continue
        if not is_text_file(path):
            skipped_binary += 1
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_file_bytes:
            skipped_large += 1
            continue
        rel_text = rel.as_posix()
        files.append(
            {
                "path": rel_text,
                "size": size,
                "mtime": int(path.stat().st_mtime),
                "kind": file_kind(rel_text),
                "priority": file_priority(rel_text),
                "sha1": stable_hash(path),
            }
        )

    files.sort(key=lambda item: (-item["priority"], item["path"]))
    counts: dict[str, int] = {}
    for item in files:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1

    important = [item["path"] for item in files if item["priority"] >= 20][:30]
    return {
        "schema_version": SCHEMA_VERSION,
        "project_name": name or root.name,
        "project_root": str(root),
        "created_at": now_iso(),
        "index_options": {"max_file_bytes": max_file_bytes},
        "overview": {
            "file_count": len(files),
            "kind_counts": counts,
            "important_files": important,
            "skipped_large": skipped_large,
            "skipped_binary": skipped_binary,
        },
        "files": files,
    }


def match_faqs(
    question: str,
    log_text: str,
    faqs: list[dict[str, Any]],
    limit: int = 3,
    min_score: int = 5,
) -> list[dict[str, Any]]:
    """根据问题和日志给 FAQ 条目打分，返回最匹配的答案。"""
    combined = normalize(f"{question}\n{log_text}")
    q_terms = extract_terms(combined)
    hits: list[dict[str, Any]] = []

    for entry in faqs:
        score = 0
        keywords = [str(item) for item in entry.get("keywords", [])]
        for keyword in keywords:
            if normalize(keyword) in combined or keyword in question:
                score += 8
        entry_text = normalize(
            " ".join(
                [
                    str(entry.get("title", "")),
                    str(entry.get("question", "")),
                    " ".join(keywords),
                    str(entry.get("answer", "")),
                ]
            )
        )
        overlap = q_terms & extract_terms(entry_text)
        score += min(len(overlap), 8)
        if score >= min_score:
            copy = dict(entry)
            copy["score"] = score
            copy["matched_terms"] = sorted(overlap)[:12]
            hits.append(copy)

    hits.sort(key=lambda item: (-item["score"], item.get("id", "")))
    return hits[:limit]


def should_enter_fourth_layer(
    triage: dict[str, Any],
    faq_hits: list[dict[str, Any]],
) -> bool:
    """根据分流结果决定是否进入第四层模型。"""
    review = triage.get("review", {})
    if not first_layer_passed(review):
        return False
    category = str(triage.get("category", "out_of_scope"))
    if category in {"hardware_risk", "out_of_scope"}:
        return False
    if category == "simple_faq" and faq_hits:
        return False
    return (
        category in {"project_debug", "robot_general"}
        or as_bool(triage.get("need_project_context"))
        or not faq_hits
    )


def format_review_section(review: dict[str, Any]) -> str:
    """格式化第一层审查报告段，命令行即时输出和完整报告共用。"""
    return "\n".join(
        [
            "## 第一层审查",
            f"- 提供方：{review.get('provider', 'unknown')}",
            f"- 是否通过：{first_layer_passed(review)}",
            f"- 是否相关：{review.get('related')}",
            f"- 理由：{review.get('reason', '') or '无'}",
        ]
    )


def render_faq_section(faq_hits: list[dict[str, Any]]) -> str:
    """渲染给第四层模型使用的 FAQ 命中详情。"""
    faq_section = "无 FAQ 命中。"
    if faq_hits:
        faq_lines = []
        for hit in faq_hits:
            faq_lines.append(
                f"- {hit.get('title') or hit.get('id')}: {hit.get('answer', '')}"
            )
        faq_section = "\n".join(faq_lines)
    return faq_section


def suggest_action(
    triage: dict[str, Any],
    faq_hits: list[dict[str, Any]],
) -> str:
    """根据分流和 FAQ 结果生成简短处理建议。"""
    if triage.get("need_human"):
        risks = "、".join(triage.get("hardware_risk", [])) or "硬件风险"
        return f"建议立即转人工处理。原因：检测到 {risks}，先让学生断电，避免继续通电测试。"
    if not triage.get("related"):
        return "问题不像机器人售后范围。建议让售后人员人工确认，或引导学生提供机器人型号、使用场景和报错信息。"
    if triage.get("category") == "simple_faq" and faq_hits:
        answer = str(faq_hits[0].get("answer", "")).strip()
        return f"可以直接走第三层 FAQ 回复，不需要进入项目 Debug。\n\n推荐回复：{answer}"
    if as_bool(
        triage.get("enter_fourth_layer"),
        should_enter_fourth_layer(triage, faq_hits),
    ):
        return "建议进入第四层项目 Debug。当前流程不再预检索代码片段；使用 Codex CLI 时会让 Codex 在只读项目目录中自行检索。"
    return "需要售后人员补充更多信息后再判断，优先补充型号、完整日志和复现步骤。"


def command_index(args: argparse.Namespace) -> None:
    """CLI 的 index 子命令：建立并保存项目索引。"""
    index_data = build_index(Path(args.project), args.name, args.max_file_bytes)
    out_path = Path(args.out)
    write_json(out_path, index_data)
    print(f"已建立项目索引: {out_path}")
    print(f"项目: {index_data['project_name']}")
    print(f"文件数: {index_data['overview']['file_count']}")
    print(f"重要文件: {len(index_data['overview']['important_files'])}")


def command_inspect(args: argparse.Namespace) -> None:
    """CLI 的 inspect 子命令：打印已保存索引的概况。"""
    index_data = load_json(Path(args.index), default=None)
    if not index_data:
        raise SystemExit(f"索引不存在: {args.index}")
    overview = index_data.get("overview", {})
    print(f"项目: {index_data.get('project_name')}")
    print(f"路径: {index_data.get('project_root')}")
    print(f"索引时间: {index_data.get('created_at')}")
    print(f"文件数: {overview.get('file_count')}")
    print("文件类型:")
    for kind, count in sorted(overview.get("kind_counts", {}).items()):
        print(f"  - {kind}: {count}")
    print("重要文件:")
    for item in overview.get("important_files", [])[:20]:
        print(f"  - {item}")


def command_ask(args: argparse.Namespace) -> None:
    """CLI 的 ask 子命令：分流问题并生成诊断报告。"""
    index_data = load_json(Path(args.index), default=None)
    if not index_data:
        raise SystemExit(f"索引不存在: {args.index}")
    if index_data.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit("索引版本不兼容，请重新运行 index。")

    question = args.question or ""
    if args.question_file:
        question = read_text_safe(Path(args.question_file), max_bytes=args.max_question_bytes)
    question = question.strip()
    if not question:
        raise SystemExit("请通过 --question 或 --question-file 提供学生问题。")

    log_text = ""
    if args.log_file:
        log_text = read_text_safe(Path(args.log_file), max_bytes=args.max_log_bytes)
    if args.log_text:
        log_text = f"{log_text}\n{args.log_text}".strip()

    faqs = load_json(Path(args.faq), default=[]) if args.faq else []
    if not isinstance(faqs, list):
        raise SystemExit("FAQ 文件必须是 JSON 数组。")

    project_name = index_data.get("project_name", "unknown")
    generated_at = now_iso()
    report_parts = [
        "\n".join(
            [
                "# AI售后 ",
                "",
                f"生成时间：{generated_at}",
                f"项目：{project_name}",
                "",
                "## 学生问题",
                question.strip(),
            ]
        )
    ]
    print(report_parts[-1], end="\n\n", flush=True)

    llm_config = load_llm_configs(args.llm_config)                              #加载LLM配置
    review_config = layer_llm_config(llm_config, "review")
    classification_config = layer_llm_config(llm_config, "classification")
    print("## 运行进度\n- 正在进行第一层审查...\n", flush=True)
    try:
        review = call_llm_review_layer(question, log_text, review_config)       #第一层：审查层
    except (SystemExit, ValueError) as exc:
        if args.triage_mode == "llm":
            raise
        raise SystemExit(f"模型服务审查分类失败: {exc}") from exc

    review_section = format_review_section(review)
    report_parts.append(review_section)
    print(review_section, end="\n\n", flush=True)

    if not first_layer_passed(review):                                          #第一层不通过：输出结果后退出
        report = "\n\n".join(report_parts) + "\n"
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(report, encoding="utf-8")
            print(f"已生成报告: {out_path}")
        raise SystemExit(0)

    print("## 运行进度\n- 正在进行第二层分类...\n", flush=True)
    try:                                            
        classification = call_llm_classification_layer(                         #第二层：分类层
            question,
            log_text,
            review,
            classification_config,
        )
    except (SystemExit, ValueError) as exc:
        if args.triage_mode == "llm":
            raise
        raise SystemExit(f"模型服务审查分类失败: {exc}") from exc

    category = str(classification.get("category") or "out_of_scope")
    hardware_risk = as_bool(classification.get("hardware_risk")) or category == "hardware_risk"
    difficulty = str(classification.get("difficulty") or "ignore_or_manual")
    need_project_context = as_bool(classification.get("need_project_context"))
    need_human = as_bool(classification.get("need_human")) or hardware_risk
    hardware_risk_keywords = list(classification.get("hardware_risk_keywords") or [])
    if hardware_risk:
        category = "hardware_risk"
        difficulty = "human_review"
        need_project_context = False
        hardware_risk_keywords = hardware_risk_keywords or ["硬件风险"]

    missing_info = list(classification.get("missing_info") or [])
    missing_info_text = "、".join(missing_info) or "无"
    triage = {
        "related": True,
        "category": category,
        "difficulty": difficulty,
        "need_project_context": need_project_context,
        "need_human": need_human,
        "hardware_risk": hardware_risk_keywords,
        "missing_info": missing_info,
        "review": review,
        "classification": classification,
        "review_passed": True,
    }

    classification_section = "\n".join(
        [
            "## 第二层分类",
            f"- 提供方：{classification.get('provider', 'unknown')}",
            f"- 分类：{triage.get('category')}",
            f"- 难度：{triage.get('difficulty')}",
            f"- 是否硬件风险：{bool(triage.get('hardware_risk'))}",
            f"- 是否建议人工介入：{triage.get('need_human')}",
            f"- 是否需要项目上下文：{triage.get('need_project_context')}",
            f"- 理由：{classification.get('reason', '') or '无'}",
            f"- 缺少信息：{missing_info_text}",
        ]
    )
    report_parts.append(classification_section)
    print(classification_section, end="\n\n", flush=True)

    print("## 运行进度\n- 正在匹配第三层 FAQ...\n", flush=True)
    faq_hits = []
    faq_hits = match_faqs(question, log_text, faqs)                             #第三层：FAQ
    triage["enter_fourth_layer"] = should_enter_fourth_layer(triage, faq_hits)

    faq_summary = "无"
    if faq_hits:
        lines = []
        for hit in faq_hits:
            lines.append(f"- {hit.get('title') or hit.get('id')} (score={hit.get('score')})")
        faq_summary = "\n".join(lines)

    action_section = "\n".join(["## 建议处理", suggest_action(triage, faq_hits)])
    report_parts.append(action_section)
    print(action_section, end="\n\n", flush=True)

    faq_report_section = "\n".join(["## 第三层 FAQ 命中", faq_summary])
    report_parts.append(faq_report_section)
    print(faq_report_section, end="\n\n", flush=True)

    if triage.get("online_note"):
        online_section = "\n".join(["## 在线分类备注", str(triage.get("online_note"))])
        report_parts.append(online_section)
        print(online_section, end="\n\n", flush=True)

    llm_answer = None
    if triage["enter_fourth_layer"]:
        prompt_factory = make_codex_debug_prompt if args.call_codex else make_debug_prompt
        prompt = prompt_factory(
            project_name,
            question,
            log_text,
            triage,
            render_faq_section(faq_hits),
        )
        prompt_section = "\n".join(
            [
                "## 给第四层模型的诊断提示",
                "````text",
                prompt,
                "````",
            ]
        )
        report_parts.append(prompt_section)
        print(prompt_section, end="\n\n", flush=True)

        if args.call_codex:
            print("## 运行进度\n- 正在调用 Codex CLI 第四层 Debug...\n", flush=True)
            llm_answer = call_codex_cli(                                #第四层：CodexCLI
                prompt,
                cwd=Path(index_data["project_root"]),
                codex_bin=args.codex_bin,
                timeout_seconds=args.codex_timeout,
            )
        if llm_answer:
            answer_section = "## 第四层模型回答\n" + llm_answer.strip()
            report_parts.append(answer_section)
    else:
        fourth_section = "\n".join(
            [
                "## 第四层模型提示",
                "当前未进入第四层模型；第一层未通过、FAQ 已覆盖，或第三层没有检索到可用项目上下文。",
            ]
        )
        report_parts.append(fourth_section)
        print(fourth_section, end="\n\n", flush=True)

    report = "\n\n".join(report_parts) + "\n"

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"已生成报告: {out_path}")

    if args.history:
        append_history(
            Path(args.history),
            {
                "created_at": now_iso(),
                "question": question,
                "triage": triage,
                "faq_hits": [
                    {"id": hit.get("id"), "title": hit.get("title"), "score": hit.get("score")}
                    for hit in faq_hits
                ],
                "enter_fourth_layer": triage.get("enter_fourth_layer"),
                "report_path": args.out,
            },
        )


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器并注册子命令。"""
    parser = argparse.ArgumentParser(
        description="机器人内部售后 AI 助手 MVP",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="为机器人功能包建立只读索引")
    index_parser.add_argument("--project", required=True, help="项目/功能包文件夹路径")
    index_parser.add_argument("--out", required=True, help="输出索引 JSON 路径")
    index_parser.add_argument("--name", help="项目显示名称")
    index_parser.add_argument("--max-file-bytes", type=int, default=200_000, help="单文件读取上限")
    index_parser.set_defaults(func=command_index)

    inspect_parser = subparsers.add_parser("inspect", help="查看索引概况")
    inspect_parser.add_argument("--index", required=True, help="索引 JSON 路径")
    inspect_parser.set_defaults(func=command_inspect)

    ask_parser = subparsers.add_parser("ask", help="输入学生问题，生成售后诊断报告")
    ask_parser.add_argument("--index", required=True, help="索引 JSON 路径")
    ask_parser.add_argument("--question", help="学生问题。Windows 中文参数乱码时建议使用 --question-file")
    ask_parser.add_argument("--question-file", help="UTF-8 文本文件，内容为学生问题")
    ask_parser.add_argument("--max-question-bytes", type=int, default=20_000, help="学生问题读取上限")
    ask_parser.add_argument("--faq", default="data/faqs.json", help="FAQ JSON 文件")
    ask_parser.add_argument("--log-file", help="学生日志文件")
    ask_parser.add_argument("--log-text", help="直接传入日志文本")
    ask_parser.add_argument("--max-log-bytes", type=int, default=120_000, help="日志读取上限")
    ask_parser.add_argument("--out", help="输出 Markdown 报告路径")
    ask_parser.add_argument("--history", help="追加记录到 JSONL 历史文件")
    ask_parser.add_argument("--llm-config", default=LLM_DEFAULT_CONFIG, help="AI 服务本地配置 JSON 路径")
    ask_parser.add_argument(
        "--triage-mode",
        choices=["auto", "llm"],
        default="auto",
        help="两层审查分类模式；本地关键词分流已移除，auto 和 llm 都需要模型服务 Key",
    )
    ask_parser.add_argument("--call-codex", action="store_true", help="调用本地 Codex CLI 生成第四层诊断回答")
    ask_parser.add_argument(
        "--codex-bin",
        help=f"Codex CLI 可执行文件路径；默认读取 {CODEX_BIN_ENV_NAME}，否则使用 PATH 中的 codex",
    )
    ask_parser.add_argument(
        "--codex-timeout",
        type=int,
        default=CODEX_DEFAULT_TIMEOUT_SECONDS,
        help="Codex CLI 调用超时时间（秒）",
    )
    ask_parser.set_defaults(func=command_ask)

    return parser


def main(argv: list[str] | None = None) -> int:
    """解析命令行参数，分发到对应命令，并返回退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
