"""Convert a Claude Code session transcript (JSONL) into readable Markdown.

  python -m scripts.export_transcript [path/to/session.jsonl] [--plan plan.md]

Writes docs/session-transcript.md. System reminders are dropped and email addresses are
redacted, as are Anthropic API keys. Long tool outputs are truncated; the user's own messages are kept in full.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
API_KEY = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")
REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)
MAX_TOOL_OUT = 1500
MAX_TOOL_IN = 1200


def clean(text: str) -> str:
    text = REMINDER.sub("", text)
    text = API_KEY.sub("[api key redacted]", text)
    return EMAIL.sub("[email redacted]", text).strip()


def clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + f"\n… [truncated, {len(text) - n} more chars]"


def block_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content or []:
        t = b.get("type")
        if t == "text":
            parts.append(b["text"])
        elif t == "document":
            parts.append("[attached document]")
        elif t == "image":
            parts.append("[image]")
    return "\n".join(parts)


def default_transcript() -> str:
    # Claude Code names project folders by replacing every non-alphanumeric character
    # with "-" (e.g. C:\Users\me\repo -> C--Users-me-repo, /home/u/repo -> -home-u-repo).
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(ROOT))
    files = glob.glob(os.path.expanduser(f"~/.claude/projects/{slug}/*.jsonl"))
    if not files:
        raise SystemExit("no transcript found; pass the .jsonl path")
    return max(files, key=os.path.getmtime)


def convert(path: str) -> str:
    out = ["# Session transcript: building the Weather Risk Intelligence Agent with Claude Code",
           "", "Exported from the Claude Code session log. System reminders are removed, email "
           "addresses redacted, and long tool outputs truncated. The assistant's internal "
           "reasoning is not included (it is not stored in readable form).", ""]
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        if d.get("isSidechain") or d.get("type") not in ("user", "assistant"):
            continue
        m = d.get("message", {})
        content = m.get("content")
        if d["type"] == "user":
            if isinstance(content, str):
                txt = clean(content)
                if txt:
                    out += ["---", "", "## 🧑 User", "", txt, ""]
                continue
            for b in content or []:
                if b.get("type") == "tool_result":
                    txt = clean(block_text(b.get("content")))
                    # Rejected plan approvals carry the user's own feedback: keep in full.
                    is_feedback = "The user doesn't want to proceed" in txt or "user said:" in txt
                    label = "User feedback (via tool rejection)" if is_feedback else "Tool result"
                    body = txt if is_feedback else clip(txt, MAX_TOOL_OUT)
                    out += [f"<details><summary>{label}</summary>", "", "```", body or "(empty)",
                            "```", "</details>", ""]
                elif b.get("type") == "text":
                    txt = clean(b["text"])
                    if txt:
                        out += ["---", "", "## 🧑 User", "", txt, ""]
        else:
            for b in content or []:
                if b.get("type") == "text" and b["text"].strip():
                    out += ["### 🤖 Claude", "", clean(b["text"]), ""]
                elif b.get("type") == "tool_use":
                    inp = json.dumps(b.get("input", {}), indent=1)
                    out += [f"**Tool call:** `{b['name']}`", "", "```json",
                            clip(clean(inp), MAX_TOOL_IN), "```", ""]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?")
    ap.add_argument("--plan")
    args = ap.parse_args()
    md = convert(args.path or default_transcript())
    if args.plan and Path(args.plan).exists():
        md += "\n\n---\n\n# Appendix: approved plan\n\n" + clean(Path(args.plan).read_text(encoding="utf-8"))
    dest = ROOT / "docs" / "session-transcript.md"
    dest.write_text(md, encoding="utf-8")
    print(f"wrote {dest} ({len(md):,} chars)")


if __name__ == "__main__":
    main()
