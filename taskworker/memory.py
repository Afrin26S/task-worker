"""Persistent company memory: durable facts the agent learned in earlier runs (JSON on disk)."""
import json
import time
from pathlib import Path


class Memory:
    def __init__(self, path):
        self.path = Path(path) if path else None
        self.facts = []
        if self.path and self.path.exists():
            try:
                self.facts = json.loads(self.path.read_text()).get("facts", [])
            except (json.JSONDecodeError, OSError):
                self.facts = []

    def add(self, key, value, run=""):
        self.facts = [f for f in self.facts if f["key"] != key]  # newer knowledge replaces older
        self.facts.append({"key": key, "value": value, "run": run, "ts": time.strftime("%Y-%m-%d %H:%M")})
        self._save()

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"facts": self.facts}, indent=2))

    def render(self, limit=25) -> str:
        return "\n".join(f"- {f['key']}: {f['value']}" for f in self.facts[-limit:])
