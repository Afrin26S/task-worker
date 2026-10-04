"""Structured run trace (JSONL on disk) + readable console output."""
import json
import sys
import time
from pathlib import Path

_C = {"goal": "1;36", "plan": "36", "thought": "2", "tool": "1;34", "ok": "32", "err": "31", "approval": "1;33",
      "verify": "1;35", "warn": "33", "done": "1;32"}


class Trace:
    def __init__(self, run_dir, verbose=True):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.file = (self.run_dir / "trace.jsonl").open("a", encoding="utf-8")
        self.verbose = verbose
        self.color = sys.stdout.isatty()

    def event(self, type_, **data):
        self.file.write(json.dumps({"ts": round(time.time(), 2), "type": type_, **data}, default=str) + "\n")
        self.file.flush()

    def say(self, kind, text):
        if not self.verbose:
            return
        if self.color:
            text = f"\033[{_C.get(kind, '0')}m{text}\033[0m"
        print(text, flush=True)

    def close(self):
        self.file.close()
