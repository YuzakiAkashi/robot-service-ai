#!/usr/bin/env python3
"""
机器人内部售后 AI 助手 MVP。

用于只读索引机器人项目、审查和分类学生问题、匹配 FAQ、检索项目片段，
并生成可交给代码模型继续排查的项目 Debug 提示。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DOUBAO_DEFAULT_CONFIG = "doubao_config.local.json"
DOUBAO_DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_DEFAULT_MODEL = "doubao-seed-2-0-lite-260215"
KEYWORDS_CONFIG_PATH = Path(__file__).with_name("config") / "keywords.toml"

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


def _string_list(value: Any, name: str) -> list[str]:
    """校验 TOML 配置值是否为非空字符串列表。"""
    if not isinstance(value, list):
        raise SystemExit(f"关键词配置 {name} 必须是字符串数组")
    return [str(item).strip() for item in value if str(item).strip()]


def load_keywords_config(path: Path = KEYWORDS_CONFIG_PATH) -> dict[str, Any]:
    """从 TOML 文件加载并校验本地关键词规则。"""
    if not path.exists():
        raise SystemExit(f"关键词配置文件不存在: {path}")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"关键词 TOML 格式错误: {path} ({exc})") from exc

    expansions = data.get("chinese_expansions", {})
    if not isinstance(expansions, dict):
        raise SystemExit("关键词配置 chinese_expansions 必须是表")

    keywords = data.get("keywords", {})
    if not isinstance(keywords, dict):
        raise SystemExit("关键词配置 keywords 必须是表")

    return {
        "chinese_expansions": {
            str(key): _string_list(value, f"chinese_expansions.{key}")
            for key, value in expansions.items()
        },
        "related": _string_list(keywords.get("related", []), "keywords.related"),
        "simple": _string_list(keywords.get("simple", []), "keywords.simple"),
        "complex": _string_list(keywords.get("complex", []), "keywords.complex"),
        "hardware_risk": _string_list(keywords.get("hardware_risk", []), "keywords.hardware_risk"),
    }


KEYWORD_CONFIG = load_keywords_config()
CHINESE_EXPANSIONS = KEYWORD_CONFIG["chinese_expansions"]
RELATED_KEYWORDS = KEYWORD_CONFIG["related"]
SIMPLE_KEYWORDS = KEYWORD_CONFIG["simple"]
COMPLEX_KEYWORDS = KEYWORD_CONFIG["complex"]
HARDWARE_RISK_KEYWORDS = KEYWORD_CONFIG["hardware_risk"]


def now_iso() -> str:
    """返回当前本地时间的 ISO 字符串，不包含微秒。"""
    return dt.datetime.now().replace(microsecond=0).isoformat()


def read_text_safe(path: Path, max_bytes: int | None = None) -> str:
    """用常见编码安全读取文本文件，读取失败时返回空字符串。"""
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
    """计算文件前 max_bytes 字节的稳定 SHA-1 哈希。"""
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


def load_json(path: Path, default: Any) -> Any:
    """读取 JSON 文件；文件不存在时返回默认值。"""
    if not path.exists():
        return default
    try:
        return json.loads(read_text_safe(path))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 格式错误: {path} ({exc})") from exc


def write_json(path: Path, data: Any) -> None:
    """以 UTF-8 写入 JSON 文件，并按需创建父目录。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def is_placeholder_api_key(value: str) -> bool:
    """判断 API Key 是否为空或仍是占位文本。"""
    stripped = value.strip()
    if not stripped:
        return True
    placeholders = {"YOUR_API_KEY_HERE", "your-api-key-here", "替换成你的 API Key"}
    return stripped in placeholders or "替换" in stripped or "你的" in stripped


def load_llm_config(config_path: str | None = None) -> dict[str, Any]:
    """从本地 JSON 配置和环境变量读取豆包 API 设置。"""
    path = Path(config_path or os.getenv("AFTERSALES_DOUBAO_CONFIG", DOUBAO_DEFAULT_CONFIG))
    file_config: dict[str, Any] = {}
    if path.exists():
        loaded = load_json(path, default={})
        if not isinstance(loaded, dict):
            raise SystemExit(f"豆包配置文件必须是 JSON 对象: {path}")
        file_config = loaded

    api_key = (
        os.getenv("ARK_API_KEY")
        or os.getenv("AFTERSALES_DOUBAO_API_KEY")
        or str(file_config.get("api_key", ""))
    ).strip()
    base_url = (
        os.getenv("AFTERSALES_DOUBAO_BASE_URL")
        or os.getenv("ARK_BASE_URL")
        or str(file_config.get("base_url", DOUBAO_DEFAULT_BASE_URL))
    ).strip()
    model = (
        os.getenv("AFTERSALES_DOUBAO_MODEL")
        or os.getenv("ARK_MODEL")
        or str(file_config.get("model", DOUBAO_DEFAULT_MODEL))
    ).strip()

    return {
        "api_key": api_key,
        "base_url": base_url or DOUBAO_DEFAULT_BASE_URL,
        "model": model or DOUBAO_DEFAULT_MODEL,
        "config_path": str(path),
    }


def ensure_llm_api_key(config: dict[str, Any], purpose: str) -> None:
    """在未配置豆包 API Key 时终止执行，并给出清晰提示。"""
    if is_placeholder_api_key(str(config.get("api_key", ""))):
        raise SystemExit(
            f"缺少豆包 API Key，无法{purpose}。请在 {config.get('config_path')} 填写 api_key，"
            "或设置环境变量 ARK_API_KEY。"
        )


def call_doubao_chat(
    messages: list[dict[str, str]],
    config: dict[str, Any],
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str:
    """调用豆包 OpenAI-compatible Chat API，并返回模型文本内容。"""
    ensure_llm_api_key(config, "调用豆包")
    url = str(config.get("base_url", DOUBAO_DEFAULT_BASE_URL)).rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": config.get("model", DOUBAO_DEFAULT_MODEL),
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise SystemExit(f"调用豆包失败: HTTP {exc.code} {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"调用豆包失败: {exc}") from exc

    data = json.loads(body)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SystemExit(f"豆包返回格式异常: {body[:1000]}") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(content)


def parse_json_object(text: str) -> dict[str, Any]:
    """从模型回复中提取并解析 JSON 对象，兼容代码块格式。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("response does not contain a JSON object")
    return json.loads(stripped[start : end + 1])


def normalize(text: str) -> str:
    """标准化文本，用于大小写不敏感的关键词匹配。"""
    return text.lower().replace("\\", "/")


def extract_terms(text: str) -> set[str]:
    """从问题、日志和关键词扩展中提取可检索词。"""
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


def as_bool(value: Any, default: bool = False) -> bool:
    """将常见布尔值写法转换为 bool，无法识别时返回默认值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "是"}:
            return True
        if lowered in {"false", "no", "0", "否"}:
            return False
    return default


def as_float(value: Any, default: float = 0.0) -> float:
    """将值转换为 0.0 到 1.0 范围内的置信度浮点数。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, number))


def as_string_list(value: Any) -> list[str]:
    """将单值或列表值整理成非空字符串列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(value).strip()]


def normalize_choice(value: Any, allowed: set[str], default: str) -> str:
    """当值属于允许的枚举选项时返回该值，否则返回默认值。"""
    text = str(value or "").strip()
    return text if text in allowed else default


def build_local_review(triage: dict[str, Any]) -> dict[str, Any]:
    """把本地规则分类结果转换成第一层审查结构。"""
    hardware_risk = as_string_list(triage.get("hardware_risk"))
    return {
        "provider": "local_rules",
        "related": bool(triage.get("related")),
        "hardware_risk": bool(hardware_risk),
        "need_human": bool(triage.get("need_human")),
        "hardware_risk_keywords": hardware_risk,
        "reason": "基于本地关键词规则完成审查。",
        "confidence": 0.65,
    }


def build_local_classification(triage: dict[str, Any]) -> dict[str, Any]:
    """把本地规则分类结果转换成第二层分类结构。"""
    return {
        "provider": "local_rules",
        "category": triage.get("category", "out_of_scope"),
        "difficulty": triage.get("difficulty", "ignore_or_manual"),
        "need_project_context": bool(triage.get("need_project_context")),
        "missing_info": as_string_list(triage.get("missing_info")),
        "reason": "基于本地关键词规则完成分类。",
        "confidence": 0.65,
    }


def merge_triage_layers(
    review: dict[str, Any],
    classification: dict[str, Any],
    scores: dict[str, int],
) -> dict[str, Any]:
    """合并第一层审查和第二层分类，生成统一的分流结果。"""
    allowed_categories = {
        "hardware_risk",
        "project_debug",
        "simple_faq",
        "robot_general",
        "out_of_scope",
    }
    allowed_difficulties = {"human_review", "complex", "simple", "medium", "ignore_or_manual"}

    related = as_bool(review.get("related"))
    need_human = as_bool(review.get("need_human"))
    hardware_risk = as_bool(review.get("hardware_risk"))
    hardware_risk_keywords = as_string_list(review.get("hardware_risk_keywords"))

    category = normalize_choice(classification.get("category"), allowed_categories, "out_of_scope")
    difficulty = normalize_choice(classification.get("difficulty"), allowed_difficulties, "ignore_or_manual")
    need_project_context = as_bool(classification.get("need_project_context"))

    if need_human or hardware_risk:
        category = "hardware_risk"
        difficulty = "human_review"
        need_human = True
        need_project_context = False
        related = True
    elif not related:
        category = "out_of_scope"
        difficulty = "ignore_or_manual"
        need_project_context = False

    review_result = {
        "provider": str(review.get("provider", "unknown")),
        "related": related,
        "hardware_risk": hardware_risk,
        "need_human": need_human,
        "hardware_risk_keywords": hardware_risk_keywords,
        "reason": str(review.get("reason", "")).strip(),
        "confidence": as_float(review.get("confidence"), 0.0),
    }
    classification_result = {
        "provider": str(classification.get("provider", "unknown")),
        "category": category,
        "difficulty": difficulty,
        "need_project_context": need_project_context,
        "missing_info": as_string_list(classification.get("missing_info")),
        "reason": str(classification.get("reason", "")).strip(),
        "confidence": as_float(classification.get("confidence"), 0.0),
    }

    return {
        "related": related,
        "category": category,
        "difficulty": difficulty,
        "need_project_context": need_project_context,
        "need_human": need_human,
        "hardware_risk": hardware_risk_keywords,
        "missing_info": classification_result["missing_info"],
        "review": review_result,
        "classification": classification_result,
        "scores": scores,
    }


def classify_question_local(question: str, log_text: str) -> dict[str, Any]:
    """使用本地关键词规则分类学生问题，作为离线兜底方案。"""
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

    triage = {
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
    return merge_triage_layers(
        build_local_review(triage),
        build_local_classification(triage),
        triage["scores"],
    )


def call_doubao_review_layer(
    question: str,
    log_text: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """调用豆包做第一层审查：相关性、硬件风险和是否转人工。"""
    content = call_doubao_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是机器人售后问题的第一层审查器。只判断输入是否属于机器人售后范围，"
                    "以及是否存在硬件安全风险或必须人工介入的情况。只输出 JSON。"
                ),
            },
            {
                "role": "user",
                "content": "\n".join(
                    [
                        "请审查下面的学生问题和日志，返回 JSON：",
                        "{",
                        '  "related": true/false,',
                        '  "hardware_risk": true/false,',
                        '  "need_human": true/false,',
                        '  "hardware_risk_keywords": ["命中的风险词"],',
                        '  "reason": "一句话说明审查依据",',
                        '  "confidence": 0.0到1.0',
                        "}",
                        "",
                        "判定规则：",
                        "- related 只表示是否和机器人/ROS/传感器/底盘/功能包售后相关。",
                        "- 出现冒烟、短路、烧坏、异味、明显过热、电源反接等，hardware_risk 和 need_human 必须为 true。",
                        "- 不要做 FAQ 或 Debug 分类，那是下一层的任务。",
                        "",
                        "学生问题：",
                        question.strip() or "无",
                        "",
                        "学生日志：",
                        log_text.strip() or "无",
                    ]
                ),
            },
        ],
        config,
        temperature=0.0,
        max_tokens=500,
    )
    data = parse_json_object(content)
    return {
        "provider": "doubao",
        "related": as_bool(data.get("related")),
        "hardware_risk": as_bool(data.get("hardware_risk")),
        "need_human": as_bool(data.get("need_human")),
        "hardware_risk_keywords": as_string_list(data.get("hardware_risk_keywords")),
        "reason": str(data.get("reason", "")).strip(),
        "confidence": as_float(data.get("confidence"), 0.0),
    }


def call_doubao_classification_layer(
    question: str,
    log_text: str,
    review: dict[str, Any],
    local_hint: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """在第一层审查后，调用豆包做第二层处理路径分类。"""
    content = call_doubao_chat(
        [
            {
                "role": "system",
                "content": (
                    "你是机器人售后问题的第二层分类器。基于第一层审查结果，"
                    "把问题分到固定处理路径。只输出 JSON，不要输出解释性正文。"
                ),
            },
            {
                "role": "user",
                "content": "\n".join(
                    [
                        "请分类下面的学生问题和日志，返回 JSON：",
                        "{",
                        '  "category": "hardware_risk|project_debug|simple_faq|robot_general|out_of_scope",',
                        '  "difficulty": "human_review|complex|simple|medium|ignore_or_manual",',
                        '  "need_project_context": true/false,',
                        '  "missing_info": ["还需要补充的信息"],',
                        '  "reason": "一句话说明分类依据",',
                        '  "confidence": 0.0到1.0',
                        "}",
                        "",
                        "分类定义：",
                        "- hardware_risk：有硬件安全风险，直接人工处理。",
                        "- project_debug：需要结合项目文件、日志、launch/config/src 等做 Debug。",
                        "- simple_faq：型号、参数、默认配置、账号、基础接线等简单 FAQ 可以处理。",
                        "- robot_general：机器人售后相关，但暂时不确定是否需要项目上下文。",
                        "- out_of_scope：和机器人售后无关。",
                        "",
                        "第一层审查结果：",
                        json.dumps(review, ensure_ascii=False),
                        "",
                        "本地规则提示，仅供参考，若和输入矛盾请以输入为准：",
                        json.dumps(
                            {
                                "category": local_hint.get("category"),
                                "difficulty": local_hint.get("difficulty"),
                                "scores": local_hint.get("scores"),
                                "missing_info": local_hint.get("missing_info"),
                            },
                            ensure_ascii=False,
                        ),
                        "",
                        "学生问题：",
                        question.strip() or "无",
                        "",
                        "学生日志：",
                        log_text.strip() or "无",
                    ]
                ),
            },
        ],
        config,
        temperature=0.0,
        max_tokens=700,
    )
    data = parse_json_object(content)
    return {
        "provider": "doubao",
        "category": data.get("category", "out_of_scope"),
        "difficulty": data.get("difficulty", "ignore_or_manual"),
        "need_project_context": as_bool(data.get("need_project_context")),
        "missing_info": as_string_list(data.get("missing_info")),
        "reason": str(data.get("reason", "")).strip(),
        "confidence": as_float(data.get("confidence"), 0.0),
    }


def classify_question(
    question: str,
    log_text: str,
    llm_config: dict[str, Any] | None = None,
    triage_mode: str = "auto",
) -> dict[str, Any]:
    """按配置运行分流流程，可使用豆包在线分类或本地规则分类。"""
    local_triage = classify_question_local(question, log_text)
    if triage_mode == "local":
        return local_triage

    config = llm_config or load_llm_config()
    has_api_key = not is_placeholder_api_key(str(config.get("api_key", "")))
    if not has_api_key:
        if triage_mode == "doubao":
            ensure_llm_api_key(config, "运行豆包两层审查分类")
        local_triage["online_note"] = f"未找到豆包 API Key，已使用本地规则。配置文件: {config.get('config_path')}"
        return local_triage

    try:
        review = call_doubao_review_layer(question, log_text, config)
        classification = call_doubao_classification_layer(
            question,
            log_text,
            review,
            local_triage,
            config,
        )
        return merge_triage_layers(review, classification, local_triage.get("scores", {}))
    except (SystemExit, ValueError, json.JSONDecodeError) as exc:
        if triage_mode == "doubao":
            raise
        local_triage["online_note"] = f"豆包审查分类失败，已使用本地规则: {exc}"
        return local_triage


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


def score_file_for_query(file_item: dict[str, Any], content: str, terms: set[str]) -> tuple[int, list[str]]:
    """根据检索词给单个索引文件打分，并返回命中的词。"""
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
    """围绕命中检索词的行生成带行号的短文本片段。"""
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
    """为当前问题检索最相关的项目文件和代码片段。"""
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
    """生成发送给项目 Debug 模型层的完整提示词。"""
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
    review = triage.get("review", {})
    classification = triage.get("classification", {})

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
            f"- 提供方：{review.get('provider', 'unknown')}",
            f"- 是否相关：{triage.get('related')}",
            f"- 硬件风险：{bool(triage.get('hardware_risk'))}",
            f"- 需要人工：{triage.get('need_human')}",
            f"- 理由：{review.get('reason', '') or '无'}",
            "",
            "第二层分类：",
            f"- 提供方：{classification.get('provider', 'unknown')}",
            f"- 类别：{triage.get('category')}",
            f"- 难度：{triage.get('difficulty')}",
            f"- 需要项目上下文：{triage.get('need_project_context')}",
            f"- 理由：{classification.get('reason', '') or '无'}",
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
    """渲染一份供售后人员审核的完整 Markdown 诊断报告。"""
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
    review = triage.get("review", {})
    classification = triage.get("classification", {})
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
        f"- 提供方：{review.get('provider', 'unknown')}",
        f"- 是否相关：{triage.get('related')}",
        f"- 是否硬件风险：{bool(triage.get('hardware_risk'))}",
        f"- 是否建议人工介入：{triage.get('need_human')}",
        f"- 理由：{review.get('reason', '') or '无'}",
        "",
        "## 第二层分类",
        f"- 提供方：{classification.get('provider', 'unknown')}",
        f"- 分类：{triage.get('category')}",
        f"- 难度：{triage.get('difficulty')}",
        f"- 是否需要项目上下文：{triage.get('need_project_context')}",
        f"- 理由：{classification.get('reason', '') or '无'}",
        f"- 缺少信息：{missing}",
        "",
        "## 建议处理",
        action_summary,
        "",
        "## 第三层 FAQ 命中",
        faq_summary,
        "",
        "## 第四层项目检索",
        context_summary,
        "",
    ]
    if triage.get("online_note"):
        lines.extend(["## 在线分类备注", str(triage.get("online_note")), ""])
    if triage.get("need_project_context") or contexts:
        lines.extend(
            [
                "## 给第四层模型的诊断提示",
                "````text",
                prompt,
                "````",
            ]
        )
    else:
        lines.extend(
            [
                "## 第四层模型提示",
                "本问题已由上游审查分类和 FAQ 处理，当前不需要进入项目 Debug。",
            ]
        )
    report = "\n".join(lines)

    if llm_answer:
        report += "\n\n## 第四层模型回答\n" + llm_answer.strip()

    return report + "\n"


def suggest_action(
    triage: dict[str, Any],
    faq_hits: list[dict[str, Any]],
    contexts: list[dict[str, Any]],
) -> str:
    """根据分流、FAQ 和项目检索结果生成简短处理建议。"""
    if triage.get("need_human"):
        risks = "、".join(triage.get("hardware_risk", [])) or "硬件风险"
        return f"建议立即转人工处理。原因：检测到 {risks}，先让学生断电，避免继续通电测试。"
    if not triage.get("related"):
        return "问题不像机器人售后范围。建议让售后人员人工确认，或引导学生提供机器人型号、使用场景和报错信息。"
    if triage.get("category") == "simple_faq" and faq_hits:
        answer = str(faq_hits[0].get("answer", "")).strip()
        return f"可以直接走第三层 FAQ 回复，不需要进入项目 Debug。\n\n推荐回复：{answer}"
    if contexts:
        paths = "、".join(item["path"] for item in contexts[:3])
        return f"建议进入第四层项目 Debug。已检索到相关文件：{paths}。把下方诊断提示交给豆包/Codex 类模型即可。"
    return "需要售后人员补充更多信息后再判断，优先补充型号、完整日志和复现步骤。"


def call_openai_compatible(prompt: str, llm_config: dict[str, Any] | None = None) -> str:
    """调用配置好的豆包接口，生成最终模型诊断文本。"""
    config = llm_config or load_llm_config()
    return call_doubao_chat(
        [
            {
                "role": "system",
                "content": "你是谨慎的机器人售后技术助手，只输出有依据的排查建议。",
            },
            {"role": "user", "content": prompt},
        ],
        config,
        temperature=0.2,
    )


def append_history(history_path: Path, payload: dict[str, Any]) -> None:
    """向 JSONL 历史文件追加一条售后案例处理记录。"""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


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

    llm_config = None
    if args.triage_mode != "local" or args.call_llm:
        llm_config = load_llm_config(args.llm_config)
    triage = classify_question(
        question,
        log_text,
        llm_config=llm_config,
        triage_mode=args.triage_mode,
    )
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
        if llm_config is None:
            llm_config = load_llm_config(args.llm_config)
        llm_answer = call_openai_compatible(prompt, llm_config)

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
    ask_parser.add_argument("--top-k", type=int, default=8, help="第四层检索文件数量")
    ask_parser.add_argument("--out", help="输出 Markdown 报告路径")
    ask_parser.add_argument("--history", help="追加记录到 JSONL 历史文件")
    ask_parser.add_argument("--llm-config", default=DOUBAO_DEFAULT_CONFIG, help="豆包本地配置 JSON 路径")
    ask_parser.add_argument(
        "--triage-mode",
        choices=["auto", "doubao", "local"],
        default="auto",
        help="两层审查分类模式；auto 有豆包 Key 就调用豆包，否则走本地规则",
    )
    ask_parser.add_argument("--call-llm", action="store_true", help="调用豆包生成第四层诊断回答")
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
