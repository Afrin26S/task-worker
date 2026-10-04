"""Permissions layer: what the agent may touch, and which actions need a human's approval.

The agent never gets to decide this itself - every tool call passes through `Policy` first.
"""
import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class ApprovalRequest:
    action: str       # human-readable description of the side effect
    details: dict     # exact payload that will be sent (so the human approves what really happens)

    @property
    def key(self) -> str:
        return json.dumps([self.action, self.details], sort_keys=True)


@dataclass
class Decision:
    blocked: Optional[str] = None
    approval: Optional[ApprovalRequest] = None


class Policy:
    def __init__(self, allowed_hosts=("127.0.0.1", "localhost"), blocked_path_prefixes=("/_debug",),
                 safe_submit_paths=("/login",)):
        self.allowed_hosts = set(allowed_hosts)
        self.blocked_path_prefixes = tuple(blocked_path_prefixes)
        self.safe_submit_paths = set(safe_submit_paths)

    def check_url(self, url: str) -> Optional[str]:
        u = urlparse(url)
        if u.scheme not in ("http", "https"):
            return f"scheme '{u.scheme}' is not allowed"
        if u.hostname not in self.allowed_hosts:
            return f"host '{u.hostname}' is outside the allowed hosts {sorted(self.allowed_hosts)}"
        if any(u.path.startswith(p) for p in self.blocked_path_prefixes):
            return f"path '{u.path}' is restricted"
        return None

    def check_click(self, info: dict) -> Decision:
        """`info` is a description of what a click would do (see browser backends' click_info)."""
        if info.get("kind") == "link":
            reason = self.check_url(info["href"])
            return Decision(blocked=reason) if reason else Decision()
        if info.get("submit") and info.get("action"):
            reason = self.check_url(info["action"])
            if reason:
                return Decision(blocked=reason)
            path = urlparse(info["action"]).path
            if info.get("method", "get").lower() != "get" and path not in self.safe_submit_paths:
                return Decision(approval=ApprovalRequest(
                    f"Submit form via {info['method'].upper()} {path} (button '{info.get('label', '')}')",
                    info.get("fields", {})))
        return Decision()
