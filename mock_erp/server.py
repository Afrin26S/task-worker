"""Mock company ERP ("Acme Payables") used as the agent's sandbox environment.

Stdlib only. It deliberately behaves like a real internal tool:
  * login + session cookie
  * a bills list and a "new bill" form with strict validation
  * duplicate protection (vendor + invoice number)
  * a "Mark paid" action
  * CHAOS MODE (on by default): the first two *valid* bill submissions fail
      1st -> the session silently expires (redirect to login, nothing saved)
      2nd -> HTTP 503 "temporarily unavailable" (nothing saved)
    so the agent must notice, recover and re-verify instead of assuming success.
  * /_debug/* endpoints exist ONLY for the evaluation harness (ground truth);
    the agent's policy layer blocks them.
"""
import html
import json
import re
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

USERS = {"demo": "demo123"}
VENDORS = ["Northwind Traders", "Globex Corp", "Initech Ltd", "Umbrella Supplies"]
CURRENCIES = ["USD", "EUR", "INR"]

SEED = [
    dict(vendor="Northwind Traders", invoice_number="INV-2041", amount="980.00", currency="USD", due_date="2026-09-13", status="Pending"),
    dict(vendor="Globex Corp", invoice_number="INV-7710", amount="450.00", currency="USD", due_date="2026-10-05", status="Pending"),
    dict(vendor="Umbrella Supplies", invoice_number="INV-5530", amount="2300.00", currency="EUR", due_date="2026-10-09", status="Pending"),
    dict(vendor="Initech Ltd", invoice_number="INV-0088", amount="41000.00", currency="INR", due_date="2026-10-03", status="Paid"),
    dict(vendor="Globex Corp", invoice_number="INV-7655", amount="1200.00", currency="USD", due_date="2026-11-01", status="Pending"),
]

CSS = """body{font-family:system-ui,sans-serif;margin:0;color:#1b1f24}header{background:#12344d;color:#fff;padding:12px 24px;display:flex;gap:24px;align-items:center}
header a{color:#cfe8ff;text-decoration:none}main{padding:24px;max-width:980px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #d0d7de;padding:6px 10px;text-align:left}
th{background:#f3f6f9}label{display:block;margin-top:12px;font-weight:600}input,select,textarea{padding:6px;min-width:320px}button{margin-top:16px;padding:8px 16px}
.flash{background:#e6f4ea;border:1px solid #9bd3ae;padding:8px 12px;margin-bottom:12px}.error{background:#fdecea;border:1px solid #f5a8a0;padding:8px 12px;margin-bottom:12px}"""


class State:
    def __init__(self, chaos=True):
        self.chaos = chaos
        self.reset(chaos)

    def reset(self, chaos=None):
        if chaos is not None:
            self.chaos = chaos
        self.bills = [{"id": f"BILL-{i:04d}", **b} for i, b in enumerate(SEED, 1)]
        self.sessions = {}
        self.valid_submissions = 0

    def next_id(self):
        return f"BILL-{len(self.bills) + 1:04d}"


def layout(title, body, user=None):
    nav = ""
    if user:
        nav = '<a href="/bills">Bills</a><a href="/bills/new">New bill</a><a href="/logout">Log out</a>'
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title>"
            f"<style>{CSS}</style></head><body><header><strong>Acme Payables</strong>{nav}</header>"
            f"<main><h1>{html.escape(title)}</h1>{body}</main></body></html>")


def login_form(nxt="/bills", msg=None):
    err = f'<div class="error" role="alert">{html.escape(msg)}</div>' if msg else ""
    return err + f"""<form method="post" action="/login">
<input type="hidden" name="next" value="{html.escape(nxt)}">
<label for="username">Username</label><input id="username" name="username" type="text">
<label for="password">Password</label><input id="password" name="password" type="password">
<button type="submit">Sign in</button></form>"""


def bill_form(values=None, errors=None):
    v = values or {}
    err = ""
    if errors:
        err = '<div class="error" role="alert"><strong>Please fix the following:</strong><ul>' + "".join(
            f"<li>{html.escape(e)}</li>" for e in errors) + "</ul></div>"
    vend = '<option value="">-- choose --</option>' + "".join(
        f'<option value="{html.escape(x)}"{" selected" if v.get("vendor") == x else ""}>{html.escape(x)}</option>' for x in VENDORS)
    cur = "".join(f'<option value="{c}"{" selected" if v.get("currency", "USD") == c else ""}>{c}</option>' for c in CURRENCIES)
    return err + f"""<form method="post" action="/bills">
<label for="vendor">Vendor</label><select id="vendor" name="vendor">{vend}</select>
<label for="invoice_number">Invoice number</label><input id="invoice_number" name="invoice_number" type="text" value="{html.escape(v.get('invoice_number', ''))}">
<label for="amount">Amount (plain number, e.g. 1250.00)</label><input id="amount" name="amount" type="text" value="{html.escape(v.get('amount', ''))}">
<label for="currency">Currency</label><select id="currency" name="currency">{cur}</select>
<label for="due_date">Due date (YYYY-MM-DD)</label><input id="due_date" name="due_date" type="text" placeholder="YYYY-MM-DD" value="{html.escape(v.get('due_date', ''))}">
<label for="notes">Notes (optional)</label><textarea id="notes" name="notes">{html.escape(v.get('notes', ''))}</textarea>
<button type="submit">Save bill</button></form>"""


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AcmePayables/1.0"

        def log_message(self, *a):
            pass

        # ---- helpers
        def _sid(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "sid":
                    return v
            return None

        def _user(self):
            return state.sessions.get(self._sid())

        def _send(self, body, status=200, ctype="text/html; charset=utf-8", headers=None):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, to, headers=None):
            self.send_response(302)
            self.send_header("Location", to)
            self.send_header("Content-Length", "0")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()

        def _form(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode() if n else ""
            return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        def _need_login(self, nxt):
            self._redirect(f"/login?msg={quote('Please log in to continue.')}&next={quote(nxt)}")

        # ---- routes
        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/":
                return self._redirect("/bills")
            if u.path == "/login":
                return self._send(layout("Sign in", login_form(q.get("next", "/bills"), q.get("msg"))))
            if u.path == "/logout":
                state.sessions.pop(self._sid(), None)
                return self._redirect("/login")
            if u.path == "/_debug/state":
                return self._send(json.dumps({"bills": state.bills}), ctype="application/json")
            user = self._user()
            if u.path == "/bills":
                if not user:
                    return self._need_login("/bills")
                rows = ""
                for b in state.bills:
                    act = ""
                    if b["status"] != "Paid":
                        act = (f'<form method="post" action="/bills/{b["id"]}/pay"><button type="submit" '
                               f'aria-label="Mark {html.escape(b["invoice_number"])} as paid">Mark paid</button></form>')
                    rows += (f"<tr><td>{b['id']}</td><td>{html.escape(b['vendor'])}</td><td>{html.escape(b['invoice_number'])}</td>"
                             f"<td>{b['amount']}</td><td>{b['currency']}</td><td>{b['due_date']}</td><td>{b['status']}</td><td>{act}</td></tr>")
                flash = f'<div class="flash" role="status">{html.escape(q["flash"])}</div>' if q.get("flash") else ""
                table = ("<table><thead><tr><th>ID</th><th>Vendor</th><th>Invoice #</th><th>Amount</th><th>Currency</th>"
                         f"<th>Due date</th><th>Status</th><th>Action</th></tr></thead><tbody>{rows}</tbody></table>")
                return self._send(layout("Bills", flash + table, user))
            if u.path == "/bills/new":
                if not user:
                    return self._need_login("/bills/new")
                return self._send(layout("New bill", bill_form(), user))
            self._send(layout("Not found", "<p>No such page.</p>", user), status=404)

        def do_POST(self):
            u = urlparse(self.path)
            f = self._form()
            if u.path == "/_debug/reset":
                state.reset(chaos=f.get("chaos", "1") == "1")
                return self._send("ok", ctype="text/plain")
            if u.path == "/login":
                if USERS.get(f.get("username")) == f.get("password"):
                    sid = secrets.token_hex(12)
                    state.sessions[sid] = f["username"]
                    nxt = f.get("next") or "/bills"
                    nxt = nxt if nxt.startswith("/") else "/bills"
                    return self._redirect(nxt, {"Set-Cookie": f"sid={sid}; Path=/; HttpOnly"})
                return self._send(layout("Sign in", login_form("/bills", "Invalid username or password.")), status=401)
            user = self._user()
            if u.path == "/bills":
                if not user:
                    return self._need_login("/bills/new")
                errors = []
                vendor = f.get("vendor", "")
                inv = f.get("invoice_number", "").strip()
                amount = f.get("amount", "").strip()
                due = f.get("due_date", "").strip()
                cur = f.get("currency", "")
                if vendor not in VENDORS:
                    errors.append("Vendor is required.")
                if not re.fullmatch(r"[A-Za-z0-9\-]+", inv):
                    errors.append("Invoice number is required (letters, digits, dashes).")
                if not re.fullmatch(r"\d+(\.\d{1,2})?", amount):
                    errors.append("Amount must be a plain number such as 1250.00 (no commas or currency symbols).")
                if cur not in CURRENCIES:
                    errors.append("Currency is required.")
                try:
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
                        raise ValueError
                    datetime.strptime(due, "%Y-%m-%d")
                except ValueError:
                    errors.append("Due date must be in YYYY-MM-DD format.")
                if not errors and any(b["vendor"] == vendor and b["invoice_number"] == inv for b in state.bills):
                    errors.append(f"Duplicate: invoice {inv} already exists for {vendor}.")
                if errors:
                    return self._send(layout("New bill", bill_form(f, errors), user), status=422)
                if state.chaos:
                    state.valid_submissions += 1
                    if state.valid_submissions == 1:  # silent session expiry
                        state.sessions.pop(self._sid(), None)
                        return self._redirect(f"/login?msg={quote('Your session expired. Please sign in again.')}&next=/bills/new")
                    if state.valid_submissions == 2:  # transient outage
                        return self._send(layout("Service unavailable", "<p>The payables service is temporarily unavailable. Please try again.</p>", user), status=503)
                bill = {"id": state.next_id(), "vendor": vendor, "invoice_number": inv, "amount": amount,
                        "currency": cur, "due_date": due, "status": "Pending"}
                state.bills.append(bill)
                return self._redirect(f"/bills?flash={quote('Bill ' + bill['id'] + ' saved.')}")
            m = re.fullmatch(r"/bills/(BILL-\d+)/pay", u.path)
            if m:
                if not user:
                    return self._need_login("/bills")
                for b in state.bills:
                    if b["id"] == m.group(1):
                        b["status"] = "Paid"
                        return self._redirect(f"/bills?flash={quote(b['id'] + ' marked as paid.')}")
            self._send(layout("Not found", "<p>No such page.</p>", user), status=404)

    return Handler


def start(port=8800, chaos=True, host="127.0.0.1"):
    """Start the mock ERP in a background thread. Returns (server, state)."""
    state = State(chaos)
    server = ThreadingHTTPServer((host, port), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


if __name__ == "__main__":
    srv, _ = start(8800)
    print("Mock ERP on http://127.0.0.1:8800  (login demo / demo123)  - Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
