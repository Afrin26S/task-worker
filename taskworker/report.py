"""Human-readable run report: outcome, evidence, and the full decision trail (why the agent did what it did)."""
from pathlib import Path

from .agent import brief


def write_report(agent, result, goal) -> Path:
    L = [f"# Run report", "", f"**Task:** {goal}", "",
         f"**Outcome:** `{result.outcome}`   |   **Independently verified:** `{result.verified}`   |   "
         f"**Steps:** {len(result.steps)}   |   **Human approvals:** {result.approvals}   |   **LLM calls:** {result.llm_calls}   |   "
         f"**Time:** {result.seconds:.0f}s", "", "## Summary (from the agent)", "", result.summary or "(none)", ""]
    if result.verification:
        L += ["## Independent verification", "", f"- **Verdict:** {'VERIFIED' if result.verification.get('verified') else 'NOT VERIFIED'}",
              f"- **Evidence observed:** {result.verification.get('evidence', '')}", f"- **Reasoning:** {result.verification.get('reason', '')}", ""]
    if result.evidence:
        L += ["## Evidence files", ""] + [f"- `{Path(e).name}`" for e in result.evidence] + [""]
    L += ["## Plan & success criteria", ""] + [f"- criterion: {c}" for c in result.plan.get("criteria", [])]
    L += [""] + [f"{i}. {p}" for i, p in enumerate(result.plan.get("plan", []), 1)] + ["", "## Decision trail", "",
          "| # | Why (agent's reasoning) | Action | Result |", "|---|---|---|---|"]
    for s in result.steps:
        cell = lambda t, n: t.replace("|", "\\|").replace("\n", " ")[:n]
        L.append(f"| {s.n} | {cell(s.thought, 220)} | `{s.tool}` {cell(str(s.args), 90)} | {'OK' if s.ok else 'ERR'}: {cell(brief(s.output), 140)} |")
    path = Path(agent.run_dir) / "report.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path
