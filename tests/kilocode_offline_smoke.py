"""Run inside the disposable image; scripted local model, never a scored trial.

Drives the real `kilo` (@kilocode/cli) binary against a local OpenAI-compatible
server, with the provider config from trace_lab.kilocode.initialize pointing at
it. Validates the full native flow end to end with no network or real key:
run -> bash tool -> SQLite trace -> same-session resume.
"""
import json
from pathlib import Path
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from trace_lab import kilocode

WORKSPACE = str(Path.home() / "ws")
phase = "task"
request_count = 0
requests = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        global request_count
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests.append(body)
        request_count += 1
        planned = {
            "task": [("bash", {"command": "printf SMOKE_OK > " + WORKSPACE + "/smoke.txt"})],
            "resume": [("bash", {"command": "cat " + WORKSPACE + "/smoke.txt"})],
            # A resumed turn (no --auto) touching a path outside the workspace, as an
            # agent looking for its own trace store does. Must not be auto-rejected.
            "outside": [("glob", {"pattern": "**/*.db*", "path": str(Path.home() / ".local/share")})],
        }.get(phase, [])
        if request_count <= len(planned):
            name, arguments = planned[request_count - 1]
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": f"call_{phase}_{request_count}",
                     "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "Preserved SMOKE_OK."}
            finish = "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for part, reason in [(delta, None), ({}, finish)]:
            chunk = {"id": "chatcmpl-smoke", "object": "chat.completion.chunk",
                     "created": int(time.time()), "model": body["model"],
                     "choices": [{"index": 0, "delta": part, "finish_reason": reason}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
        self.wfile.write(b"data: [DONE]\n\n")


def main():
    global phase, request_count
    Path(WORKSPACE).mkdir(parents=True, exist_ok=True)
    kilocode.initialize("smoke-model", str(Path.home()))
    server = HTTPServer(("127.0.0.1", 8080), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sid = None
    results = {}
    try:
        for phase, prompt in [("task", "Validate the local integration."),
                              ("resume", "Read the same file."),
                              ("outside", "Look for database files outside the workspace.")]:
            request_count = 0
            command = kilocode.native_command("trace_lab/smoke-model", WORKSPACE, sid, bool(sid)) + [prompt]
            process = subprocess.run(command, text=True, capture_output=True, timeout=120)
            events = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")]
            observed = kilocode.session_id(events)
            assert not process.returncode and kilocode.succeeded(events), (phase, process.returncode, process.stderr[-2000:])
            assert observed and (sid is None or sid == observed), ("session changed", sid, observed)
            sid = observed
            tools = list(kilocode.tool_inputs(events))
            assert all(t[2]["_native_status"] == "completed" for t in tools), tools
            if phase == "resume":
                assert any("SMOKE_OK" in str(t[2]["_native_output"]) for t in tools), tools
            results[phase] = {"passed": True, "session_id": sid, "tool_count": len(tools)}
        assert (Path.home() / kilocode.DB).exists(), "native store missing"
        # The read-only reader (shared with OpenCode) must see this session's rows.
        from trace_lab.kilocode_trace_check import check
        inspected = check(sid, home=Path.home(), include_records=True)
        assert inspected["verified"] and not inspected["records_absent"], inspected
        assert inspected["row_counts"].get("session", 0) >= 1, inspected["row_counts"]
        results["trace_check"] = {"verified": True, "row_counts": inspected["row_counts"]}
        print(json.dumps({"passed": True, "phases": results, "native_database": kilocode.DB}))
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
