"""Tool abstraction. A new capability/connector = one `Tool(...)` registration; the agent loop never changes."""
from dataclasses import dataclass, field
from typing import Callable, Optional

from .policy import Decision


@dataclass
class ToolResult:
    ok: bool
    output: str
    data: dict = field(default_factory=dict)


@dataclass
class Param:
    name: str
    type: str = "string"   # string | int | bool
    desc: str = ""
    required: bool = True


class Tool:
    def __init__(self, name, description, params, fn: Callable[..., ToolResult],
                 precheck: Optional[Callable[[dict], Optional[Decision]]] = None):
        self.name, self.description, self.params, self.fn, self.precheck = name, description, params, fn, precheck

    def signature(self) -> str:
        ps = ", ".join(f"{p.name}: {p.type}{'' if p.required else '?'}" for p in self.params)
        extra = "".join(f"\n      - {p.name}: {p.desc}" for p in self.params if p.desc)
        return f"{self.name}({ps}) - {self.description}{extra}"

    def validate(self, args: dict):
        """Returns (clean_args, error)."""
        if not isinstance(args, dict):
            return {}, "args must be a JSON object"
        known = {p.name for p in self.params}
        extra = set(args) - known
        if extra:
            return {}, f"unexpected argument(s) {sorted(extra)}; expected {sorted(known)}"
        clean = {}
        for p in self.params:
            if p.name not in args or args[p.name] is None:
                if p.required:
                    return {}, f"missing required argument '{p.name}'"
                continue
            v = args[p.name]
            try:
                if p.type == "int":
                    v = int(v)
                elif p.type == "bool":
                    v = v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes")
                else:
                    v = v if isinstance(v, str) else str(v)
            except (TypeError, ValueError):
                return {}, f"argument '{p.name}' must be {p.type}"
            clean[p.name] = v
        return clean, None


class ToolRegistry:
    def __init__(self, tools=()):
        self._tools = {}
        for t in tools:
            self.register(t)

    def register(self, tool: Tool):
        self._tools[tool.name] = tool

    def get(self, name):
        return self._tools.get(name)

    def names(self):
        return list(self._tools)

    def subset(self, names):
        return ToolRegistry([self._tools[n] for n in names if n in self._tools])

    def describe(self) -> str:
        return "\n".join(f"  * {t.signature()}" for t in self._tools.values())
