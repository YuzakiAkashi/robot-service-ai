from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


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


def append_history(history_path: Path, payload: dict[str, Any]) -> None:
    """向 JSONL 历史文件追加一条售后案例处理记录。"""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def is_placeholder_api_key(value: str) -> bool:
    """判断 API Key 是否为空或仍是占位文本。"""
    stripped = value.strip()
    if not stripped:
        return True
    placeholders = {"YOUR_API_KEY_HERE", "your-api-key-here", "替换成你的 API Key"}
    return stripped in placeholders or "替换" in stripped or "你的" in stripped


def is_placeholder_model(value: str) -> bool:
    """判断模型名是否为空或仍是占位文本。"""
    stripped = value.strip()
    if not stripped:
        return True
    placeholders = {"YOUR_MODEL_NAME", "your-model-name", "替换成你的模型名"}
    return (
        stripped in placeholders
        or stripped.startswith("your-")
        or "替换" in stripped
        or "你的" in stripped
    )


def is_placeholder_base_url(value: str) -> bool:
    """判断 API Base URL 是否为空或仍是占位文本。"""
    stripped = value.strip()
    if not stripped:
        return True
    return stripped in {"https://example.com/api/v1", "your-base-url"} or "example.com" in stripped


def load_config_object(path: Path, label: str) -> dict[str, Any]:
    """读取可选 JSON 配置文件，并保证内容是对象。"""
    if not path.exists():
        return {}
    loaded = load_json(path, default={})
    if not isinstance(loaded, dict):
        raise SystemExit(f"{label} 配置文件必须是 JSON 对象: {path}")
    return loaded


def first_config_value(
    file_config: dict[str, Any],
    key: str,
    env_names: tuple[str, ...],
    default: str,
) -> str:
    """按环境变量、配置文件、默认值的优先级读取字符串配置。"""
    for env_name in env_names:
        value = os.getenv(env_name)
        if value:
            return value.strip()
    value = file_config.get(key, default)
    if value is None:
        return default
    return str(value).strip()


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
    """标准化文本，用于大小写不敏感的检索匹配。"""
    return text.lower().replace("\\", "/")


def extract_terms(text: str) -> set[str]:
    """从问题和日志中提取基础可检索词。"""
    lowered = normalize(text)
    terms = set(re.findall(r"[a-z0-9_./:-]{2,}", lowered))
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

