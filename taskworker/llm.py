"""Provider-agnostic LLM client (stdlib only: no SDK needed).

The agent only ever calls `llm.complete(system, user) -> str`, so any provider can be plugged in.
  * Anthropic Messages API        (ANTHROPIC_API_KEY)
  * Any OpenAI-compatible API     (OPENAI_API_KEY / LLM_API_KEY + LLM_BASE_URL)
    -> OpenAI, Google Gemini (OpenAI-compat endpoint), Groq, OpenRouter, Ollama, ...
Transient failures (429/5xx/timeouts) are retried with exponential backoff.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    def __init__(self, msg, retryable=False, quota=False):
        super().__init__(msg)
        self.retryable = retryable
        self.quota = quota      # daily/long-window quota exhausted: retrying the same model is pointless


def extract_json(text: str) -> dict:
    """Pull the first balanced JSON object out of a model reply (tolerates prose / code fences)."""
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object found in reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except json.JSONDecodeError as e:
                    raise ValueError(f"invalid JSON: {e}")
                if not isinstance(obj, dict):
                    raise ValueError("reply JSON is not an object")
                return obj
    raise ValueError("unterminated JSON object")


class BaseLLM:
    name = "base"

    def __init__(self):
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def complete(self, system: str, user: str) -> str:
        """Retry transient errors with backoff; if a model is exhausted (daily quota) or stays overloaded, fail over
        to the next model in `self.chain` (LLM_MODELS=a,b,c) and keep going."""
        delays = [2, 4, 8, 16, 32, 60]
        attempt = 0
        while True:
            try:
                self.calls += 1
                return self._complete(system, user)
            except LLMError as e:
                if not (e.retryable or e.quota):
                    raise
                if (e.quota or attempt >= 3) and self._next_model():
                    attempt = 0
                    continue
                if e.quota or attempt >= len(delays):
                    raise
                print(f"   (LLM busy: retrying in {delays[attempt]}s)", flush=True)
                time.sleep(delays[attempt])
                attempt += 1

    def _next_model(self):
        chain = getattr(self, "chain", [])
        if self.model in chain and chain.index(self.model) + 1 < len(chain):
            self.model = chain[chain.index(self.model) + 1]
            print(f"   (switching to model: {self.model})", flush=True)
            return True
        return False

    def _complete(self, system, user):  # pragma: no cover
        raise NotImplementedError

    @staticmethod
    def _post(url, headers, body, timeout=120):
        headers = {**headers, "User-Agent": "Mozilla/5.0 (task-worker)"}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:1500]
            # "Please retry in 13h49m" (or minutes) = quota window, not a blip: don't wait it out, switch model
            long_wait = e.code == 429 and re.search(r"retry in \d+h|retry in \d+m", detail) is not None
            raise LLMError(f"HTTP {e.code} from LLM API: {detail[:500]}",
                           retryable=e.code in (408, 409, 429, 500, 502, 503, 529) and not long_wait, quota=long_wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"network error calling LLM API: {e}", retryable=True)


class AnthropicLLM(BaseLLM):
    name = "anthropic"

    def __init__(self, model=None, api_key=None):
        super().__init__()
        self.model = model or os.environ.get("LLM_MODEL", "claude-sonnet-5-5")
        self.api_key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self.base_url = (os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")

    def _complete(self, system, user):
        data = self._post(
            f"{self.base_url}/v1/messages",
            {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            {"model": self.model, "max_tokens": 2048, "system": system, "messages": [{"role": "user", "content": user}]},
        )
        usage = data.get("usage", {})
        self.input_tokens += usage.get("input_tokens", 0)
        self.output_tokens += usage.get("output_tokens", 0)
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class OpenAICompatLLM(BaseLLM):
    name = "openai-compatible"

    def __init__(self, model=None, api_key=None, base_url=None):
        super().__init__()
        self.model = model or os.environ.get("LLM_MODEL", "gpt-4o")
        self.api_key = api_key or os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        extra = [m.strip() for m in (os.environ.get("LLM_MODELS", "") + "," + os.environ.get("LLM_FALLBACK_MODEL", "")).split(",") if m.strip()]
        self.chain = [self.model] + [m for m in extra if m != self.model]

    def _complete(self, system, user):
        body = {"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if os.environ.get("LLM_JSON_MODE") == "1":
            body["response_format"] = {"type": "json_object"}
        data = self._post(f"{self.base_url}/chat/completions",
                          {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, body)
        usage = data.get("usage", {})
        self.input_tokens += usage.get("prompt_tokens", 0)
        self.output_tokens += usage.get("completion_tokens", 0)
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError):
            raise LLMError(f"unexpected LLM response shape: {str(data)[:300]}")


def make_llm(provider=None, model=None):
    provider = provider or os.environ.get("LLM_PROVIDER")
    if not provider:
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("LLM_BASE_URL"):
            provider = "openai"
        else:
            raise SystemExit("No LLM configured. Set ANTHROPIC_API_KEY, or LLM_API_KEY (+ LLM_BASE_URL, LLM_MODEL) - see .env.example")
    return AnthropicLLM(model) if provider == "anthropic" else OpenAICompatLLM(model)