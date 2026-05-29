from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from support_utils import (
    as_bool,
    as_float,
    as_string_list,
    first_config_value,
    is_placeholder_api_key,
    is_placeholder_base_url,
    is_placeholder_model,
    load_config_object,
    parse_json_object,
    read_text_safe,
)


LLM_DEFAULT_CONFIG = "ai_config.local.json"
LLM_DEFAULT_BASE_URL = "https://example.com/api/v1"
LLM_DEFAULT_MODEL = "your-model-name"
CODEX_BIN_ENV_NAME = "CODEX_BIN"
CODEX_DEFAULT_BIN = "codex"
CODEX_SANDBOX_MODE = "read-only"
CODEX_DEFAULT_TIMEOUT_SECONDS = 600
PROMPT_TEMPLATE_PATH = Path(__file__).resolve().parent / "prompts" / "ai_prompts.md"
LLM_API_KEY_ENV_NAMES = ("AFTERSALES_AI_API_KEY", "OPENAI_API_KEY")
LLM_BASE_URL_ENV_NAMES = ("AFTERSALES_AI_BASE_URL", "OPENAI_BASE_URL")
LLM_MODEL_ENV_NAMES = ("AFTERSALES_AI_MODEL", "OPENAI_MODEL")
LLM_LAYER_ENV_NAMES = {
    "review": {
        "api_key": ("AFTERSALES_REVIEW_AI_API_KEY",) + LLM_API_KEY_ENV_NAMES,
        "base_url": ("AFTERSALES_REVIEW_AI_BASE_URL",) + LLM_BASE_URL_ENV_NAMES,
        "model": ("AFTERSALES_REVIEW_AI_MODEL",) + LLM_MODEL_ENV_NAMES,
    },
    "classification": {
        "api_key": ("AFTERSALES_CLASSIFICATION_AI_API_KEY",) + LLM_API_KEY_ENV_NAMES,
        "base_url": ("AFTERSALES_CLASSIFICATION_AI_BASE_URL",) + LLM_BASE_URL_ENV_NAMES,
        "model": ("AFTERSALES_CLASSIFICATION_AI_MODEL",) + LLM_MODEL_ENV_NAMES,
    },
}

_PROMPT_TEMPLATE_CACHE: dict[str, str] | None = None


def load_prompt_templates() -> dict[str, str]:
    """读取 Markdown 提示词模板文件中的命名块。"""
    global _PROMPT_TEMPLATE_CACHE
    if _PROMPT_TEMPLATE_CACHE is not None:
        return _PROMPT_TEMPLATE_CACHE

    text = read_text_safe(PROMPT_TEMPLATE_PATH)
    if not text:
        raise SystemExit(f"提示词模板文件不存在或为空: {PROMPT_TEMPLATE_PATH}")

    pattern = re.compile(
        r"<!--\s*prompt:([a-zA-Z0-9_-]+)\s*-->\s*(.*?)\s*<!--\s*/prompt\s*-->",
        re.DOTALL,
    )
    templates = {name: body.strip() for name, body in pattern.findall(text)}
    if not templates:
        raise SystemExit(f"提示词模板文件没有可用模板块: {PROMPT_TEMPLATE_PATH}")
    _PROMPT_TEMPLATE_CACHE = templates
    return templates


def prompt_template(name: str) -> str:
    """按名称读取单个提示词模板。"""
    templates = load_prompt_templates()
    try:
        return templates[name]
    except KeyError as exc:
        raise SystemExit(f"提示词模板缺失: {name}") from exc


def render_prompt_template(name: str, values: dict[str, Any] | None = None) -> str:
    """用 {{name}} 占位符渲染提示词模板。"""
    template = prompt_template(name)
    values = values or {}

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        if key not in values:
            raise SystemExit(f"提示词模板 {name} 缺少变量: {key}")
        return str(values[key])

    return re.sub(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}", replace, template).strip()


def layer_file_config(file_config: dict[str, Any], layer: str | None) -> dict[str, Any]:
    """合并顶层配置和指定层级配置；层级配置优先。"""
    merged = {
        "api_key": file_config.get("api_key", ""),
        "base_url": file_config.get("base_url", LLM_DEFAULT_BASE_URL),
        "model": file_config.get("model", LLM_DEFAULT_MODEL),
    }
    if not layer:
        return merged
    layer_config = file_config.get(layer, {})
    if layer_config is None:
        return merged
    if not isinstance(layer_config, dict):
        raise SystemExit(f"AI 配置中的 {layer} 必须是 JSON 对象")
    merged.update({key: value for key, value in layer_config.items() if value is not None})
    return merged


def load_llm_config(config_path: str | None = None, layer: str | None = None) -> dict[str, Any]:
    """从本地 JSON 配置和环境变量读取某一层的模型服务 API 设置。"""
    path = Path(config_path or os.getenv("AFTERSALES_AI_CONFIG", LLM_DEFAULT_CONFIG))
    file_config = load_config_object(path, "AI")
    layer_config = layer_file_config(file_config, layer)
    env_names = LLM_LAYER_ENV_NAMES.get(
        layer or "",
        {
            "api_key": LLM_API_KEY_ENV_NAMES,
            "base_url": LLM_BASE_URL_ENV_NAMES,
            "model": LLM_MODEL_ENV_NAMES,
        },
    )

    return {
        "api_key": first_config_value(layer_config, "api_key", env_names["api_key"], ""),
        "base_url": first_config_value(
            layer_config,
            "base_url",
            env_names["base_url"],
            LLM_DEFAULT_BASE_URL,
        ),
        "model": first_config_value(layer_config, "model", env_names["model"], LLM_DEFAULT_MODEL),
        "config_path": str(path),
        "layer": layer or "default",
    }


def load_llm_configs(config_path: str | None = None) -> dict[str, dict[str, Any]]:
    """读取第一层和第二层的模型配置。"""
    return {
        "review": load_llm_config(config_path, "review"),
        "classification": load_llm_config(config_path, "classification"),
    }


def ensure_llm_config(config: dict[str, Any], purpose: str) -> None:
    """在未配置模型服务关键参数时终止执行，并给出清晰提示。"""
    if is_placeholder_api_key(str(config.get("api_key", ""))):
        raise SystemExit(
            f"缺少模型服务 API Key，无法{purpose}。请在 {config.get('config_path')} 填写 api_key，"
            "或设置环境变量 AFTERSALES_AI_API_KEY / OPENAI_API_KEY。"
        )
    if is_placeholder_model(str(config.get("model", ""))):
        raise SystemExit(
            f"缺少模型名称，无法{purpose}。请在 {config.get('config_path')} 填写 model，"
            "或设置环境变量 AFTERSALES_AI_MODEL / OPENAI_MODEL。"
        )
    if is_placeholder_base_url(str(config.get("base_url", ""))):
        raise SystemExit(
            f"缺少 API Base URL，无法{purpose}。请在 {config.get('config_path')} 填写 base_url，"
            "或设置环境变量 AFTERSALES_AI_BASE_URL / OPENAI_BASE_URL。"
        )


def llm_provider_name(config: dict[str, Any]) -> str:
    """返回报告中展示的模型名称。"""
    model = str(config.get("model", "")).strip()
    return model or "unknown"


def layer_llm_config(configs: dict[str, Any], layer: str) -> dict[str, Any]:
    """兼容单模型配置和分层模型配置。"""
    layer_config = configs.get(layer)
    if isinstance(layer_config, dict):
        return layer_config
    return configs


def call_llm_chat(
    messages: list[dict[str, str]],
    config: dict[str, Any],
    *,
    temperature: float = 0.2,
    max_tokens: int | None = None,
) -> str:
    """调用 OpenAI-compatible Chat API，并返回模型文本内容。"""
    ensure_llm_config(config, "调用模型服务")
    url = str(config.get("base_url", LLM_DEFAULT_BASE_URL)).rstrip("/") + "/chat/completions"
    payload: dict[str, Any] = {
        "model": config.get("model", LLM_DEFAULT_MODEL),
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
        raise SystemExit(f"调用模型服务失败: HTTP {exc.code} {detail[:1000]}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"调用模型服务失败: {exc}") from exc

    data = json.loads(body)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise SystemExit(f"模型服务返回格式异常: {body[:1000]}") from exc
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


def first_layer_passed(review: dict[str, Any]) -> bool:
    """判断第一层审查是否允许问题继续进入后续层级。"""
    return as_bool(review.get("related"))


def call_llm_review_layer(
    question: str,
    log_text: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """调用便宜模型做第一层相关性审查。"""
    content = call_llm_chat(
        [
            {
                "role": "system",
                "content": prompt_template("review_system"),
            },
            {
                "role": "user",
                "content": render_prompt_template(
                    "review_user",
                    {
                        "question": question.strip() or "无",
                        "log_text": log_text.strip() or "无",
                    },
                ),
            },
        ],
        config,
        temperature=0.0,
        max_tokens=500,
    )
    data = parse_json_object(content)
    return {
        "provider": llm_provider_name(config),
        "related": as_bool(data.get("related")),
        "reason": str(data.get("reason", "")).strip(),
        "confidence": as_float(data.get("confidence"), 0.0),
    }


def call_llm_classification_layer(
    question: str,
    log_text: str,
    review: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """第一层通过后，调用模型服务做第二层处理路径分类。"""
    content = call_llm_chat(
        [
            {
                "role": "system",
                "content": prompt_template("classification_system"),
            },
            {
                "role": "user",
                "content": render_prompt_template(
                    "classification_user",
                    {
                        "review_json": json.dumps(review, ensure_ascii=False),
                        "question": question.strip() or "无",
                        "log_text": log_text.strip() or "无",
                    },
                ),
            },
        ],
        config,
        temperature=0.0,
        max_tokens=700,
    )
    data = parse_json_object(content)
    return {
        "provider": llm_provider_name(config),
        "category": data.get("category", "out_of_scope"),
        "difficulty": data.get("difficulty", "ignore_or_manual"),
        "hardware_risk": as_bool(data.get("hardware_risk")),
        "need_human": as_bool(data.get("need_human")),
        "hardware_risk_keywords": as_string_list(data.get("hardware_risk_keywords")),
        "need_project_context": as_bool(data.get("need_project_context")),
        "missing_info": as_string_list(data.get("missing_info")),
        "reason": str(data.get("reason", "")).strip(),
        "confidence": as_float(data.get("confidence"), 0.0),
    }


def make_debug_prompt(
    project_name: str,
    question: str,
    log_text: str,
    triage: dict[str, Any],
    faq_section: str,
    debug_rule_1: str | None = None,
    project_context_note: str | None = None,
) -> str:
    """生成发送给项目 Debug 模型层的完整提示词。"""
    log_section = log_text.strip() or "学生没有提供日志。"
    missing = "、".join(triage.get("missing_info", [])) or "无"
    review = triage.get("review", {})
    classification = triage.get("classification", {})

    return render_prompt_template(
        "debug_prompt",
        {
            "debug_rule_1": debug_rule_1 or prompt_template("debug_rule_standard"),
            "project_name": project_name,
            "review_provider": review.get("provider", "unknown"),
            "review_passed": triage.get("review_passed"),
            "related": triage.get("related"),
            "review_reason": review.get("reason", "") or "无",
            "classification_provider": classification.get("provider", "unknown"),
            "category": triage.get("category"),
            "difficulty": triage.get("difficulty"),
            "hardware_risk": bool(triage.get("hardware_risk")),
            "need_human": triage.get("need_human"),
            "need_project_context": triage.get("need_project_context"),
            "classification_reason": classification.get("reason", "") or "无",
            "missing_info": missing,
            "question": question.strip(),
            "log_text": log_section,
            "faq_section": faq_section,
            "project_context_note": project_context_note
            or "当前流程不再预检索项目代码片段；如果没有项目文件依据，请明确说明需要补充日志、文件名或代码上下文。",
        },
    )


def make_codex_debug_prompt(
    project_name: str,
    question: str,
    log_text: str,
    triage: dict[str, Any],
    faq_section: str,
) -> str:
    """生成给本地 Codex CLI 的提示词，允许它在只读沙盒中自行检索项目文件。"""
    base_prompt = make_debug_prompt(
        project_name,
        question,
        log_text,
        triage,
        faq_section,
        debug_rule_1=prompt_template("debug_rule_codex"),
        project_context_note="当前流程不再预检索项目代码片段；你需要在只读项目目录中自行搜索相关文件。",
    )
    return f"{prompt_template('codex_debug_prefix')}\n\n{base_prompt}"


def resolve_codex_bin(codex_bin: str | None = None) -> str:
    """按命令行参数、环境变量、PATH 默认值解析 Codex CLI 可执行文件。"""
    resolved = (codex_bin or os.getenv(CODEX_BIN_ENV_NAME) or CODEX_DEFAULT_BIN).strip()
    return resolved or CODEX_DEFAULT_BIN


def call_codex_cli(
    prompt: str,
    *,
    cwd: Path,
    codex_bin: str | None = None,
    timeout_seconds: int = CODEX_DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """通过本地 Codex CLI 的非交互模式生成第四层诊断文本。"""
    if timeout_seconds <= 0:
        raise SystemExit("--codex-timeout 必须大于 0。")
    if not cwd.exists() or not cwd.is_dir():
        raise SystemExit(f"Codex CLI 工作目录不存在或不是文件夹: {cwd}")

    resolved_bin = resolve_codex_bin(codex_bin)
    output_path: Path | None = None

    def cleanup_output() -> None:
        if output_path:
            try:
                output_path.unlink()
            except OSError:
                pass

    try:
        with tempfile.NamedTemporaryFile(prefix="support_ai_codex_", suffix=".md", delete=False) as fh:
            output_path = Path(fh.name)

        command = [
            resolved_bin,
            "exec",
            "--sandbox",
            CODEX_SANDBOX_MODE,
            "-o",
            str(output_path),
            "-",
        ]
        print("## Codex CLI 实时输出", flush=True)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
        )

        captured_output: list[str] = []

        def stream_process_output() -> None:
            if process.stdout is None:
                return
            while True:
                chunk = process.stdout.read(1)
                if not chunk:
                    break
                captured_output.append(chunk)
                print(chunk, end="", flush=True)

        output_thread = threading.Thread(target=stream_process_output, daemon=True)
        output_thread.start()
        try:
            if process.stdin is not None:
                process.stdin.write(prompt)
                process.stdin.close()
        except OSError:
            pass
        returncode = process.wait(timeout=timeout_seconds)
        output_thread.join(timeout=5)
        print("\n## Codex CLI 输出结束", flush=True)
    except FileNotFoundError as exc:
        cleanup_output()
        raise SystemExit(
            f"找不到 Codex CLI: {resolved_bin}。请设置 {CODEX_BIN_ENV_NAME} 或传入 --codex-bin。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        try:
            process.kill()
        except OSError:
            pass
        cleanup_output()
        raise SystemExit(
            f"调用 Codex CLI 超时（{timeout_seconds} 秒），可增大 --codex-timeout。"
        ) from exc
    except OSError as exc:
        cleanup_output()
        raise SystemExit(f"调用 Codex CLI 失败: {exc}") from exc

    stdout = "".join(captured_output).strip()
    if returncode != 0:
        detail = stdout or f"exit code {returncode}"
        cleanup_output()
        raise SystemExit(f"调用 Codex CLI 失败: {detail[:2000]}")

    answer = ""
    if output_path and output_path.exists():
        answer = read_text_safe(output_path).strip()
    cleanup_output()
    return answer or stdout
