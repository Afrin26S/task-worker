"""Core non-browser tools: sandboxed file access, run notes, persistent memory, asking the human."""
import time
from pathlib import Path

from .tools import Param, Tool, ToolResult

MAX_FILE_CHARS = 8000


def _safe(root: Path, rel: str) -> Path:
    p = (root / rel).resolve()
    if root.resolve() != p and root.resolve() not in p.parents:
        raise PermissionError(f"path '{rel}' is outside the sandbox")
    return p


def build_core_tools(sandbox, notes: dict, memory, human, secrets, run_id=""):
    root = Path(sandbox)

    def list_files(path="."):
        d = _safe(root, path)
        if not d.is_dir():
            return ToolResult(False, f"'{path}' is not a directory")
        rows = []
        for f in sorted(d.iterdir()):
            kind = "dir " if f.is_dir() else "file"
            rows.append(f"{kind} {f.name}  ({f.stat().st_size} bytes)" if f.is_file() else f"{kind} {f.name}/")
        return ToolResult(True, "\n".join(rows) or "(empty directory)")

    def read_file(path):
        f = _safe(root, path)
        if not f.is_file():
            return ToolResult(False, f"'{path}' is not a file (use list_files to see what exists)")
        if f.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader
                text = "\n".join(pg.extract_text() or "" for pg in PdfReader(str(f)).pages)
            except ImportError:
                return ToolResult(False, "PDF support needs `pip install pypdf`")
        else:
            text = f.read_text(errors="replace")
        trunc = len(text) > MAX_FILE_CHARS
        return ToolResult(True, text[:MAX_FILE_CHARS] + ("\n...[truncated]" if trunc else ""))

    def note(key, value):
        notes[key] = value
        return ToolResult(True, f"noted {key}")

    def remember(key, value):
        memory.add(key, secrets.redact(value), run=run_id)
        return ToolResult(True, f"saved to long-term memory: {key}")

    def ask_user(question):
        ans = human.ask(question)
        if ans is None:
            return ToolResult(False, "No human is available to answer right now. Choose the safest reasonable path or finish with outcome 'needs_human'.")
        return ToolResult(True, f"User answered: {ans}")

    return [
        Tool("list_files", "List files in the company document sandbox (inbox of vendor documents).",
             [Param("path", "string", "directory relative to the sandbox root, e.g. 'inbox'", required=False)], list_files),
        Tool("read_file", "Read a text/PDF document from the sandbox.", [Param("path", "string", "e.g. 'inbox/file.txt'")], read_file),
        Tool("note", "Save a fact for THIS run (scratchpad shown to you every step).",
             [Param("key", "string"), Param("value", "string")], note),
        Tool("remember", "Save a DURABLE, reusable fact about how a system/procedure works for FUTURE runs (never one-off data or secrets).",
             [Param("key", "string"), Param("value", "string")], remember),
        Tool("ask_user", "Ask the human ONE concise question when you cannot safely decide yourself.",
             [Param("question", "string")], ask_user),
    ]
