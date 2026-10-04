"""Test helpers: a scripted stand-in for the LLM that drives the REAL agent loop against the REAL mock ERP."""
import json
import os
import re
import tempfile
import unittest
import shutil

from mock_erp.server import start
from taskworker.factory import build_agent, close_agent
from taskworker.human import AutoHuman

os.environ.setdefault("ERP_PASSWORD", "demo123")

PLAN = {"restated_goal": "Enter the latest Northwind invoice into the ERP",
        "success_criteria": ["ERP shows Northwind Traders INV-2057 with amount 1284.50 USD due 2026-10-12"],
        "plan": ["read invoice", "check ERP for duplicates", "enter bill", "verify"], "ambiguities": []}


def act(tool, thought="", **args):
    return {"thought": thought or f"call {tool}", "action": {"tool": tool, "args": args}}


def snap(prompt):
    """The most recent page snapshot inside a prompt."""
    return prompt[prompt.rfind("URL: "):] if "URL: " in prompt else prompt


def eid(prompt, label):
    """Find the [id] of the last element whose label contains `label` in the latest snapshot."""
    found = None
    for m in re.finditer(r'\[(\d+)\] \w+(?:\([^)]*\))? "([^"]*)"', snap(prompt)):
        if label.lower() in m.group(2).lower():
            found = int(m.group(1))
    if found is None:
        raise AssertionError(f"element '{label}' not found in latest snapshot:\n{snap(prompt)[:1500]}")
    return found


class ScriptedLLM:
    name = "scripted"

    def __init__(self, worker=None, verifier=None):
        self.calls = self.input_tokens = self.output_tokens = 0
        self.factories = {"worker": worker, "verifier": verifier}
        self.gens = {k: (f() if f else None) for k, f in self.factories.items()}
        self.started = set()
        self.prompts = []

    def _next(self, who, user):
        g = self.gens[who]
        if g is None:
            raise AssertionError(f"unexpected {who} call")
        if who not in self.started:
            self.started.add(who)
            out = next(g)
        else:
            try:
                out = g.send(user)
            except StopIteration:            # the verifier is invoked once per verification attempt: restart its script
                if who != "verifier":
                    raise AssertionError("worker script exhausted but the agent asked for another action")
                self.gens[who] = self.factories[who]()
                out = next(self.gens[who])
        return out if isinstance(out, str) else json.dumps(out)

    def complete(self, system, user):
        self.calls += 1
        self.prompts.append((system, user))
        if "currently struggling" in system:
            return json.dumps({"diagnosis": "test", "plan": ["try something else"]})
        if "You review a COMPLETED" in system:
            return json.dumps({"learnings": [{"key": "erp-session", "value": "The ERP session can expire silently; log in again and re-enter the form."}]})
        if "VERIFIER (auditor)" in system:
            return self._next("verifier", user)
        if "You are the PLANNER" in system:
            return json.dumps(PLAN)
        return self._next("worker", user)


def login_steps(p):
    """Use with `p = yield from login_steps(p)`: logs in with the secret placeholder (login page must be showing)."""
    p = yield act("browser_type", id=eid(p, "Username"), text="demo")
    p = yield act("browser_type", id=eid(p, "Password"), text="{{secret:ERP_PASSWORD}}")
    p = yield act("browser_click", id=eid(p, "Sign in"))
    return p


def verifier_check(expect):
    """Verifier script: open /bills (logging in if needed) and judge by the page content."""
    def script():
        p = yield act("browser_goto", url=expect["base"] + "/bills")
        if "Sign in" in snap(p):
            p = yield from login_steps(p)
        s = snap(p)
        ok = all(x in s for x in expect["must_contain"])
        yield act("verdict", verified=ok, evidence=s[-400:], reason="found expected record" if ok else "expected record not found in ERP")
    return script


def fill_bill(p, amount):
    """Generator: fill the bill form (already open) and click Save. Returns the prompt after clicking."""
    p = yield act("browser_select", id=eid(p, "Vendor"), option="Northwind Traders")
    p = yield act("browser_type", id=eid(p, "Invoice number"), text="INV-2057")
    p = yield act("browser_type", id=eid(p, "Amount"), text=amount)
    p = yield act("browser_type", id=eid(p, "Due date"), text="2026-10-12")
    p = yield act("browser_click", id=eid(p, "Save bill"))
    return p



def happy_worker(base):
    """A scripted 'model' that completes the invoice task, deliberately hitting every fault the ERP injects."""
    p = yield act("list_files", path="inbox")
    p = yield act("read_file", path="inbox/northwind_2026-09-14_INV-2057.txt")
    p = yield act("browser_goto", url=base + "/bills")                   # -> login screen
    p = yield from login_steps(p)
    assert "INV-2057" not in snap(p), "duplicate check: not in ERP yet"   # observed the bills list first
    p = yield act("browser_goto", url=base + "/bills/new")
    p = yield from fill_bill(p, "1,284.50")                              # wrong format on purpose
    assert "plain number" in snap(p)                                      # validation error observed
    p = yield act("browser_type", id=eid(p, "Amount"), text="1284.50")
    p = yield act("browser_click", id=eid(p, "Save bill"))               # -> session silently expired
    assert "expired" in snap(p).lower()
    p = yield from login_steps(p)
    p = yield act("browser_goto", url=base + "/bills/new")
    p = yield from fill_bill(p, "1284.50")                               # -> HTTP 503
    assert "HTTP 503" in snap(p)
    p = yield act("browser_goto", url=base + "/bills/new")
    p = yield from fill_bill(p, "1284.50")                               # -> saved
    assert "saved" in snap(p).lower()
    yield act("finish", outcome="completed", summary="Entered Northwind Traders INV-2057, 1284.50 USD, due 2026-10-12.")


class ErpTestCase(unittest.TestCase):
    browser = "http"

    def setUp(self):
        self.server, self.state = start(0, chaos=True)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.tmp = tempfile.TemporaryDirectory()
        # Cleanups run last-in-first-out, so the agent (registered later) closes its
        # trace file BEFORE the temp dir is deleted. Windows locks open files.
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def agent(self, llm, human=None, **kw):
        a = build_agent(llm, human or AutoHuman(True), erp_url=self.base, run_root=self.tmp.name, memory_path=f"{self.tmp.name}/mem.json",
                        browser=self.browser, verbose=False, goal_slug="test", **kw)
        self.addCleanup(close_agent, a)
        return a
