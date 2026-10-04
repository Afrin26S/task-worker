"""The agent loop:  Goal -> Understand -> Plan -> [ Decide -> Gate -> Execute -> Observe -> Adapt ]* -> Verify -> Complete

Design notes (also explained in README):
  * State is explicit and rebuilt into the prompt every step (goal, criteria, plan, notes, memory, recent history),
    instead of relying on an ever-growing chat transcript. Easy to debug, easy to bound in size.
  * The LLM only *proposes* the next action as JSON. Everything with side effects passes through the policy gate in code.
  * Failures are observations, not exceptions: the agent sees them and adapts. Repetition / consecutive failures trigger
    warnings -> replanning -> escalation to the human, so it never loops forever.
  * "Done" is not self-declared: finish(completed) triggers an independent read-only VERIFIER pass that re-inspects the real system.
"""
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMError, extract_json
from .prompts import PLANNER_SYSTEM, REFLECT_SYSTEM, REPLAN_SYSTEM, STEP_SYSTEM, VERIFIER_SYSTEM
from .tools import ToolResult

FULL_OBS_CHARS = 3500     # most recent observations are shown (almost) in full
RECENT_FULL = 3           # how many recent steps keep their full observation
OLD_OBS_CHARS = 160       # older observations are compacted to one line
KEEP_FULL_TOOLS = {"read_file", "list_files", "note", "ask_user"}   # small, high-value facts: never compacted away
KEEP_FULL_MAX = 2500


class AgentError(RuntimeError):
    pass


def brief(output: str) -> str:
    """One-line console summary of an observation (for browser results: what happened + where we landed)."""
    lines = [l for l in (output or "").splitlines() if l.strip()]
    if not lines:
        return ""
    if len(lines) > 2 and lines[1].startswith("URL:") and lines[2].startswith("TITLE:"):
        return f"{lines[0]} | {lines[1][5:].strip()} | {lines[2]}"[:230]
    return lines[0][:230]


def _require_list(key):
    def check(o):
        if not isinstance(o.get(key), list):
            raise ValueError(f"need a '{key}' array")
    return check


@dataclass
class Step:
    n: int
    thought: str
    tool: str
    args: dict
    ok: bool
    output: str


@dataclass
class RunResult:
    outcome: str                      # completed | blocked | needs_human | unverified | incomplete | error
    summary: str
    verified: object = None           # True / False / None (not applicable)
    verification: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    run_dir: Path = None
    llm_calls: int = 0
    tokens: tuple = (0, 0)
    seconds: float = 0.0
    evidence: list = field(default_factory=list)
    approvals: int = 0


class Agent:
    def __init__(self, llm, tools, policy, human, memory, trace, secrets, notes, context_text="",
                 max_steps=50, verifier_steps=8, evidence_hook=None, reflect=True, run_id=""):
        self.llm, self.tools, self.policy, self.human = llm, tools, policy, human
        self.memory, self.trace, self.secrets, self.notes = memory, trace, secrets, notes
        self.context_text = context_text[:6000]
        self.max_steps, self.verifier_steps = max_steps, verifier_steps
        self.evidence_hook, self.reflect, self.run_id = evidence_hook, reflect, run_id
        self._approved = set()
        self.approvals = 0

    # ------------------------------------------------------------------ LLM helper with format repair
    def _ask_json(self, system, user, check):
        err = None
        for attempt in range(3):
            prompt = user if attempt == 0 else (
                f"{user}\n\nYOUR PREVIOUS REPLY WAS INVALID ({err}). Reply with ONLY one valid JSON object in the required format.")
            raw = self.llm.complete(system, prompt)
            try:
                obj = extract_json(raw)
                check(obj)
                return obj
            except ValueError as e:
                err = str(e)
                self.trace.event("format_error", error=err, raw=raw[:500])
                self.trace.say("warn", f"   (model reply invalid: {err}; retrying)")
        raise AgentError(f"model failed to produce a valid reply after 3 attempts: {err}")

    # ------------------------------------------------------------------ planning
    def _make_plan(self):
        user = f"REQUEST: {self.goal}\n\nCOMPANY CONTEXT:\n{self.context_text}\n\nLONG-TERM MEMORY:\n{self.memory.render() or '(empty)'}"

        def check(o):
            if not isinstance(o.get("plan"), list) or not isinstance(o.get("success_criteria"), list):
                raise ValueError("need 'plan' and 'success_criteria' arrays")

        try:
            plan = self._ask_json(PLANNER_SYSTEM, user, check)
        except AgentError:
            plan = {"restated_goal": self.goal, "success_criteria": ["The requested outcome is visibly achieved in the target system"],
                    "plan": ["Inspect sources and current system state", "Act", "Verify the result"], "ambiguities": []}
        self.criteria = [str(c) for c in plan["success_criteria"]]
        self.plan = [str(p) for p in plan["plan"]]
        self.trace.event("plan", **plan)
        self.trace.say("plan", f"PLAN: {plan.get('restated_goal', self.goal)}")
        for c in self.criteria:
            self.trace.say("plan", f"   success criterion: {c}")
        for i, p in enumerate(self.plan, 1):
            self.trace.say("plan", f"   {i}. {p}")
        return plan

    def _replan(self, reason):
        user = (f"GOAL: {self.goal}\nSUCCESS CRITERIA: {self.criteria}\nCURRENT PLAN: {self.plan}\nPROBLEM: {reason}\n"
                f"RECENT HISTORY:\n{self._history_text(force_compact=False)}")
        try:
            obj = self._ask_json(REPLAN_SYSTEM, user, _require_list("plan"))
        except AgentError:
            return
        self.plan = [str(p) for p in obj["plan"]]
        self.trace.event("replan", diagnosis=obj.get("diagnosis"), plan=self.plan)
        self.trace.say("warn", f"REPLAN ({obj.get('diagnosis', '')}): " + " | ".join(self.plan))

    # ------------------------------------------------------------------ prompt assembly
    def _history_text(self, force_compact=False):
        lines, n = [], len(self.history)
        for idx, s in enumerate(self.history):
            full = (n - idx) <= RECENT_FULL and not force_compact
            keep = s.tool in KEEP_FULL_TOOLS and len(s.output) <= KEEP_FULL_MAX    # e.g. the invoice text: never forget it
            if full or keep:
                out = s.output[:FULL_OBS_CHARS] + ("...[truncated]" if len(s.output) > FULL_OBS_CHARS else "")
                full = True
            else:
                out = brief(s.output)[:OLD_OBS_CHARS * 2]      # stale web page -> what happened + where we landed
            lines.append(f"[step {s.n}] thought: {s.thought[:300 if full else 120]}\n  action: {s.tool} {json.dumps(s.args)[:300]}\n"
                         f"  result ({'OK' if s.ok else 'ERROR'}): {out}")
        return "\n".join(lines) or "(no actions yet)"

    def _step_prompt(self, step):
        p = [f"GOAL: {self.goal}", "SUCCESS CRITERIA:\n" + "\n".join(f"- {c}" for c in self.criteria)]
        if self.context_text:
            p.append(f"COMPANY CONTEXT (procedures & facts):\n{self.context_text}")
        mem = self.memory.render()
        if mem:
            p.append(f"LONG-TERM MEMORY (learned in earlier runs):\n{mem}")
        if self.notes:
            p.append("RUN NOTES:\n" + "\n".join(f"- {k}: {v}" for k, v in self.notes.items()))
        p.append("CURRENT PLAN:\n" + "\n".join(f"{i}. {x}" for i, x in enumerate(self.plan, 1)))
        p.append("PROGRESS SO FAR (oldest first):\n" + self._history_text())
        if self.warnings:
            p.append("WARNINGS FROM THE RUNTIME:\n" + "\n".join(f"- {w}" for w in self.warnings))
            self.warnings = []
        p.append(f"This is step {step} of at most {self.max_steps}. Reply with the JSON object for your next action.")
        return "\n\n".join(p)

    # ------------------------------------------------------------------ gated execution
    def _get_approval(self, req):
        if req.key in self._approved:
            self.trace.event("approval", action=req.action, details=req.details, approved=True, reused=True)
            self.trace.say("approval", "   >> identical payload was already approved earlier in this run (reusing approval)")
            return True, ""
        self.trace.say("approval", f"   >> approval requested: {req.action}")
        ok, feedback = self.human.approve(req)
        self.trace.event("approval", action=req.action, details=req.details, approved=ok, feedback=feedback)
        self.trace.say("approval", f"   >> {'APPROVED' if ok else 'REJECTED'} by human" + (f": {feedback}" if feedback else ""))
        if ok:
            self._approved.add(req.key)
            self.approvals += 1
        return ok, feedback

    def _execute(self, name, args, verifying=False) -> ToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return ToolResult(False, f"unknown tool '{name}'")
        try:
            real_args = self.secrets.resolve(args)
        except KeyError as e:
            return ToolResult(False, str(e))
        clean, err = tool.validate(real_args)
        if err:
            return ToolResult(False, f"invalid arguments for {name}: {err}")
        if tool.precheck:
            try:
                decision = tool.precheck(clean)
            except Exception as e:
                return ToolResult(False, f"{type(e).__name__}: {e}")
            if decision and decision.blocked:
                self.trace.event("blocked", tool=name, reason=decision.blocked)
                return ToolResult(False, f"BLOCKED by policy: {decision.blocked}")
            if decision and decision.approval:
                if verifying:
                    return ToolResult(False, "Verification is read-only: actions that change data are not permitted here.")
                ok, feedback = self._get_approval(decision.approval)
                if not ok:
                    msg = "REJECTED by the human reviewer"
                    msg += f" with feedback: \"{feedback}\"." if feedback else "."
                    return ToolResult(False, msg + " Do not retry the same action; adapt, ask_user, or finish with outcome 'blocked'.")
        try:
            res = tool.fn(**clean)
        except Exception as e:  # tool failures are observations the agent can react to
            res = ToolResult(False, f"{type(e).__name__}: {e}")
        res.output = self.secrets.redact(res.output)
        return res

    # ------------------------------------------------------------------ main loop
    def run(self, goal: str) -> RunResult:
        t0 = time.time()
        self.goal, self.history, self.warnings = goal, [], []
        self.criteria, self.plan = [], []
        self.trace.event("goal", goal=goal)
        self.trace.say("goal", f"\nGOAL: {goal}")
        try:
            self._make_plan()
            result = self._loop()
        except (LLMError, AgentError) as e:
            self.trace.say("err", f"ERROR: {e}")
            self.trace.event("error", error=str(e))
            result = RunResult("error", f"Run aborted: {e}")
        result.plan = {"criteria": self.criteria, "plan": self.plan}
        result.steps = self.history
        result.llm_calls, result.tokens = self.llm.calls, (self.llm.input_tokens, self.llm.output_tokens)
        result.seconds = time.time() - t0
        result.approvals = self.approvals
        if self.evidence_hook:
            try:
                result.evidence = self.evidence_hook()
            except Exception as e:
                self.trace.event("evidence_error", error=str(e))
        if result.outcome == "completed" and result.verified and self.reflect:
            self._reflect(result)
        self.trace.event("result", outcome=result.outcome, verified=result.verified, summary=result.summary)
        self.trace.say("done", f"\nRESULT: {result.outcome.upper()}  verified={result.verified}\n{result.summary}")
        return result

    def _loop(self) -> RunResult:
        fails = replans = verify_fails = 0
        recent = []
        for step in range(1, self.max_steps + 1):
            allowed = set(self.tools.names()) | {"finish"}

            def check(o):
                a = o.get("action")
                if not isinstance(a, dict) or not isinstance(a.get("tool"), str):
                    raise ValueError("missing 'action': {\"tool\": ..., \"args\": {...}}")
                if a["tool"] not in allowed:
                    raise ValueError(f"unknown tool '{a['tool']}'. Valid tools: {sorted(allowed)}")
                if not isinstance(a.get("args", {}), dict):
                    raise ValueError("'args' must be a JSON object")

            d = self._ask_json(STEP_SYSTEM.format(tools=self.tools.describe()), self._step_prompt(step), check)
            thought = str(d.get("thought", ""))
            tool, args = d["action"]["tool"], d["action"].get("args", {}) or {}
            if isinstance(d.get("plan_update"), list) and d["plan_update"]:
                self.plan = [str(x) for x in d["plan_update"]]
                self.trace.event("plan_update", plan=self.plan)
                self.trace.say("plan", "   plan updated: " + " | ".join(self.plan))
            self.trace.say("thought", f"\n[{step}] {thought}")
            self.trace.say("tool", f"    -> {tool} {json.dumps(args)[:200]}")
            self.trace.event("decision", step=step, thought=thought, tool=tool, args=args)

            if tool == "finish":
                outcome, summary = str(args.get("outcome", "completed")), str(args.get("summary", ""))
                if outcome != "completed":
                    self.history.append(Step(step, thought, tool, args, True, f"finished with outcome {outcome}"))
                    return RunResult(outcome if outcome in ("blocked", "needs_human") else "blocked", summary, verified=None)
                v = self._verify(summary)
                self.trace.event("verification", **v)
                self.trace.say("verify", f"   VERIFIER: {'VERIFIED' if v['verified'] else 'NOT VERIFIED'} - {v['reason']}")
                if v["verified"]:
                    self.history.append(Step(step, thought, tool, args, True, "verified"))
                    return RunResult("completed", summary, verified=True, verification=v)
                verify_fails += 1
                self.history.append(Step(step, thought, tool, args, False,
                                         f"VERIFICATION FAILED: {v['reason']} Fix this and call finish again (or finish 'blocked' if impossible)."))
                if verify_fails >= 2:
                    return RunResult("unverified", summary, verified=False, verification=v)
                continue

            res = self._execute(tool, args)
            self.history.append(Step(step, thought, tool, args, res.ok, res.output))
            self.trace.event("observation", step=step, ok=res.ok, output=res.output[:2000])
            self.trace.say("ok" if res.ok else "err", f"    {'OK ' if res.ok else 'ERR'}: {brief(res.output)}")

            # --- reliability: detect failure streaks and repetition
            fails = 0 if res.ok else fails + 1
            sig = (tool, json.dumps(args, sort_keys=True))
            recent = (recent + [sig])[-3:]
            if len(recent) == 3 and len(set(recent)) == 1 and tool not in ("browser_snapshot",):
                self.warnings.append(f"You have issued the identical action '{tool}' 3 times in a row. Change your approach, ask_user, or finish 'blocked'.")
                recent = []
            if fails >= 4:
                if replans < 2:
                    replans += 1
                    self._replan(f"{fails} consecutive failed actions; last error: {res.output[:300]}")
                    self.warnings.append("You were failing repeatedly; the plan was revised. Re-read the current page/state before continuing.")
                    fails = 0
                else:
                    ans = self.human.ask(f"I'm stuck on: \"{self.goal}\". Last error: {res.output[:200]} How should I proceed? (or type 'stop')")
                    if ans is None or ans.strip().lower() == "stop":
                        return RunResult("blocked", f"Stuck after repeated failures and replanning. Last error: {res.output[:300]}", verified=None)
                    self.notes["human_guidance"] = ans
                    self.warnings.append(f"The human gave guidance: {ans}")
                    fails = replans = 0
        return RunResult("incomplete", f"Step budget ({self.max_steps}) exhausted before the goal was verified.", verified=False)

    # ------------------------------------------------------------------ independent verification
    def _verify(self, claim: str) -> dict:
        names = [n for n in self.tools.names() if n not in ("note", "remember", "ask_user")]
        vtools = self.tools.subset(names)
        vnames = set(names) | {"verdict"}
        system = VERIFIER_SYSTEM.format(tools=vtools.describe())
        actions = "\n".join(f"- step {s.n}: {s.tool} {json.dumps(s.args)[:160]} -> {'ok' if s.ok else 'error'}" for s in self.history if s.tool != "finish")
        base = (f"GOAL: {self.goal}\nSUCCESS CRITERIA:\n" + "\n".join(f"- {c}" for c in self.criteria) +
                f"\n\nCOMPANY CONTEXT:\n{self.context_text}\n\nWORKER'S CLAIM: {claim}\n\nWORKER ACTION LOG (NOT proof):\n{actions}\n")
        vh = []
        for i in range(1, self.verifier_steps + 1):
            def check(o):
                a = o.get("action")
                if not isinstance(a, dict) or a.get("tool") not in vnames:
                    raise ValueError(f"action.tool must be one of {sorted(vnames)}")
            hist = "\n".join(f"[v{n}] {t} {json.dumps(a)[:200]}\n  result: {(o if k >= len(vh) - 2 else o[:200])[:3500]}" for k, (n, t, a, o) in enumerate(vh)) or "(nothing inspected yet)"
            d = self._ask_json(system, f"{base}\nVERIFICATION PROGRESS:\n{hist}\n\nVerifier step {i} of {self.verifier_steps}. Reply with the JSON object.", check)
            tool, args = d["action"]["tool"], d["action"].get("args", {}) or {}
            self.trace.say("verify", f"   verifier -> {tool} {json.dumps(args)[:140]}")
            if tool == "verdict":
                v = args.get("verified")
                v = v if isinstance(v, bool) else str(v).lower() == "true"
                return {"verified": v, "evidence": str(args.get("evidence", "")), "reason": str(args.get("reason", ""))}
            res = self._execute(tool, args, verifying=True)
            vh.append((i, tool, args, res.output))
        return {"verified": False, "evidence": "", "reason": "verifier ran out of steps without reaching a verdict"}

    # ------------------------------------------------------------------ learning from outcomes
    def _reflect(self, result):
        try:
            trace = "\n".join(f"- {s.tool} {json.dumps(s.args)[:120]} -> {'ok' if s.ok else 'ERROR: ' + s.output[:150].replace(chr(10), ' ')}" for s in result.steps)
            obj = self._ask_json(REFLECT_SYSTEM, f"GOAL: {self.goal}\nSUMMARY: {result.summary}\nTRACE:\n{trace}", _require_list("learnings"))
            for item in obj["learnings"][:3]:
                if isinstance(item, dict) and item.get("key") and item.get("value"):
                    self.memory.add("learned:" + str(item["key"])[:40], self.secrets.redact(str(item["value"])), run=self.run_id)
                    self.trace.event("learned", **item)
        except (AgentError, LLMError):
            pass