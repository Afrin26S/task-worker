# Autonomous AI Task Worker

A small, **general** autonomous worker: you give it a task in plain language, it works out the steps, uses a **real browser and files**,
observes what happens, recovers from failures, asks a human before changing anything, and finally has an **independent verifier**
re-inspect the real system before it is allowed to say "done".

```
python run.py "Find the latest invoice from Northwind Traders, extract the amount and due date,
               enter it into our internal system, and tell me once it is done."
```

> Built for the CentrAlign AI *AI Engineering Intern* assignment (problem: "Autonomous AI Task Worker").

---

## 1. What it does (demo scenario)

The sandbox is a fake company:

| Piece | What it is |
|---|---|
| `sandbox/inbox/` | Messy vendor emails: invoices, a **payment reminder** (newest file, *not* an invoice), a price list, an invoice with only "Net 30" terms, an INR invoice with thousands separators, an invoice that is already in the ERP |
| `mock_erp/` | "Acme Payables": login, bills list, strict new-bill form, duplicate protection, "mark paid". **Chaos mode** injects real-world failures: the 1st valid save silently **expires the session**, the 2nd returns **HTTP 503** |
| `sandbox/company_context.md` | The company's AP procedure (what a short request leaves unstated: check duplicates, total incl. tax, plain-number amounts, compute due date from terms, never mark paid unless asked, ...) |

For the headline task the agent must: find the right invoice among distractors → read it → compute `12 Sep + Net 30 = 2026-10-12` →
pick the *total payable* (1,284.50, not the subtotal) → check the ERP for a duplicate → log in → fill the form (amount **without comma**) →
get **approval** from the human → survive the expired session and the 503 → confirm the bill is really in the list → report back.

Different tasks run through the **same unchanged code** (see `eval/`): enter an invoice in another currency, *refuse to duplicate* an invoice
that's already entered, answer a read-only reporting question, mark a bill paid, and *not fabricate* anything when the source doesn't exist.

## 2. Setup & run

Python 3.9+. The agent core uses **only the standard library**; Playwright is recommended for the real-browser backend.

```bash
git clone <this-repo> && cd task-worker
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium                              # real browser (optional: without it an HTTP backend is used automatically)

cp .env.example .env                                     # then put your API key in .env  (Anthropic, or any OpenAI-compatible provider)
```

```bash
# Interactive run, visible browser (best for the demo video). You will be asked to approve writes in the terminal.
python run.py "Find the latest invoice from Northwind Traders, extract the amount and due date, enter it into our internal system, and tell me once it is done." --headed --slow-mo 300

python run.py "Enter Initech's newest invoice into the ERP."
python run.py "Please enter the latest invoice from Globex Corp into the ERP."          # already entered -> it must NOT duplicate
python run.py "Which bills in the ERP are still unpaid and due before 10 October 2026? Give me the total amount per currency."
python run.py "<task>" --auto-approve --no-chaos --browser http                        # non-interactive / no failures / no browser install
python run.py --serve-only                                                              # just start the mock ERP (http://127.0.0.1:8800, demo / demo123)
```

Every run writes `runs/<timestamp>-<task>/` containing `report.md` (outcome, verifier evidence, full decision trail with the agent's reasoning),
`trace.jsonl` (machine-readable event log) and `final_page_state.png` (screenshot evidence).

**Tests (no API key needed)** and **evaluation (needs an LLM key)**:

```bash
python -m unittest discover -s tests -t . -v     # 18 tests
python -m eval.run_eval                          # 6 different tasks, graded against the ERP's ground truth -> eval/results.md
```

## 3. Architecture

```
                         ┌────────────────────────── Agent loop (taskworker/agent.py) ──────────────────────────┐
 user task ─► PLANNER ─► │  DECIDE (LLM proposes ONE tool call as JSON)                                          │
 (+company context,      │     │                                                                                 │
  +long-term memory)     │     ▼                                                                                 │
  → goal, success        │  GATE  (policy.py: URL allow-list, blocked paths, approval for data-changing submits) │──► human
  criteria, plan         │     │            ▲ rejected / blocked → returned to the LLM as an observation         │   approve / answer
                         │     ▼            │                                                                   │
                         │  EXECUTE (tool registry: files · browser · notes · memory · ask_user)                 │
                         │     │                                                                                 │
                         │     ▼                                                                                 │
                         │  OBSERVE → compact state (history, notes, plan) → failure/repeat detector             │
                         │     │        3× same action → warning · 4 failures → REPLAN · still stuck → ask human │
                         │     ▼                                                                                 │
                         │  finish(completed) ──► VERIFIER (separate pass, read-only, re-inspects the real system)│
                         │        not verified → sent back to the agent with the reason (max 2 times)            │
                         └───────────────────────────────────────────────────────────────────────────────────────┘
                                         │ verified
                                         ▼
                      report.md + trace.jsonl + screenshot · REFLECT → durable learnings saved to memory
```

| Module | Responsibility |
|---|---|
| `taskworker/agent.py` | The loop, planning/replanning, failure handling, verification, reflection |
| `taskworker/tools.py`, `core_tools.py`, `browser_tools.py` | Tool abstraction (name, typed params, optional pre-check) and the built-in tools |
| `taskworker/browser.py` | Two interchangeable browser backends with an identical text observation format |
| `taskworker/policy.py` | Permissions & approval rules, enforced in code (not by the prompt) |
| `taskworker/vault.py` | Secret placeholders — the LLM never sees credentials |
| `taskworker/memory.py` | Persistent company memory (JSON) |
| `taskworker/llm.py` | Provider-agnostic LLM client (stdlib HTTP, retries/backoff) |
| `taskworker/prompts.py` | All prompts — **none mention invoices or any specific app** |
| `mock_erp/`, `sandbox/` | The simulated company environment |

## 4. Key design decisions (and why)

1. **One loop, task-agnostic.** Nothing in `agent.py` or `prompts.py` knows about invoices. Task knowledge enters only through
   (a) the user's request, (b) `company_context.md` (procedures — what a short request leaves unstated), (c) persistent memory, and (d) observations.
   New capabilities = register a `Tool`; new company workflows = edit the context. This is the generalization story.
2. **The LLM proposes, code disposes.** The model returns one JSON action per step. Everything with side effects goes through `Policy` first:
   host allow-list, restricted paths (the ERP's `/_debug/*` ground-truth endpoints are blocked), and **human approval for any form submission that changes data**
   (the approval shows the *exact field values* about to be sent; login is exempt; an identical already-approved payload is not re-asked).
   A rejection is fed back as an observation — the agent adapts instead of crashing.
3. **Verification is independent, not self-reported.** `finish(completed)` doesn't end the run. A *separate* verifier pass — different prompt, **read-only**
   (any data-changing action is refused), told explicitly not to trust the worker's claim — re-opens the real system and must cite what it saw against the
   success criteria that the planner wrote *before* any action. Failed verification goes back to the agent with the reason. This is what separates
   "I clicked save" from "the bill exists with the right values".
4. **Failures are observations, not exceptions.** Tool errors, HTTP 422/503, validation messages and expired sessions all come back as text the model reacts to.
   The runtime adds a safety net so it can never loop forever: identical action ×3 → warning; 4 consecutive failures → forced **replan**; still stuck → **ask the human**; step budget → honest "incomplete".
5. **Observation = text + numbered interactive elements** (`[7] button "Save bill" (submits form POST /bills)`), not screenshots. It's cheap, deterministic, works with any model
   (no vision needed), makes the approval gate precise, and is identical across backends. Screenshots are still captured as *evidence*.
6. **Two browser backends behind one interface.** Playwright/Chromium (real browser, JS) and a zero-dependency HTTP backend. The same test-suite runs against both, so the abstraction is proven rather than assumed.
7. **Explicit, bounded state instead of an ever-growing chat.** Each step the prompt is rebuilt from: goal, success criteria, company context, memory, run notes, current plan, and history
   (last 3 observations in full, older ones compacted to one line). Easy to debug, cost stays flat as runs get long, and it makes "why did the agent do X?" answerable from the trace (every step stores its `thought`).
8. **Secrets never reach the model.** The context says `password: {{secret:ERP_PASSWORD}}`; the model types the placeholder; the tool layer substitutes the value and redacts it from observations, traces and memory. A test asserts the password appears in no prompt and no log.
9. **Learning from outcomes.** After a *verified* run, a reflection step extracts ≤3 durable facts ("the ERP session can expire silently; re-login and re-enter the form") into `.agent_memory/memory.json`, which later runs see.
   Only verified runs can write memory, so failures don't poison it.
10. **JSON-action protocol instead of native tool-calling.** Slightly less elegant than provider tool-calling APIs, but it works unchanged across Anthropic, OpenAI-compatible providers (Gemini, Groq, local models),
    and lets the runtime validate/repair malformed replies (up to 3 attempts with the parse error fed back).

## 5. How this maps to the evaluation criteria

| Criterion | Where to look |
|---|---|
| Autonomy | The model chooses every step from goal + procedures; nothing task-specific is scripted |
| Execution | Real browser actions and file reads; the ERP's state actually changes (checked by the eval harness) |
| Reliability | Chaos mode (session expiry, 503), validation errors, repeat/failure detection, replan, escalate; tests `test_happy_path_recovers_...` |
| Verification | Independent read-only verifier + success criteria defined up front; test `test_false_completion_claim_is_caught_...` |
| Generalization | `eval/run_eval.py`: 6 different tasks, zero code changes between them |
| Engineering quality | Small modules, typed tool params, policy as code, 18 tests (incl. the real HTTP LLM client and the CLI end-to-end) |
| Product thinking | Approval shows exact payload; "do nothing + explain" is a first-class successful outcome (duplicate case); evidence report for the human |
| Technical understanding | This document + `trace.jsonl` / `report.md` decision trail |

## 6. Testing

`tests/` drives the **real** agent loop, tools, policy, browsers and ERP with a *scripted stand-in for the LLM* (so they're deterministic and need no API key):

* happy path through **all three injected faults** (bad amount format → expired session → 503 → success), on **both** browser backends; exactly one bill, correct values, approvals requested, secret never in prompt/trace
* human rejection is respected (nothing written) · policy blocks restricted paths & foreign hosts · path traversal blocked
* a **false "completed" claim is caught** by the verifier · malformed model output is repaired · repetition triggers a warning
* the real HTTP LLM client (parsing, token accounting, 429 retry) against a fake server · the full **CLI end-to-end** as a subprocess

These prove the *machinery*. How well a given **model** drives it is measured by `python -m eval.run_eval` (results in `eval/results.md`).

## 7. Models, APIs, frameworks, external services

* **LLM:** any. Default Anthropic Messages API (`claude-sonnet-5-5`, configurable via `LLM_MODEL`); or any OpenAI-compatible `/chat/completions` endpoint (OpenAI, Gemini's OpenAI-compat endpoint, Groq, OpenRouter, Ollama). Called with plain `urllib` — no SDK.
* **Browser automation:** [Playwright](https://playwright.dev/python/) (Chromium), plus a stdlib fallback (`urllib` + `html.parser`).
* **Optional:** `pypdf` to read PDF invoices.
* **No agent framework** (LangChain etc.) on purpose: the loop is ~350 lines and every decision in it is explainable.
* The ERP, inbox and company are **entirely simulated** — no real credentials, systems or data.
AI tools disclosure: I built this with the help of AI coding assistants (Claude, plus others for debugging). I wrote and reviewed the design, and I can explain, debug and modify every module.

## 8. Known limitations

* **Prototype scale:** one browser tab, sequential steps, one task at a time; no persistent job queue, scheduling or background execution.
* **Text observation can't see visual-only UI** (canvas, icon-only buttons without labels, CAPTCHAs); no desktop-application control, only web + files.
* **Approval policy is rule-based** (POST submissions outside a safe-list); it doesn't understand *risk by content* (e.g. amount thresholds).
* **Verifier uses the same underlying model** as the worker, so it can share blind spots (it does re-inspect the real system, which catches false claims but not misunderstandings of the task itself).
* **Reflection may learn a wrong lesson** (mitigated by only learning from verified runs and keeping ≤3 facts per run; no confidence scoring or expiry yet).
* **Context window:** old observations are truncated, not summarized by a model.
* **Eval is small** (6 tasks, single run each); LLM runs are non-deterministic, so results vary between runs.
* The HTTP fallback backend doesn't execute JavaScript.

## 9. What I would build next

1. **Durable execution:** task queue + persisted run state so a run survives crashes, with retries/schedules and a web dashboard for approvals (Slack/email approval instead of terminal).
2. **Risk-aware permissions:** per-tool/per-system scopes, amount thresholds, dry-run mode that shows the diff before committing, immutable audit log.
3. **Connectors over clicking:** prefer an API/connector when one exists, fall back to the browser (the `Tool` interface already supports this); desktop control via an OS-level computer-use backend.
4. **A stronger verifier:** a different model, plus programmatic checks (e.g. read back via API) where available; calibrate with a bigger eval suite and failure-injection fuzzing (random latency, layout changes, popups).
5. **Procedural memory with provenance:** store learnings as reusable, versioned "skills" with success statistics, expiry and human review; per-company memory isolation.
6. **Sub-agents & parallelism** for multi-system tasks, and cost/latency optimization (smaller model for routine steps, larger for planning/verification).

## 10. Assumptions

* The requester is an AP employee at the (fictional) Acme Corp; the handbook in `sandbox/company_context.md` is available to the agent as "company knowledge".
* "Latest invoice" means the invoice with the most recent invoice date from that vendor (reminders/statements/price lists don't count).
* Sandbox login credentials are given to the agent through the secret mechanism, as a real deployment would via a vault.
* The demo's current date is early October 2026, so "due before 10 October" in the eval is evaluated against the seeded data.

## Demo run notes
- Live demo video: <paste YouTube link here>
- Models used in the demo: Groq OpenAI-compatible API (openai/gpt-oss-120b, fallback qwen/qwen3.8-27b). Earlier runs used Google Gemini Flash models until the free-tier daily quota ran out.
- In the recorded run the agent found the latest Northwind invoice (INV-2057), entered it into the mock ERP, recovered from the injected session expiry and the 503 error, and saved the bill (BILL-0006).
- Known issue in this recording: the run then aborted at the final independent-verification step, because the fallback model exceeded Groq's free-tier tokens-per-minute limit (HTTP 413). The verifier logic itself is covered by the tests in 	ests/.
