"""Human-in-the-loop interfaces: approvals and clarification questions."""
import sys

from .policy import ApprovalRequest


class Human:
    def ask(self, question: str):
        raise NotImplementedError

    def approve(self, req: ApprovalRequest):
        """Returns (approved: bool, feedback: str)."""
        raise NotImplementedError


class CLIHuman(Human):
    def ask(self, question):
        if not sys.stdin.isatty():
            return None
        print(f"\n\033[1;33m? AGENT NEEDS INPUT:\033[0m {question}")
        return input("  your answer > ").strip()

    def approve(self, req):
        if not sys.stdin.isatty():
            return False, "no interactive human available to approve"
        print("\n\033[1;33m========== APPROVAL REQUIRED ==========\033[0m")
        print(f"  Action : {req.action}")
        for k, v in req.details.items():
            print(f"    {k:<16} = {v}")
        ans = input("  Approve? [y = yes / n = no / or type feedback] > ").strip()
        if ans.lower() in ("y", "yes"):
            return True, ""
        return False, "" if ans.lower() in ("", "n", "no") else ans


class AutoHuman(Human):
    """Non-interactive: approves everything (sandbox/eval use) and answers questions from a script."""

    def __init__(self, auto_approve=True, answers=None):
        self.auto_approve = auto_approve
        self.answers = list(answers or [])
        self.questions, self.approvals = [], []

    def ask(self, question):
        self.questions.append(question)
        return self.answers.pop(0) if self.answers else None

    def approve(self, req):
        self.approvals.append(req)
        return (True, "") if self.auto_approve else (False, "auto-rejected")
