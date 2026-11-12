#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Hook 适配器：从 coding agent 的回合结束事件里取出最后一条回复，播报一句摘要。

支持两种来源：
  - Claude Code 的 Stop hook（事件 JSON 从 stdin 传入，里面带 transcript_path）
  - Codex 的 notify（事件 JSON 作为第一个命令行参数传入）

**这里刻意不调用 LLM。** 规则摘要（取首段 + 截断）质量有限，它的定位是
"在你忘了播报时兜住，别让这一轮彻底没声音"。想要播报稿聪明，就该让 agent
自己按 docs/broadcast-style.md 写好，直接调 speak.py。

用法（在 hook 配置里指向本文件即可，不需要参数）：
    python notify.py
    python notify.py '{"type":"agent-turn-complete","last-assistant-message":"..."}'
"""
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SPEAK = os.path.join(HERE, "speak.py")
MAX_CHARS = 60


def _clean(text):
    """去掉 markdown 标记与代码块，压成一行。"""
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"[*_#>]+", "", text)
    text = re.sub(r"^\s*[-+*]\s+", "", text, flags=re.M)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _summarize(text):
    """取最开头那一个完整句子。宁可短，也不要把整段念出来。"""
    text = _clean(text)
    if not text:
        return ""

    # 第一句：到第一个中文或英文句末标点为止
    m = re.search(r"[。！？!?；;\n]", text)
    head = text[: m.start()] if m else text
    head = head.strip(" ，,")

    if not head:
        head = text[:MAX_CHARS]

    if len(head) > MAX_CHARS:
        head = head[:MAX_CHARS].rstrip("，,、 ") + "…"
    return head


def _last_assistant_text(transcript_path):
    """从 Claude Code 的 .jsonl 会话记录里取最后一条 assistant 文本。"""
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    last = ""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                chunks = []
                if isinstance(content, str):
                    chunks.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            chunks.append(part.get("text", ""))
                text = "\n".join(c for c in chunks if c).strip()
                if text:
                    last = text
    except OSError:
        return ""
    return last


def _looks_like_broken_json(text):
    """残缺的 JSON（被 shell 吃掉引号之类）—— 念出来是噪音，不如不念。"""
    t = text.lstrip()
    return t.startswith("{") or t.startswith("[")


def _extract(argv):
    """返回要播报的原文。看不懂事件就返回空串，静默退出。"""
    payload = None

    if len(argv) > 1:
        # Codex notify：JSON 作为参数
        try:
            payload = json.loads(argv[1])
        except ValueError:
            return "" if _looks_like_broken_json(argv[1]) else argv[1]

    if payload is None:
        # Claude Code hook：JSON 从 stdin
        raw = ""
        if not sys.stdin.isatty():
            try:
                raw = sys.stdin.read()
            except (OSError, ValueError):
                raw = ""
        if raw.strip():
            try:
                payload = json.loads(raw)
            except ValueError:
                return "" if _looks_like_broken_json(raw) else raw

    if not isinstance(payload, dict):
        return ""

    # Codex 直接把最后一条消息放在事件里
    for key in ("last-assistant-message", "last_assistant_message", "message"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val

    # Claude Code 给的是 transcript 路径
    return _last_assistant_text(payload.get("transcript_path"))


def main(argv):
    text = _extract(argv)
    line = _summarize(text)
    if not line:
        return 0  # 没东西可说就安静退出，不要在 hook 里报错

    print(f"[notify] {line}", flush=True)
    return subprocess.run([sys.executable, SPEAK, line]).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv))
