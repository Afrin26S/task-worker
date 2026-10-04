import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from taskworker import llm as llm_mod
from taskworker.llm import LLMError, OpenAICompatLLM, extract_json
from tests.fake_llm_server import serve
from tests.helpers import ScriptedLLM, happy_worker, verifier_check

ROOT = Path(__file__).resolve().parent.parent


class TestLLMClient(unittest.TestCase):
    def test_extract_json_tolerates_prose_and_fences(self):
        self.assertEqual(extract_json('Here you go:\n```json\n{"a": {"b": "}"}}\n```'), {"a": {"b": "}"}})
        with self.assertRaises(ValueError):
            extract_json("no json here")
        with self.assertRaises(ValueError):
            extract_json('{"a": ')

    def test_openai_compat_client_parses_usage_and_retries_429(self):
        class Echo:
            def complete(self, s, u):
                return '{"ok": true}'
        srv, url = serve(Echo(), fail_first=1)
        real_sleep, llm_mod.time.sleep = llm_mod.time.sleep, lambda s: None     # don't wait for backoff in tests
        try:
            c = OpenAICompatLLM(model="m", api_key="k", base_url=url)
            self.assertEqual(c.complete("sys", "user"), '{"ok": true}')
            self.assertEqual(c.calls, 2, "first call got 429 and was retried")
            self.assertEqual((c.input_tokens, c.output_tokens), (10, 5))
        finally:
            llm_mod.time.sleep = real_sleep
            srv.shutdown()
            srv.server_close()

    def test_non_retryable_error_surfaces(self):
        c = OpenAICompatLLM(model="m", api_key="k", base_url="http://127.0.0.1:9/v1")  # nothing listens here
        llm_mod.time.sleep, real = (lambda s: None), llm_mod.time.sleep
        try:
            with self.assertRaises(LLMError):
                c.complete("a", "b")
        finally:
            llm_mod.time.sleep = real


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestCLI(unittest.TestCase):
    def test_cli_end_to_end_through_real_http_llm_client(self):
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        scripted = ScriptedLLM(lambda: happy_worker(base), verifier_check({"base": base, "must_contain": ["INV-2057", "1284.50", "2026-10-12"]}))
        srv, llm_url = serve(scripted)
        runs = Path(ROOT / "runs")
        before = set(runs.glob("*")) if runs.exists() else set()
        env = dict(os.environ, LLM_PROVIDER="openai", LLM_API_KEY="test", LLM_BASE_URL=llm_url, LLM_MODEL="fake", PYTHONPATH=str(ROOT))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                p = subprocess.run([sys.executable, "run.py", "Find the latest invoice from Northwind Traders and enter it into the ERP",
                                    "--auto-approve", "--port", str(port), "--no-memory", "--max-steps", "60"],
                                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=180)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(p.returncode, 0, p.stdout[-2000:] + p.stderr[-2000:])
        self.assertIn("RESULT: COMPLETED", p.stdout)
        new = sorted(set(runs.glob("*")) - before)
        self.assertTrue(new and (new[-1] / "report.md").exists() and (new[-1] / "trace.jsonl").exists())
        report = (new[-1] / "report.md").read_text()
        self.assertIn("Independently verified:** `True`", report)
        self.assertNotIn("demo123", report)
        for d in new:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
