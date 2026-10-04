"""Browser backends behind one small interface.

  PlaywrightBrowser : real Chromium (JS-capable, headed mode for demos, screenshots as evidence)
  HttpBrowser       : zero-dependency fallback (urllib + html.parser): same observation format

Both expose the page to the LLM as TEXT + a numbered list of interactive elements
(cheap, deterministic, no vision model needed):

    [4] select "Vendor" name=vendor value="" options=["-- choose --", "Northwind Traders"]
    [9] button "Save bill" (submits form POST /bills)
"""
import http.cookiejar
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

MAX_TEXT = 3000


class BrowserError(RuntimeError):
    pass


def format_snapshot(url, status, title, text, elements):
    lines = [f"URL: {url}  (HTTP {status})", f"TITLE: {title}", "--- PAGE TEXT ---"]
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    lines.append(text[:MAX_TEXT] + ("\n...[text truncated]" if len(text) > MAX_TEXT else ""))
    lines.append("--- INTERACTIVE ELEMENTS (use the [id] with browser_click / browser_type / browser_select) ---")
    for e in elements:
        k = e["kind"]
        label = e.get("label") or e.get("name") or ""
        if k == "link":
            lines.append(f'[{e["id"]}] link "{label}" -> {e.get("href", "")}')
        elif k == "button":
            tail = f' (submits form {e["method"].upper()} {e["action"]})' if e.get("submit") and e.get("action") is not None else ""
            lines.append(f'[{e["id"]}] button "{label}"{tail}')
        elif k == "select":
            lines.append(f'[{e["id"]}] select "{label}" name={e.get("name")} value="{e.get("value", "")}" options={e.get("options")}')
        else:
            val = "********" if e.get("type") == "password" and e.get("value") else e.get("value", "")
            lines.append(f'[{e["id"]}] {k}({e.get("type", "text")}) "{label}" name={e.get("name")} value="{val}"')
    if not elements:
        lines.append("(none)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- HTTP fallback
class _Parser(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "table", "ul", "ol", "header", "main", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.text = "", []
        self.elements, self.forms, self.labels = [], [], {}
        self._skip = 0
        self._in_title = False
        self._form = None
        self._label_for, self._label_buf = None, None
        self._cur = None      # element currently collecting text (a / button / textarea)
        self._select = None
        self._option = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "form":
            self.forms.append({"action": a.get("action", ""), "method": (a.get("method") or "get").lower(), "hidden": []})
            self._form = len(self.forms) - 1
        elif tag == "label":
            self._label_for, self._label_buf = a.get("for"), []
        elif tag == "input":
            t = (a.get("type") or "text").lower()
            if t == "hidden":
                if self._form is not None:
                    self.forms[self._form]["hidden"].append((a.get("name", ""), a.get("value", "")))
            elif t in ("submit", "button"):
                self.elements.append({"kind": "button", "label": a.get("aria-label") or a.get("value") or "Submit", "name": a.get("name"),
                                      "bvalue": a.get("value", ""), "submit": t == "submit", "form": self._form})
            else:
                self.elements.append({"kind": "input", "type": t, "html_id": a.get("id"), "name": a.get("name"), "value": a.get("value", ""),
                                      "label": a.get("aria-label"), "placeholder": a.get("placeholder"), "form": self._form})
        elif tag == "textarea":
            self._cur = {"kind": "textarea", "type": "text", "html_id": a.get("id"), "name": a.get("name"), "value": "",
                         "label": a.get("aria-label"), "form": self._form, "_buf": []}
            self.elements.append(self._cur)
        elif tag == "select":
            self._select = {"kind": "select", "html_id": a.get("id"), "name": a.get("name"), "options": [], "_vals": [], "value": "",
                            "label": a.get("aria-label"), "form": self._form}
            self.elements.append(self._select)
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value"), "text": [], "selected": "selected" in a}
        elif tag == "button":
            self._cur = {"kind": "button", "label": a.get("aria-label"), "name": a.get("name"), "bvalue": a.get("value", ""),
                         "submit": (a.get("type") or "submit").lower() == "submit", "form": self._form, "_buf": []}
            self.elements.append(self._cur)
        elif tag == "a" and a.get("href"):
            self._cur = {"kind": "link", "href": a["href"], "label": a.get("aria-label"), "form": None, "_buf": []}
            self.elements.append(self._cur)
        if tag in self.BLOCK:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "title":
            self._in_title = False
        elif tag == "form":
            self._form = None
        elif tag == "label":
            if self._label_for:
                self.labels[self._label_for] = "".join(self._label_buf).strip()
            self._label_for = self._label_buf = None
        elif tag == "option" and self._option is not None and self._select is not None:
            txt = "".join(self._option["text"]).strip()
            val = self._option["value"] if self._option["value"] is not None else txt
            self._select["options"].append(txt)
            self._select["_vals"].append(val)
            if self._option["selected"] or not self._select["value"] and len(self._select["options"]) == 1:
                self._select["value"] = val
            self._option = None
        elif tag == "select":
            self._select = None
        elif tag in ("a", "button", "textarea") and self._cur is not None:
            txt = " ".join("".join(self._cur.pop("_buf")).split())
            if self._cur["kind"] == "textarea":
                self._cur["value"] = txt
            elif not self._cur.get("label"):
                self._cur["label"] = txt
            self._cur = None
        elif tag in ("td", "th"):
            self.text.append(" | ")
        elif tag == "tr":
            self.text.append("\n")
        if tag in self.BLOCK:
            self.text.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
        if self._label_buf is not None:
            self._label_buf.append(data)
        if self._option is not None:
            self._option["text"].append(data)
            return
        if self._select is not None:
            return
        if self._cur is not None:
            self._cur["_buf"].append(data)
            if self._cur["kind"] == "textarea":
                return
        self.text.append(data)


class HttpBrowser:
    kind = "http"

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.url, self.status, self.title, self.text = "", 0, "", ""
        self.elements, self.forms, self.values = [], [], {}

    # -- plumbing
    def _fetch(self, method, url, data=None):
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data is not None else None, method=method)
        try:
            resp = self.opener.open(req, timeout=15)
        except urllib.error.HTTPError as e:
            resp = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise BrowserError(f"could not reach {url}: {getattr(e, 'reason', e)}")
        body = resp.read().decode(errors="replace")
        self.url = resp.geturl() if hasattr(resp, "geturl") else url
        self.status = getattr(resp, "status", None) or resp.getcode()
        p = _Parser()
        p.feed(body)
        self.title = " ".join(p.title.split())
        self.text = "".join(p.text).replace(" | \n", "\n")
        self.forms = p.forms
        self.elements = []
        self.values = {}
        for i, e in enumerate(p.elements, 1):
            e["id"] = i
            if e["kind"] in ("input", "textarea", "select") and not e.get("label"):
                e["label"] = p.labels.get(e.get("html_id")) or e.get("placeholder") or e.get("name")
            if e["kind"] == "select":
                e["value_text"] = e["value"]
            if e["kind"] == "link":
                e["href"] = urllib.parse.urljoin(self.url, e["href"])
            if e["kind"] == "button" and e.get("form") is not None:
                f = self.forms[e["form"]]
                e["method"], e["action"] = f["method"], urllib.parse.urljoin(self.url, f["action"] or self.url)
            self.values[i] = e.get("value", "")
            self.elements.append(e)

    def _el(self, i, kinds=None):
        for e in self.elements:
            if e["id"] == int(i):
                if kinds and e["kind"] not in kinds:
                    raise BrowserError(f"element [{i}] is a {e['kind']}, expected one of {sorted(kinds)}")
                return e
        raise BrowserError(f"no element with id [{i}] on the current page (ids change after every page load - take a fresh snapshot)")

    def _fields(self, e, mask=True):
        f = self.forms[e["form"]]
        data = {k: v for k, v in f["hidden"]}
        for x in self.elements:
            if x.get("form") == e["form"] and x["kind"] in ("input", "textarea", "select") and x.get("name"):
                data[x["name"]] = self.values[x["id"]]
        if e.get("name") and e.get("submit"):
            data[e["name"]] = e.get("bvalue", "")
        return data

    # -- interface
    def goto(self, url):
        self._fetch("GET", url)

    def click(self, i):
        e = self._el(i, {"link", "button"})
        if e["kind"] == "link":
            return self._fetch("GET", e["href"])
        if not e.get("submit") or e.get("form") is None:
            raise BrowserError(f"button [{i}] does not submit a form")
        data = self._fields(e)
        if e["method"] == "get":
            sep = "&" if "?" in e["action"] else "?"
            return self._fetch("GET", e["action"] + sep + urllib.parse.urlencode(data))
        self._fetch("POST", e["action"], data)

    def type(self, i, text):
        e = self._el(i, {"input", "textarea"})
        self.values[e["id"]] = text

    def select(self, i, option):
        e = self._el(i, {"select"})
        vals = e["_vals"]
        for cand in (lambda a, b: a == option or b == option, lambda a, b: a.lower() == option.lower() or b.lower() == option.lower()):
            for txt, val in zip(e["options"], vals):
                if cand(txt, val):
                    self.values[e["id"]] = val
                    return
        raise BrowserError(f"option '{option}' not found; available options: {e['options']}")

    def click_info(self, i):
        e = self._el(i, {"link", "button", "input", "textarea", "select"})
        info = {"kind": e["kind"], "label": e.get("label", ""), "submit": bool(e.get("submit")), "href": e.get("href")}
        if e["kind"] == "button" and e.get("submit") and e.get("form") is not None:
            masked = {}
            for x in self.elements:
                if x.get("form") == e["form"] and x["kind"] in ("input", "textarea", "select") and x.get("name"):
                    masked[x["name"]] = "********" if x.get("type") == "password" else self.values[x["id"]]
            info.update(method=e["method"], action=e["action"], fields=masked)
        return info

    def snapshot(self):
        els = [dict(e, value=self.values.get(e["id"], e.get("value", ""))) for e in self.elements]
        return format_snapshot(self.url, self.status, self.title, self.text, els)

    def capture_evidence(self, directory, name):
        path = directory / f"{name}.txt"
        path.write_text(self.snapshot())
        return str(path)

    def close(self):
        pass


# --------------------------------------------------------------------------- Playwright
_JS_COLLECT = """() => {
  const out = []; let id = 0;
  document.querySelectorAll('a[href],input,select,textarea,button').forEach(el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') return;
    if (el.tagName === 'INPUT' && el.type === 'hidden') return;
    id += 1; el.setAttribute('data-agent-id', String(id));
    const tag = el.tagName.toLowerCase();
    const lab = (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute('aria-label') || el.placeholder || '';
    const d = {id, label: (el.getAttribute('aria-label') || lab || el.innerText || el.value || el.name || '').trim().replace(/\\s+/g, ' ')};
    if (tag === 'a') { d.kind = 'link'; d.href = el.href; }
    else if (tag === 'select') { d.kind = 'select'; d.name = el.name; d.value = el.value; d.options = Array.from(el.options).map(o => o.text.trim()); d.label = (lab || el.name).trim(); }
    else if (tag === 'button' || (tag === 'input' && ['submit','button'].includes(el.type))) {
      d.kind = 'button'; d.submit = !!el.form && (el.type === 'submit' || (tag === 'button' && !el.getAttribute('type')));
      if (el.form) { d.method = (el.form.method || 'get'); d.action = el.form.action; }
      if (tag === 'input') d.label = (el.getAttribute('aria-label') || el.value || 'Submit');
    }
    else { d.kind = tag === 'textarea' ? 'textarea' : 'input'; d.type = el.type || 'text'; d.name = el.name; d.value = el.value; d.label = (lab || el.name).trim(); }
    out.push(d);
  });
  return {elements: out, text: document.body ? document.body.innerText : ''};
}"""

_JS_INFO = """(el) => {
  const tag = el.tagName.toLowerCase();
  const info = {kind: tag === 'a' ? 'link' : (tag === 'button' || el.type === 'submit' ? 'button' : tag),
                label: (el.getAttribute('aria-label') || el.innerText || el.value || '').trim(), href: el.href || null, submit: false};
  if (el.form && (el.type === 'submit' || (tag === 'button' && !el.getAttribute('type')))) {
    info.submit = true; info.method = el.form.method || 'get'; info.action = el.form.action; info.fields = {};
    new FormData(el.form).forEach((v, k) => { const f = el.form.elements[k]; info.fields[k] = (f && f.type === 'password') ? '********' : String(v); });
  }
  return info;
}"""


class PlaywrightBrowser:
    kind = "playwright"

    def __init__(self, headed=False, slow_mo=0):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=not headed, slow_mo=slow_mo)
        except Exception:
            self._pw.stop()
            raise
        self.page = self._browser.new_context(viewport={"width": 1280, "height": 900}).new_page()
        self.page.set_default_timeout(8000)
        self.status = 0
        self.page.on("response", lambda r: setattr(self, "status", r.status) if r.request.is_navigation_request() else None)

    def _sel(self, i):
        return f'[data-agent-id="{int(i)}"]'

    def _guard(self, fn):
        try:
            return fn()
        except Exception as e:  # playwright errors -> concise BrowserError
            raise BrowserError(str(e).splitlines()[0][:300])

    def goto(self, url):
        self._guard(lambda: self.page.goto(url, wait_until="domcontentloaded"))

    def click(self, i):
        def go():
            self.page.click(self._sel(i))
            self.page.wait_for_load_state("domcontentloaded")
        self._guard(go)

    def type(self, i, text):
        self._guard(lambda: self.page.fill(self._sel(i), text))

    def select(self, i, option):
        def go():
            try:
                self.page.select_option(self._sel(i), label=option)
            except Exception:
                try:
                    self.page.select_option(self._sel(i), value=option)
                except Exception:
                    opts = self.page.eval_on_selector(self._sel(i), "e => Array.from(e.options).map(o => o.text.trim())")
                    raise BrowserError(f"option '{option}' not found; available options: {opts}")
        self._guard(go)

    def click_info(self, i):
        return self._guard(lambda: self.page.eval_on_selector(self._sel(i), _JS_INFO))

    def snapshot(self):
        data = self._guard(lambda: self.page.evaluate(_JS_COLLECT))
        return format_snapshot(self.page.url, self.status, self.page.title(), data["text"], data["elements"])

    def capture_evidence(self, directory, name):
        path = directory / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return str(path)

    def close(self):
        try:
            self._browser.close()
        finally:
            self._pw.stop()


def make_browser(kind="auto", headed=False, slow_mo=0):
    if kind in ("playwright", "auto"):
        try:
            return PlaywrightBrowser(headed=headed, slow_mo=slow_mo)
        except Exception as e:
            if kind == "playwright":
                raise
            print(f"[browser] Playwright unavailable ({str(e).splitlines()[0][:80]}); falling back to the HTTP browser.")
    return HttpBrowser()
