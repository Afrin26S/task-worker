"""A fake OpenAI-compatible /chat/completions server that answers with a ScriptedLLM (for testing the real HTTP client + CLI)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(scripted, fail_first=0):
    state = {"n": 0}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["n"] += 1
            assert self.headers.get("Authorization", "").startswith("Bearer ")
            if state["n"] <= fail_first:
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            system = next(m["content"] for m in body["messages"] if m["role"] == "system")
            user = next(m["content"] for m in body["messages"] if m["role"] == "user")
            out = scripted.complete(system, user)
            data = json.dumps({"choices": [{"message": {"role": "assistant", "content": out}}],
                               "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"
