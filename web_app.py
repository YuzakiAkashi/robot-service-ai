#!/usr/bin/env python3
"""Minimal local web UI for the after-sales assistant."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import codecs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
TMP_DIR = ROOT / "reports" / "_web_tmp"


def decode_process_output(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def cli_python_executable() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        python_exe = executable.with_name("python.exe")
        if python_exe.exists():
            return str(python_exe)
    return sys.executable


HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI售后助手</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1f2933;
      --muted: #697586;
      --line: #d9dee7;
      --accent: #2563eb;
      --danger: #b42318;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Microsoft YaHei", Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    h1 {
      margin: 0;
      font-size: 20px;
      font-weight: 700;
      letter-spacing: 0;
    }
    main {
      display: grid;
      grid-template-columns: 390px 1fr;
      min-height: calc(100vh - 61px);
    }
    form, .result {
      padding: 20px;
    }
    form {
      border-right: 1px solid var(--line);
      background: var(--panel);
    }
    label {
      display: block;
      margin: 0 0 14px;
      font-size: 13px;
      font-weight: 600;
    }
    input, textarea {
      width: 100%;
      margin-top: 6px;
      padding: 10px 11px;
      border: 1px solid var(--line);
      border-radius: 6px;
      font: inherit;
      font-size: 14px;
      background: #fff;
      color: var(--text);
    }
    textarea {
      min-height: 118px;
      resize: vertical;
      line-height: 1.5;
    }
    .row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 14px 0 18px;
      color: var(--muted);
      font-size: 13px;
    }
    .row input {
      width: auto;
      margin: 0;
    }
    button {
      width: 100%;
      border: 0;
      border-radius: 6px;
      padding: 11px 14px;
      background: var(--accent);
      color: white;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }
    button:disabled {
      opacity: .65;
      cursor: wait;
    }
    .result {
      overflow: auto;
    }
    .status {
      margin-bottom: 12px;
      color: var(--muted);
      font-size: 13px;
    }
    pre {
      min-height: 420px;
      margin: 0;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.55;
      font-family: Consolas, "Microsoft YaHei", monospace;
      font-size: 13px;
    }
    .error { color: var(--danger); }
    @media (max-width: 820px) {
      main { grid-template-columns: 1fr; }
      form { border-right: 0; border-bottom: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header><h1>AI售后助手</h1></header>
  <main>
    <form id="askForm">
      <label>索引文件
        <input name="index" value="indexes/mini_robot.json">
      </label>
      <label>FAQ 文件
        <input name="faq" value="data/faqs.json">
      </label>
      <label>学生问题
        <textarea name="question" required placeholder="粘贴学生原始问题"></textarea>
      </label>
      <label>日志
        <textarea name="log_text" placeholder="可选：粘贴终端日志"></textarea>
      </label>
      <label>输出报告路径
        <input name="out" value="reports/web_last.md">
      </label>
      <div class="row">
        <input id="callCodex" name="call_codex" type="checkbox">
        <label for="callCodex" style="margin:0;font-weight:400;">调用 Codex CLI 生成第四层回答</label>
      </div>
      <button id="submitBtn" type="submit">开始分析</button>
    </form>
    <section class="result">
      <div id="status" class="status">等待输入</div>
      <pre id="output">结果会显示在这里。</pre>
    </section>
  </main>
  <script>
    const form = document.getElementById('askForm');
    const output = document.getElementById('output');
    const status = document.getElementById('status');
    const submitBtn = document.getElementById('submitBtn');

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submitBtn.disabled = true;
      status.textContent = '正在分析...';
      status.className = 'status';
      output.textContent = '';

      const data = Object.fromEntries(new FormData(form).entries());
      data.call_codex = document.getElementById('callCodex').checked;

      try {
        const response = await fetch('/api/ask', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(data)
        });
        if (!response.body) {
          throw new Error('浏览器不支持流式响应');
        }
        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let fullText = '';
        while (true) {
          const {value, done} = await reader.read();
          if (done) break;
          const text = decoder.decode(value, {stream: true});
          fullText += text;
          output.textContent += text;
          output.scrollTop = output.scrollHeight;
        }
        const tail = decoder.decode();
        if (tail) {
          fullText += tail;
          output.textContent += tail;
        }
        if (response.ok && !fullText.includes('[前端] 执行失败')) {
          status.textContent = '完成';
        } else {
          status.textContent = '执行失败';
          status.className = 'status error';
        }
      } catch (err) {
        status.textContent = '请求失败';
        status.className = 'status error';
        output.textContent = String(err);
      } finally {
        submitBtn.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if urlparse(self.path).path != "/":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML.encode("utf-8"))

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/ask":
            self.send_error(404)
            return
        try:
            payload = self._read_json()
            self._stream_ask(payload)
        except Exception as exc:
            self._send_text(str(exc), 500)

    def _read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _build_ask_command(self, payload: dict[str, object]) -> tuple[list[str], str]:
        question = str(payload.get("question", "")).strip()
        if not question:
            raise ValueError("学生问题不能为空")

        TMP_DIR.mkdir(parents=True, exist_ok=True)
        question_path = TMP_DIR / "question.txt"
        log_path = TMP_DIR / "log.txt"
        question_path.write_text(question, encoding="utf-8")
        log_path.write_text(str(payload.get("log_text", "")).strip(), encoding="utf-8")

        out_path = str(payload.get("out", "reports/web_last.md")).strip() or "reports/web_last.md"
        command = [
            cli_python_executable(),
            "-u",
            str(ROOT / "support_ai.py"),
            "ask",
            "--index",
            str(payload.get("index", "indexes/mini_robot.json")).strip(),
            "--faq",
            str(payload.get("faq", "data/faqs.json")).strip(),
            "--question-file",
            str(question_path),
            "--log-file",
            str(log_path),
            "--out",
            out_path,
        ]
        if bool(payload.get("call_codex")):
            command.append("--call-codex")
        return command, out_path

    def _stream_ask(self, payload: dict[str, object]) -> None:
        command, out_path = self._build_ask_command(payload)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        if process.stdout is not None:
            while True:
                chunk = process.stdout.read(1)
                if not chunk:
                    break
                self._write_text(decoder.decode(chunk))
        tail = decoder.decode(b"", final=True)
        if tail:
            self._write_text(tail)
        returncode = process.wait()
        if returncode == 0:
            self._write_text(f"\n[前端] 执行完成，报告已保存：{out_path}\n")
        else:
            self._write_text(f"\n[前端] 执行失败，退出码 {returncode}\n")

    def _write_text(self, text: str) -> None:
        if not text:
            return
        self.wfile.write(text.encode("utf-8", errors="replace"))
        self.wfile.flush()

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8", errors="replace")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 8000), Handler)
    if sys.stdout:
        print("前端已启动: http://127.0.0.1:8000")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
