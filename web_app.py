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

from support_ai import WEB_FRONTEND_BEGIN_MARKER, WEB_FRONTEND_END_MARKER


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
    .markdown-output {
      min-height: 420px;
      margin: 0;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      overflow: auto;
      word-break: break-word;
      line-height: 1.55;
      font-size: 13px;
    }
    .markdown-output h1,
    .markdown-output h2,
    .markdown-output h3,
    .markdown-output h4 {
      margin: 16px 0 8px;
      line-height: 1.35;
      letter-spacing: 0;
    }
    .markdown-output h1 { font-size: 22px; }
    .markdown-output h2 { font-size: 18px; }
    .markdown-output h3 { font-size: 15px; }
    .markdown-output h4 { font-size: 14px; }
    .markdown-output p {
      margin: 8px 0;
    }
    .markdown-output ul,
    .markdown-output ol {
      margin: 8px 0 8px 22px;
      padding: 0;
    }
    .markdown-output li {
      margin: 4px 0;
    }
    .markdown-output pre {
      margin: 10px 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #f8fafc;
      overflow-x: auto;
      white-space: pre;
      font-family: Consolas, "Microsoft YaHei", monospace;
      font-size: 12px;
    }
    .markdown-output code {
      padding: 1px 4px;
      border-radius: 4px;
      background: #eef2f7;
      font-family: Consolas, "Microsoft YaHei", monospace;
      font-size: .94em;
    }
    .markdown-output pre code {
      padding: 0;
      background: transparent;
      border-radius: 0;
      font-size: inherit;
    }
    .markdown-output blockquote {
      margin: 10px 0;
      padding: 6px 12px;
      border-left: 3px solid var(--line);
      color: var(--muted);
      background: #fafbfc;
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
      <div class="row">
        <input id="saveQuestion" name="save_question" type="checkbox" checked>
        <label for="saveQuestion" style="margin:0;font-weight:400;">保存问题到 Cookie</label>
      </div>
      <label>日志
        <textarea name="log_text" placeholder="可选：粘贴终端日志"></textarea>
      </label>
      <label>输出报告路径
        <input name="out" value="reports/web_last.md">
      </label>
      <button id="submitBtn" type="submit">开始分析</button>
    </form>
    <section class="result">
      <div id="status" class="status">等待输入</div>
      <div id="output" class="markdown-output">结果会显示在这里。</div>
    </section>
  </main>
  <script>
    const form = document.getElementById('askForm');
    const output = document.getElementById('output');
    const status = document.getElementById('status');
    const submitBtn = document.getElementById('submitBtn');
    const saveQuestion = document.getElementById('saveQuestion');
    const savedFieldNames = ['index', 'faq', 'question', 'log_text', 'out'];

    function getCookie(name) {
      const prefix = `${name}=`;
      return document.cookie
        .split(';')
        .map(item => item.trim())
        .find(item => item.startsWith(prefix))
        ?.slice(prefix.length) || '';
    }

    function setCookie(name, value, days) {
      const expires = new Date(Date.now() + days * 864e5).toUTCString();
      document.cookie = `${name}=${encodeURIComponent(value)}; expires=${expires}; path=/; SameSite=Lax`;
    }

    function deleteCookie(name) {
      document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/; SameSite=Lax`;
    }

    function escapeHtml(text) {
      return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function renderInline(text) {
      return escapeHtml(text)
        .replace(/`([^`]+)`/g, '<code>$1</code>')
        .replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
    }

    function renderMarkdown(text) {
      const lines = String(text || '').replace(/\\r\\n/g, '\\n').split('\\n');
      const html = [];
      let inCode = false;
      let codeLines = [];
      let listType = '';

      function closeList() {
        if (listType) {
          html.push(`</${listType}>`);
          listType = '';
        }
      }

      function openList(type) {
        if (listType !== type) {
          closeList();
          html.push(`<${type}>`);
          listType = type;
        }
      }

      for (const line of lines) {
        if (line.trim().startsWith('```')) {
          if (inCode) {
            html.push(`<pre><code>${escapeHtml(codeLines.join('\\n'))}</code></pre>`);
            codeLines = [];
            inCode = false;
          } else {
            closeList();
            inCode = true;
          }
          continue;
        }

        if (inCode) {
          codeLines.push(line);
          continue;
        }

        if (!line.trim()) {
          closeList();
          continue;
        }

        const heading = line.match(/^(#{1,4})\\s+(.+)$/);
        if (heading) {
          closeList();
          const level = heading[1].length;
          html.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
          continue;
        }

        const unordered = line.match(/^\\s*[-*]\\s+(.+)$/);
        if (unordered) {
          openList('ul');
          html.push(`<li>${renderInline(unordered[1])}</li>`);
          continue;
        }

        const ordered = line.match(/^\\s*\\d+\\.\\s+(.+)$/);
        if (ordered) {
          openList('ol');
          html.push(`<li>${renderInline(ordered[1])}</li>`);
          continue;
        }

        const quote = line.match(/^>\\s?(.+)$/);
        if (quote) {
          closeList();
          html.push(`<blockquote>${renderInline(quote[1])}</blockquote>`);
          continue;
        }

        closeList();
        html.push(`<p>${renderInline(line)}</p>`);
      }

      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeLines.join('\\n'))}</code></pre>`);
      }
      closeList();
      return html.join('');
    }

    const savedForm = getCookie('support_ai_form');
    if (savedForm) {
      try {
        const values = JSON.parse(decodeURIComponent(savedForm));
        savedFieldNames.forEach((name) => {
          if (values[name] !== undefined && form.elements[name]) {
            form.elements[name].value = values[name];
          }
        });
      } catch (err) {
        deleteCookie('support_ai_form');
      }
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submitBtn.disabled = true;
      status.textContent = '正在分析... 前两层输出在终端，第三/第四层显示在这里。';
      status.className = 'status';
      output.textContent = '等待第三层/第四层输出...';

      const data = Object.fromEntries(new FormData(form).entries());
      if (saveQuestion.checked) {
        const values = {};
        savedFieldNames.forEach((name) => {
          values[name] = data[name] || '';
        });
        setCookie('support_ai_form', JSON.stringify(values), 30);
      } else {
        deleteCookie('support_ai_form');
      }

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
        let paintPending = false;
        let lastPaint = 0;

        function paintPlain(force = false) {
          const now = performance.now();
          if (!force && now - lastPaint < 120) return;
          lastPaint = now;
          output.textContent = fullText;
          output.scrollTop = output.scrollHeight;
        }

        function schedulePaint() {
          if (paintPending) return;
          paintPending = true;
          requestAnimationFrame(() => {
            paintPlain();
            paintPending = false;
          });
        }

        while (true) {
          const {value, done} = await reader.read();
          if (done) break;
          const text = decoder.decode(value, {stream: true});
          fullText += text;
          schedulePaint();
        }
        const tail = decoder.decode();
        if (tail) {
          fullText += tail;
        }
        paintPlain(true);
        if (fullText.trim()) {
          output.innerHTML = renderMarkdown(fullText);
        } else {
          output.textContent = '本次没有第三层/第四层输出，其他内容请看终端。';
        }
        output.scrollTop = output.scrollHeight;
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
            "--call-codex",
            "--web-stream-split",
        ]
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
        line_buffer = ""
        in_frontend = False

        def write_routed(text: str) -> None:
            if in_frontend:
                self._write_text(text)
            else:
                self._write_terminal(text)

        def route_line(line: str) -> None:
            nonlocal in_frontend
            stripped = line.strip()
            if stripped == WEB_FRONTEND_BEGIN_MARKER:
                in_frontend = True
                return
            if stripped == WEB_FRONTEND_END_MARKER:
                in_frontend = False
                return
            write_routed(line)

        def route_output(text: str) -> None:
            nonlocal line_buffer
            if not text:
                return
            line_buffer += text
            while "\n" in line_buffer:
                line, line_buffer = line_buffer.split("\n", 1)
                route_line(line + "\n")

        if process.stdout is not None:
            fd = process.stdout.fileno()
            while True:
                chunk = os.read(fd, 512)
                if not chunk:
                    break
                route_output(decoder.decode(chunk))
        tail = decoder.decode(b"", final=True)
        if tail:
            route_output(tail)
        if line_buffer:
            route_line(line_buffer)
        returncode = process.wait()
        if returncode == 0:
            self._write_terminal(f"\n[web_app] 执行完成，报告已保存：{out_path}\n")
        else:
            self._write_terminal(f"\n[web_app] 执行失败，退出码 {returncode}\n")

    def _write_text(self, text: str) -> None:
        if not text:
            return
        self.wfile.write(text.encode("utf-8", errors="replace"))
        self.wfile.flush()

    def _write_terminal(self, text: str) -> None:
        if not text or sys.stdout is None:
            return
        sys.stdout.write(text)
        sys.stdout.flush()

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
