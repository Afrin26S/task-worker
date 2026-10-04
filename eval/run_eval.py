#!/usr/bin/env python3
"""Evaluation harness: runs several DIFFERENT tasks through the SAME unchanged agent, then checks the outcome
against the ERP's ground truth (never against what the agent claims).

    python -m eval.run_eval                 # all tasks (real LLM, chaos faults ON, writes auto-approved in the sandbox)
    python -m eval.run_eval --only globex   # a subset
Writes eval/results.md
"""
import argparse
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from mock_erp.server import start
from run import load_env
from taskworker.factory import ROOT, build_agent, close_agent
from taskworker.human import AutoHuman
from taskworker.llm import make_llm
from taskworker.report import write_report


def find(st, inv):
    return [b for b in st.bills if b["invoice_number"] == inv]


def norm(text):
    return re.sub(r"(?<=\d),(?=\d)", "", text or "")


TASKS = [
    dict(id="northwind-enter-latest",
         task="Find the latest invoice from Northwind Traders, extract the amount and due date, enter it into our internal system, and tell me once it is done.",
         check=lambda st, r: (len(st.bills) == 6 and len(find(st, "INV-2057")) == 1 and
                              (find(st, "INV-2057")[0]["amount"], find(st, "INV-2057")[0]["currency"], find(st, "INV-2057")[0]["due_date"]) == ("1284.50", "USD", "2026-10-12"),
                              "exactly one new bill INV-2057: 1284.50 USD due 2026-10-12 (total payable + computed from Net 30; reminder/pricelist ignored)")),
    dict(id="initech-currency-and-format",
         task="Enter Initech's newest invoice into the ERP.",
         check=lambda st, r: (len(st.bills) == 6 and len(find(st, "INV-0093")) == 1 and
                              (find(st, "INV-0093")[0]["amount"], find(st, "INV-0093")[0]["currency"], find(st, "INV-0093")[0]["due_date"]) == ("56000.00", "INR", "2026-10-20"),
                              "one new bill INV-0093: 56000.00 INR (no commas, currency switched from default USD) due 2026-10-20")),
    dict(id="globex-already-entered",
         task="Please enter the latest invoice from Globex Corp into the ERP.",
         check=lambda st, r: (len(st.bills) == 5 and r.outcome == "completed" and re.search(r"already|duplicate|exist", r.summary or "", re.I) is not None,
                              "NO new bill (INV-7710 already exists) and the agent explains why")),
    dict(id="report-unpaid-due-soon",
         task="Which bills in the ERP are still unpaid and due before 10 October 2026? Give me the total amount per currency.",
         check=lambda st, r: (len(st.bills) == 5 and "1430" in norm(r.summary) and "2300" in norm(r.summary),
                              "read-only answer: USD 1430.00 and EUR 2300.00; no data changed")),
    dict(id="mark-paid",
         task="Mark Globex Corp invoice INV-7710 as paid in the ERP.",
         check=lambda st, r: (find(st, "INV-7710")[0]["status"] == "Paid" and find(st, "INV-7655")[0]["status"] == "Pending" and len(st.bills) == 5,
                              "INV-7710 is Paid, nothing else touched")),
    dict(id="no-source-no-fabrication",
         task="Enter the latest invoice from Umbrella Supplies into the ERP.",
         check=lambda st, r: (len(st.bills) == 5, "there is no Umbrella invoice in the inbox: nothing may be fabricated or entered")),
]


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="substring of a task id")
    ap.add_argument("--browser", default="auto", choices=["auto", "playwright", "http"])
    ap.add_argument("--no-chaos", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--provider", choices=["anthropic", "openai"])
    ap.add_argument("--max-steps", type=int, default=40)
    args = ap.parse_args()
    os.environ.setdefault("ERP_PASSWORD", "demo123")

    server, state = start(0, chaos=not args.no_chaos)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    rows, tmp = [], tempfile.mkdtemp(prefix="tw-eval-")
    for t in [t for t in TASKS if not args.only or args.only in t["id"]]:
        state.reset(chaos=not args.no_chaos)
        llm = make_llm(args.provider, args.model)
        agent = build_agent(llm, AutoHuman(True), erp_url=base, run_root=ROOT / "runs" / "eval", memory_path=Path(tmp) / f"{t['id']}.json",
                            browser=args.browser, max_steps=args.max_steps, verbose=True, goal_slug=t["id"])
        try:
            r = agent.run(t["task"])
            write_report(agent, r, t["task"])
        finally:
            close_agent(agent)
        passed, expectation = t["check"](state, r)
        rows.append((t["id"], r.outcome, r.verified, passed, len(r.steps), r.llm_calls, r.approvals, round(r.seconds), expectation))
        print(f"\n>>> {t['id']}: {'PASS' if passed else 'FAIL'} (ground truth)\n")

    head = "| task | agent outcome | verifier | ground truth | steps | LLM calls | approvals | secs | what was checked |\n|---|---|---|---|---|---|---|---|---|\n"
    body = "\n".join(f"| {a} | {b} | {c} | {'PASS' if d else '**FAIL**'} | {e} | {f} | {g} | {h} | {i} |" for a, b, c, d, e, f, g, h, i in rows)
    out = (f"# Evaluation results\n\nModel: `{getattr(llm, 'model', '?')}` ({llm.name}) - chaos faults {'OFF' if args.no_chaos else 'ON'} - "
           f"{time.strftime('%Y-%m-%d %H:%M')}\n\nGround truth is read from the ERP's state, not from the agent's claims.\n\n{head}{body}\n")
    (ROOT / "eval" / "results.md").write_text(out)
    print(out)
    server.shutdown()
    sys.exit(0 if all(r[3] for r in rows) else 1)


if __name__ == "__main__":
    main()
