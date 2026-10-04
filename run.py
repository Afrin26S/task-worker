#!/usr/bin/env python3
"""CLI:  python run.py "Find the latest invoice from Northwind Traders, enter it into the ERP and tell me when it's done" """
import argparse
import os
import sys
import threading

from mock_erp.server import start as start_erp
from taskworker.factory import ROOT, build_agent, close_agent
from taskworker.human import AutoHuman, CLIHuman
from taskworker.llm import make_llm
from taskworker.report import write_report


def load_env():
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    load_env()
    ap = argparse.ArgumentParser(description="Autonomous AI task worker")
    ap.add_argument("task", nargs="?", help="natural-language task")
    ap.add_argument("--browser", choices=["auto", "playwright", "http"], default="auto")
    ap.add_argument("--headed", action="store_true", help="show the browser window (great for the demo video)")
    ap.add_argument("--slow-mo", type=int, default=0, help="ms delay between browser actions (headed demos)")
    ap.add_argument("--auto-approve", action="store_true", help="approve all write actions automatically (sandbox/testing only)")
    ap.add_argument("--no-chaos", action="store_true", help="disable the ERP's injected failures (session expiry, 503)")
    ap.add_argument("--max-steps", type=int, default=50)
    ap.add_argument("--no-memory", action="store_true", help="do not read/write persistent memory")
    ap.add_argument("--no-reflect", action="store_true", help="skip the post-run learning step")
    ap.add_argument("--provider", choices=["anthropic", "openai"], help="LLM provider (default: auto from env)")
    ap.add_argument("--model")
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--no-server", action="store_true", help="don't start the mock ERP (use an already running one)")
    ap.add_argument("--serve-only", action="store_true", help="just run the mock ERP and wait (to poke at it in your own browser)")
    args = ap.parse_args()

    os.environ.setdefault("ERP_PASSWORD", "demo123")  # sandbox credential; the LLM only ever sees {{secret:ERP_PASSWORD}}
    if args.serve_only:
        start_erp(args.port, chaos=not args.no_chaos)
        print(f"Mock ERP running at http://127.0.0.1:{args.port}  (demo / demo123). Ctrl+C to stop.")
        threading.Event().wait()
    if not args.task:
        ap.error("a task is required, e.g.  python run.py \"Find the latest invoice from Northwind Traders ...\"")

    if not args.no_server:
        start_erp(args.port, chaos=not args.no_chaos)
    llm = make_llm(args.provider, args.model)
    human = AutoHuman(True) if args.auto_approve else CLIHuman()
    agent = build_agent(llm, human, erp_url=f"http://127.0.0.1:{args.port}", memory_path=None if args.no_memory else ROOT / ".agent_memory" / "memory.json",
                        browser=args.browser, headed=args.headed, slow_mo=args.slow_mo, max_steps=args.max_steps,
                        reflect=not args.no_reflect and not args.no_memory, goal_slug=args.task)
    try:
        result = agent.run(args.task)
        report = write_report(agent, result, args.task)
        print(f"\nReport: {report}")
    finally:
        close_agent(agent)
    sys.exit(0 if result.outcome == "completed" else 1)


if __name__ == "__main__":
    main()