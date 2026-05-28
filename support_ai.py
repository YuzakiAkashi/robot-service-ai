#!/usr/bin/env python3
"""
Internal after-sales assistant MVP.

It indexes a robot project in read-only mode, triages student questions,
matches simple FAQ answers, retrieves relevant project snippets, and generates
a third-layer debug prompt for a code-capable LLM.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


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

CHINESE_EXPANSIONS = {
    "雷达": ["lidar", "laser", "scan", "/scan", "rplidar", "ydlidar", "hokuyo"],
    "激光": ["lidar", "laser", "scan", "/scan"],
    "摄像头": ["camera", "image", "usb_cam", "realsense", "opencv"],
    "相机": ["camera", "image", "usb_cam", "realsense", "opencv"],
    "串口": ["serial", "ttyusb", "ttyacm", "baudrate", "port"],
    "端口": ["serial", "ttyusb", "ttyacm", "port"],
    "波特率": ["baudrate", "baud", "serial"],
    "底盘": ["base", "chassis", "cmd_vel", "odom", "motor", "driver"],
    "电机": ["motor", "driver", "base", "cmd_vel"],
    "里程计": ["odom", "odometry", "encoder"],
    "编译": ["build", "catkin", "colcon", "cmake", "make", "cmakelists", "package.xml"],
    "启动": ["launch", "roslaunch", "node", "rosrun"],
    "节点": ["node", "rosnode", "launch"],
    "话题": ["topic", "rostopic", "publisher", "subscriber"],
    "没有数据": ["no data", "topic", "publish", "subscribe", "driver"],
    "权限": ["permission", "denied", "dialout", "chmod", "udev"],
    "连接": ["connect", "network", "wifi", "ip", "serial"],
    "导航": ["navigation", "nav", "move_base", "map", "amcl"],
    "建图": ["slam", "gmapping", "cartographer", "map"],
    "模型": ["model", "type", "version"],
    "型号": ["model", "type", "version"],
    "遥控": ["remote", "joystick", "joy", "teleop"],
}

RELATED_KEYWORDS = [
    "机器人",
    "小车",
    "ros",
    "ros2",
    "launch",
    "topic",
    "节点",
    "雷达",
    "摄像头",
    "串口",
    "底盘",
    "电机",
    "编译",
    "报错",
    "日志",
    "urdf",
    "tf",
    "imu",
    "里程计",
    "导航",
    "建图",
    "gazebo",
]

SIMPLE_KEYWORDS = [
    "型号",
    "参数",
    "默认",
    "是多少",
    "接口",
    "位置",
    "电池",
    "电压",
    "尺寸",
    "重量",
    "ip",
    "wifi",
    "账号",
    "密码",
    "波特率",
]

COMPLEX_KEYWORDS = [
    "报错",
    "error",
    "exception",
    "traceback",
    "failed",
    "undefined reference",
    "permission denied",
    "catkin",
    "colcon",
    "cmake",
    "make",
    "编译",
    "启动失败",
    "没有数据",
    "打不开",
    "连不上",
    "topic",
    "roslaunch",
    "源码",
    "代码",
    "debug",
    "崩溃",
    "segmentation fault",
]

HARDWARE_RISK_KEYWORDS = [
    "冒烟",
    "烧",
    "烧坏",
    "短路",
    "发烫",
    "过热",
    "异味",
    "电源反接",
]


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat()


def read_text_safe(path: Path, max_bytes: int | None = None) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if max_bytes is not None and len(data) > max_bytes:
        data = data[:max_bytes]
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def stable_hash(path: Path, max_bytes: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    try:
        with path.open("rb") as fh:
            remaining = max_bytes
            while remaining > 0:
                chunk = fh.read(min(65536, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def is_text_file(path: Path) -> bool:
    if path.name in {"Dockerfile", "Makefile"}:
        return True
    return path.suffix.lower() in TEXT_EXTENSIONS


def file_kind(rel_path: str) -> str:
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
    parts = {part.lower() for part in path.parts}
    return bool(parts & IGNORED_DIRS)


def build_index(project_root: Path, name: str | None, max_file_bytes: int) -> dict[str, Any]:
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


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(read_text_safe(path))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 格式错误: {path} ({exc})") from exc


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize(text: str) -> str:
    return text.lower().replace("\\", "/")


def extract_terms(text: str) -> set[str]:
    lowered = normalize(text)
    terms = set(re.findall(r"[a-z0-9_./:-]{2,}", lowered))
    for cn, expansions in CHINESE_EXPANSIONS.items():
        if cn in text:
            terms.add(cn)
            terms.update(expansions)
    for keyword in RELATED_KEYWORDS + SIMPLE_KEYWORDS + COMPLEX_KEYWORDS:
        if keyword in lowered or keyword in text:
            terms.add(keyword.lower())
    return {term for term in terms if len(term) >= 2}


def classify_question(question: str, log_text: str) -> dict[str, Any]:
    combined = f"{question}\n{log_text}"
    lowered = normalize(combined)

    related_score = 0
    for keyword in RELATED_KEYWORDS:
        if keyword.lower() in lowered or keyword in combined:
            related_score += 1

    simple_score = 0
    for keyword in SIMPLE_KEYWORDS:
        if keyword.lower() in lowered or keyword in combined:
            simple_score += 1

    complex_score = 0
    for keyword in COMPLEX_KEYWORDS:
        if keyword.lower() in lowered or keyword in combined:
            complex_score += 1
    if len(log_text.strip()) > 80:
        complex_score += 2
    if "traceback" in lowered or "[error]" in lowered or "error:" in lowered:
        complex_score += 2

    hardware_risk = [kw for kw in HARDWARE_RISK_KEYWORDS if kw in combined]

    if hardware_risk:
        category = "hardware_risk"
        difficulty = "human_review"
    elif complex_score >= 2:
        category = "project_debug"
        difficulty = "complex"
    elif simple_score >= 1:
        category = "simple_faq"
        difficulty = "simple"
    elif related_score >= 1:
        category = "robot_general"
        difficulty = "medium"
    else:
        category = "out_of_scope"
        difficulty = "ignore_or_manual"

    missing_info: list[str] = []
    if category in {"project_debug", "robot_general"}:
        if not any(word in combined for word in ["型号", "model", "版本", "package", "功能包"]):
            missing_info.append("机器人型号或功能包版本")
        if category == "project_debug" and not log_text.strip() and "报错" in combined:
            missing_info.append("完整终端报错日志")
        if any(word in combined for word in ["串口", "tty", "雷达", "底盘"]) and "lsusb" not in lowered:
            missing_info.append("设备识别信息，例如 lsusb、dmesg 或 /dev/ttyUSB*")

    return {
        "related": category != "out_of_scope",
        "category": category,
        "difficulty": difficulty,
        "need_project_context": category in {"project_debug", "robot_general"},
        "need_human": category == "hardware_risk",
        "hardware_risk": hardware_risk,
        "missing_info": missing_info,
        "scores": {
            "related": related_score,
            "simple": simple_score,
            "complex": complex_score,
        },
    }


def match_faqs(
    question: str,
    log_text: str,
    faqs: list[dict[str, Any]],
    limit: int = 3,
    min_score: int = 5,
) -> list[dict[str, Any]]:
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


def score_file_for_query(file_item: dict[str, Any], content: str, terms: set[str]) -> tuple[int, list[str]]:
    rel = normalize(file_item["path"])
    content_l = normalize(content)
    score = int(file_item.get("priority", 0))
    matched: list[str] = []

    for term in terms:
        term_l = normalize(term)
        if term_l in rel:
            score += 20
            matched.append(term)
        count = content_l.count(term_l)
        if count:
            score += min(count, 10) * 3
            matched.append(term)

    return score, sorted(set(matched))


def make_snippets(content: str, terms: set[str], max_snippets: int = 3, context_lines: int = 3) -> list[str]:
    lines = content.splitlines()
    lowered_lines = [normalize(line) for line in lines]
    lowered_terms = [normalize(term) for term in terms if len(term) >= 2]
    anchors: list[int] = []

    for index, line in enumerate(lowered_lines):
        if any(term in line for term in lowered_terms):
            anchors.append(index)
            if len(anchors) >= max_snippets:
                break

    if not anchors and lines:
        anchors = [0]

    snippets: list[str] = []
    used_ranges: list[range] = []
    for anchor in anchors:
        start = max(0, anchor - context_lines)
        end = min(len(lines), anchor + context_lines + 1)
        current_range = range(start, end)
        if any(set(current_range) & set(existing) for existing in used_ranges):
            continue
        used_ranges.append(current_range)
        numbered = [f"{line_no + 1}: {lines[line_no]}" for line_no in range(start, end)]
        snippet = "\n".join(numbered)
        if len(snippet) > 4000:
            snippet = snippet[:4000] + "\n...<truncated>"
        snippets.append(snippet)
    return snippets


def retrieve_project_context(
    index_data: dict[str, Any],
    question: str,
    log_text: str,
    top_k: int,
) -> list[dict[str, Any]]:
    root = Path(index_data["project_root"])
    terms = extract_terms(f"{question}\n{log_text}")
    if not terms:
        terms = {"launch", "config", "readme", "package.xml", "cmakelists"}

    candidates: list[dict[str, Any]] = []
    max_bytes = int(index_data.get("index_options", {}).get("max_file_bytes", 200_000))

    for file_item in index_data.get("files", []):
        path = root / file_item["path"]
        if not path.exists():
            continue
        content = read_text_safe(path, max_bytes=max_bytes)
        score, matched = score_file_for_query(file_item, content, terms)
        if score <= 0:
            continue
        if matched or file_item.get("priority", 0) >= 25:
            candidates.append(
                {
                    "path": file_item["path"],
                    "kind": file_item.get("kind", "text"),
                    "score": score,
                    "matched_terms": matched[:16],
                    "snippets": make_snippets(content, terms),
                }
            )

    candidates.sort(key=lambda item: (-item["score"], item["path"]))
    return candidates[:top_k]


def make_debug_prompt(
    project_name: str,
    question: str,
    log_text: str,
    triage: dict[str, Any],
    faq_hits: list[dict[str, Any]],
    contexts: list[dict[str, Any]],
) -> str:
    faq_section = "无 FAQ 命中。"
    if faq_hits:
        faq_lines = []
        for hit in faq_hits:
            faq_lines.append(
                f"- {hit.get('title') or hit.get('id')}: {hit.get('answer', '')}"
            )
        faq_section = "\n".join(faq_lines)

    context_blocks = []
    for item in contexts:
        snippet_text = "\n\n".join(item.get("snippets", []))
        context_blocks.append(
            f"### {item['path']} ({item['kind']}, score={item['score']})\n"
            f"Matched terms: {', '.join(item.get('matched_terms', [])) or 'none'}\n"
            "```text\n"
            f"{snippet_text}\n"
            "```"
        )
    context_section = "\n\n".join(context_blocks) or "没有检索到相关项目片段。"

    log_section = log_text.strip() or "学生没有提供日志。"
    missing = "、".join(triage.get("missing_info", [])) or "无"

    return "\n".join(
        [
            "你是机器人售后工程师，正在辅助内部售后人员回复学生。",
            "",
            "工作规则：",
            "1. 只根据学生问题、日志、FAQ 和项目片段给出判断；没有依据就说明需要补充信息。",
            "2. 不要要求学生修改源码，除非项目片段和日志能明确支撑这个建议。",
            "3. 先排查连接、权限、参数、启动顺序、依赖和硬件状态，再考虑代码 bug。",
            "4. 遇到电源短路、烧坏、冒烟、异常发热，建议立即断电并转人工。",
            "5. 输出要适合售后人员审核后直接发给学生。",
            "",
            f"项目名称：{project_name}",
            "",
            "第一层审查：",
            f"- 类别：{triage.get('category')}",
            f"- 难度：{triage.get('difficulty')}",
            f"- 需要人工：{triage.get('need_human')}",
            f"- 缺少信息：{missing}",
            "",
            "学生问题：",
            question.strip(),
            "",
            "学生日志：",
            "```text",
            log_section,
            "```",
            "",
            "FAQ 命中：",
            faq_section,
            "",
            "项目相关片段：",
            context_section,
            "",
            "请按下面格式输出：",
            "## 初步结论",
            "用 1-3 句话说明最可能原因和置信度。",
            "",
            "## 依据",
            "列出你引用的日志或项目文件依据。",
            "",
            "## 排查步骤",
            "给学生可以按顺序执行的步骤，优先使用安全、可逆、低风险动作。",
            "",
            "## 需要补充的信息",
            "如果信息不足，列出最多 5 项。",
            "",
            "## 推荐回复",
            "写一段售后人员可以直接发给学生的中文回复。",
        ]
    ).strip()


def make_report(
    index_data: dict[str, Any],
    question: str,
    log_text: str,
    triage: dict[str, Any],
    faq_hits: list[dict[str, Any]],
    contexts: list[dict[str, Any]],
    llm_answer: str | None = None,
) -> str:
    project_name = index_data.get("project_name", "unknown")
    prompt = make_debug_prompt(project_name, question, log_text, triage, faq_hits, contexts)

    faq_summary = "无"
    if faq_hits:
        lines = []
        for hit in faq_hits:
            lines.append(f"- {hit.get('title') or hit.get('id')} (score={hit.get('score')})")
        faq_summary = "\n".join(lines)

    context_summary = "无"
    if contexts:
        context_summary = "\n".join(
            f"- `{item['path']}` ({item['kind']}, score={item['score']})"
            for item in contexts
        )

    missing = "、".join(triage.get("missing_info", [])) or "无"
    action_summary = suggest_action(triage, faq_hits, contexts)
    lines = [
        "# 售后问题诊断报告",
        "",
        f"生成时间：{now_iso()}",
        f"项目：{project_name}",
        "",
        "## 学生问题",
        question.strip(),
        "",
        "## 第一层审查",
        f"- 是否相关：{triage.get('related')}",
        f"- 分类：{triage.get('category')}",
        f"- 难度：{triage.get('difficulty')}",
        f"- 是否需要项目上下文：{triage.get('need_project_context')}",
        f"- 是否建议人工介入：{triage.get('need_human')}",
        f"- 缺少信息：{missing}",
        "",
        "## 建议处理",
        action_summary,
        "",
        "## 第二层 FAQ 命中",
        faq_summary,
        "",
        "## 第三层项目检索",
        context_summary,
        "",
    ]
    if triage.get("need_project_context") or contexts:
        lines.extend(
            [
                "## 给第三层模型的诊断提示",
                "````text",
                prompt,
                "````",
            ]
        )
    else:
        lines.extend(
            [
                "## 第三层模型提示",
                "本问题已由第一层和第二层处理，当前不需要进入项目 Debug。",
            ]
        )
    report = "\n".join(lines)

    if llm_answer:
        report += "\n\n## 第三层模型回答\n" + llm_answer.strip()

    return report + "\n"


def suggest_action(
    triage: dict[str, Any],
    faq_hits: list[dict[str, Any]],
    contexts: list[dict[str, Any]],
) -> str:
    if triage.get("need_human"):
        risks = "、".join(triage.get("hardware_risk", [])) or "硬件风险"
        return f"建议立即转人工处理。原因：检测到 {risks}，先让学生断电，避免继续通电测试。"
    if not triage.get("related"):
        return "问题不像机器人售后范围。建议让售后人员人工确认，或引导学生提供机器人型号、使用场景和报错信息。"
    if triage.get("category") == "simple_faq" and faq_hits:
        answer = str(faq_hits[0].get("answer", "")).strip()
        return f"可以直接走第二层 FAQ 回复，不需要进入项目 Debug。\n\n推荐回复：{answer}"
    if contexts:
        paths = "、".join(item["path"] for item in contexts[:3])
        return f"建议进入第三层项目 Debug。已检索到相关文件：{paths}。把下方诊断提示交给 DeepSeek/Codex 类模型即可。"
    return "需要售后人员补充更多信息后再判断，优先补充型号、完整日志和复现步骤。"


def call_openai_compatible(prompt: str) -> str:
    api_key = os.getenv("AFTERSALES_LLM_API_KEY")
    base_url = os.getenv("AFTERSALES_LLM_BASE_URL", "https://api.deepseek.com")
    model = os.getenv("AFTERSALES_LLM_MODEL", "deepseek-chat")
    if not api_key:
        raise SystemExit("缺少环境变量 AFTERSALES_LLM_API_KEY，未调用线上模型。")

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是谨慎的机器人售后技术助手，只输出有依据的排查建议。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise SystemExit(f"调用模型失败: {exc}") from exc

    data = json.loads(body)
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SystemExit(f"模型返回格式异常: {body[:1000]}") from exc


def append_history(history_path: Path, payload: dict[str, Any]) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def command_index(args: argparse.Namespace) -> None:
    index_data = build_index(Path(args.project), args.name, args.max_file_bytes)
    out_path = Path(args.out)
    write_json(out_path, index_data)
    print(f"已建立项目索引: {out_path}")
    print(f"项目: {index_data['project_name']}")
    print(f"文件数: {index_data['overview']['file_count']}")
    print(f"重要文件: {len(index_data['overview']['important_files'])}")


def command_inspect(args: argparse.Namespace) -> None:
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

    triage = classify_question(question, log_text)
    faq_hits = match_faqs(question, log_text, faqs)

    need_context = triage.get("need_project_context") or not faq_hits
    contexts = []
    if need_context:
        contexts = retrieve_project_context(
            index_data=index_data,
            question=question,
            log_text=log_text,
            top_k=args.top_k,
        )

    llm_answer = None
    prompt = make_debug_prompt(
        index_data.get("project_name", "unknown"),
        question,
        log_text,
        triage,
        faq_hits,
        contexts,
    )
    if args.call_llm:
        llm_answer = call_openai_compatible(prompt)

    report = make_report(index_data, question, log_text, triage, faq_hits, contexts, llm_answer)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"已生成报告: {out_path}")
    else:
        print(report)

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
                "context_paths": [item["path"] for item in contexts],
                "report_path": args.out,
            },
        )


def build_parser() -> argparse.ArgumentParser:
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
    ask_parser.add_argument("--top-k", type=int, default=8, help="第三层检索文件数量")
    ask_parser.add_argument("--out", help="输出 Markdown 报告路径")
    ask_parser.add_argument("--history", help="追加记录到 JSONL 历史文件")
    ask_parser.add_argument("--call-llm", action="store_true", help="调用 OpenAI-compatible 模型接口")
    ask_parser.set_defaults(func=command_ask)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
