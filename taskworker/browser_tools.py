"""Browser tools exposed to the agent. The backend is created lazily (file-only tasks never start a browser)."""
from .browser import BrowserError
from .policy import Decision
from .tools import Param, Tool, ToolResult


class BrowserSession:
    def __init__(self, factory):
        self._factory, self._b = factory, None

    @property
    def started(self):
        return self._b is not None

    def get(self):
        if self._b is None:
            self._b = self._factory()
        return self._b

    def close(self):
        if self._b is not None:
            self._b.close()
            self._b = None


def build_browser_tools(session: BrowserSession, policy):
    def result(action_msg):
        return ToolResult(True, f"{action_msg}\n{session.get().snapshot()}")

    def goto(url):
        session.get().goto(url)
        return result(f"Navigated to {url}")

    def goto_check(args):
        reason = policy.check_url(args["url"])
        return Decision(blocked=reason) if reason else None

    def snapshot():
        return result("Current page")

    def click(id):
        session.get().click(id)
        return result(f"Clicked element [{id}]")

    def click_check(args):
        return policy.check_click(session.get().click_info(args["id"]))

    def type_(id, text):
        session.get().type(id, text)
        return result(f"Typed into element [{id}]")

    def select(id, option):
        session.get().select(id, option)
        return result(f"Selected '{option}' in element [{id}]")

    return [
        Tool("browser_goto", "Open a URL in the browser.", [Param("url", "string")], goto, goto_check),
        Tool("browser_snapshot", "Re-read the current page (text + numbered interactive elements).", [], snapshot),
        Tool("browser_click", "Click a link/button by its [id]. Submitting forms that change data requires human approval (handled automatically).",
             [Param("id", "int", "element id from the latest snapshot")], click, click_check),
        Tool("browser_type", "Type text into an input/textarea by [id] (replaces existing text).",
             [Param("id", "int"), Param("text", "string")], type_),
        Tool("browser_select", "Choose an option (by visible text or value) in a <select> by [id].",
             [Param("id", "int"), Param("option", "string")], select),
    ]


BROWSER_ERRORS = (BrowserError,)
