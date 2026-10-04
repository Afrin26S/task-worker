import json
import unittest

from taskworker.human import AutoHuman
from tests.helpers import ErpTestCase, ScriptedLLM, act, eid, fill_bill, happy_worker, login_steps, snap, verifier_check

GOAL = "Find the latest invoice from Northwind Traders, enter it into the ERP and tell me when done"


class TestAgentEndToEnd(ErpTestCase):
    def test_happy_path_recovers_from_validation_error_expired_session_and_503(self):
        base = self.base

        llm = ScriptedLLM(lambda: happy_worker(base), verifier_check({"base": base, "must_contain": ["INV-2057", "1284.50", "2026-10-12"]}))
        human = AutoHuman(True)
        agent = self.agent(llm, human, max_steps=60)
        result = agent.run(GOAL)

        self.assertEqual(result.outcome, "completed", result.summary)
        self.assertTrue(result.verified)
        mine = [b for b in self.state.bills if b["invoice_number"] == "INV-2057"]
        self.assertEqual(len(mine), 1, "exactly one bill, no duplicates")
        self.assertEqual((mine[0]["amount"], mine[0]["currency"], mine[0]["due_date"]), ("1284.50", "USD", "2026-10-12"))
        self.assertGreaterEqual(len(human.approvals), 2, "writes were gated behind approval")
        trace = (agent.run_dir / "trace.jsonl").read_text()
        self.assertNotIn("demo123", trace, "secret must never reach the trace")
        self.assertIn("{{secret:ERP_PASSWORD}}", trace)
        for _, user in llm.prompts:
            self.assertNotIn("demo123", user, "secret must never reach the prompt")
        with open(f"{self.tmp.name}/mem.json") as f:
            self.assertIn("erp-session", f.read(), "a verified run leaves durable learnings behind")

    def test_human_rejection_is_respected(self):
        self.state.reset(chaos=False)
        base = self.base

        def worker():
            p = yield act("browser_goto", url=base + "/bills/new")
            p = yield from login_steps(p)
            p = yield act("browser_goto", url=base + "/bills/new")
            p = yield from fill_bill(p, "1284.50")
            assert "REJECTED" in p
            yield act("finish", outcome="blocked", summary="Human rejected the submission; nothing was entered.")

        agent = self.agent(ScriptedLLM(worker), AutoHuman(auto_approve=False), max_steps=30)
        result = agent.run(GOAL)
        self.assertEqual(result.outcome, "blocked")
        self.assertFalse(any(b["invoice_number"] == "INV-2057" for b in self.state.bills), "nothing written without approval")

    def test_policy_blocks_restricted_paths_and_foreign_hosts(self):
        base = self.base

        def worker():
            p = yield act("browser_goto", url=base + "/_debug/state")
            assert "BLOCKED by policy" in p
            p = yield act("browser_goto", url="http://evil.example.com/")
            assert "BLOCKED by policy" in p
            yield act("finish", outcome="blocked", summary="blocked by policy")

        result = self.agent(ScriptedLLM(worker)).run("poke around")
        self.assertEqual(result.outcome, "blocked")
        self.assertEqual([s.ok for s in result.steps[:2]], [False, False])

    def test_false_completion_claim_is_caught_by_independent_verifier(self):
        base = self.base

        def worker():
            p = yield act("finish", outcome="completed", summary="All done, INV-2057 entered.")   # lie: did nothing
            p = yield act("finish", outcome="completed", summary="Honestly done this time.")

        llm = ScriptedLLM(worker, verifier_check({"base": base, "must_contain": ["INV-2057"]}))
        result = self.agent(llm).run(GOAL)
        self.assertEqual(result.outcome, "unverified")
        self.assertFalse(result.verified)
        self.assertIn("VERIFICATION FAILED", "".join(s.output for s in result.steps))

    def test_malformed_model_output_is_repaired(self):
        def worker():
            yield "Sure! I will do that now."                                  # not JSON
            yield act("finish", outcome="blocked", summary="nothing to do")

        llm = ScriptedLLM(worker)
        result = self.agent(llm).run("noop")
        self.assertEqual(result.outcome, "blocked")
        self.assertTrue(any("PREVIOUS REPLY WAS INVALID" in u for _, u in llm.prompts))

    def test_repetition_triggers_runtime_warning(self):
        def worker():
            p = yield act("read_file", path="inbox/nope.txt")
            p = yield act("read_file", path="inbox/nope.txt")
            p = yield act("read_file", path="inbox/nope.txt")
            assert "identical action" in p
            yield act("finish", outcome="blocked", summary="file does not exist")

        self.assertEqual(self.agent(ScriptedLLM(worker)).run("read missing file").outcome, "blocked")

    def test_sandbox_blocks_path_traversal(self):
        def worker():
            p = yield act("read_file", path="../../etc/passwd")
            assert "outside the sandbox" in p
            yield act("finish", outcome="blocked", summary="denied")

        self.assertEqual(self.agent(ScriptedLLM(worker)).run("escape").outcome, "blocked")


def _playwright_available():
    try:
        from taskworker.browser import PlaywrightBrowser
        PlaywrightBrowser().close()
        return True
    except Exception:
        return False


@unittest.skipUnless(_playwright_available(), "Playwright/Chromium not installed")
class TestAgentEndToEndPlaywright(TestAgentEndToEnd):
    """Same scenarios, real Chromium instead of the HTTP backend."""
    browser = "playwright"


if __name__ == "__main__":
    unittest.main()
