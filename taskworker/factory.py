"""Single place that wires everything together (used by the CLI, the eval harness and the tests)."""
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from .agent import Agent
from .browser import HttpBrowser, make_browser
from .browser_tools import BrowserSession, build_browser_tools
from .core_tools import build_core_tools
from .memory import Memory
from .policy import Policy
from .tools import ToolRegistry
from .trace import Trace
from .vault import SecretStore

ROOT = Path(__file__).resolve().parent.parent


def build_agent(llm, human, *, erp_url="http://127.0.0.1:8800", sandbox=None, context_file=None, run_root=None,
                memory_path=None, browser="auto", headed=False, slow_mo=0, max_steps=30, reflect=True,
                verbose=True, goal_slug="run"):
    sandbox = Path(sandbox or ROOT / "sandbox")
    context_file = Path(context_file or sandbox / "company_context.md")
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + (re.sub(r"[^a-z0-9]+", "-", goal_slug.lower())[:28].strip("-") or "run")
    run_dir = Path(run_root or ROOT / "runs") / run_id
    trace = Trace(run_dir, verbose=verbose)

    host = urlparse(erp_url).hostname
    policy = Policy(allowed_hosts={host, "127.0.0.1", "localhost"})
    secrets = SecretStore()
    memory = Memory(memory_path)
    notes = {}

    if browser == "http":
        session = BrowserSession(HttpBrowser)
    else:
        session = BrowserSession(lambda: make_browser(browser, headed=headed, slow_mo=slow_mo))

    tools = ToolRegistry(build_core_tools(sandbox, notes, memory, human, secrets, run_id) + build_browser_tools(session, policy))

    def evidence():
        if not session.started:
            return []
        return [session.get().capture_evidence(run_dir, "final_page_state")]

    context = context_file.read_text().replace("{{ERP_URL}}", erp_url) if context_file.exists() else ""
    agent = Agent(llm, tools, policy, human, memory, trace, secrets, notes, context_text=context, max_steps=max_steps,
                  evidence_hook=evidence, reflect=reflect, run_id=run_id)
    agent.run_dir, agent.session = run_dir, session
    return agent


def close_agent(agent):
    agent.session.close()
    agent.trace.close()
