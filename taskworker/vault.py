"""Secret handling: the LLM only ever sees `{{secret:NAME}}` placeholders.

Real values are substituted in the tool layer right before execution and scrubbed from
every observation / trace line, so credentials never enter the prompt or the logs.
"""
import os
import re

_PLACEHOLDER = re.compile(r"\{\{secret:([A-Z0-9_]+)\}\}")


class SecretStore:
    def __init__(self, names=("ERP_PASSWORD",)):
        self.names = list(names)

    def resolve(self, obj):
        if isinstance(obj, str):
            def sub(m):
                name = m.group(1)
                if name not in os.environ:
                    raise KeyError(f"secret '{name}' is not configured")
                return os.environ[name]
            return _PLACEHOLDER.sub(sub, obj)
        if isinstance(obj, dict):
            return {k: self.resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.resolve(v) for v in obj]
        return obj

    def redact(self, text: str) -> str:
        for n in self.names:
            val = os.environ.get(n)
            if val and len(val) >= 3:
                text = text.replace(val, f"{{{{secret:{n}}}}}")
        return text
