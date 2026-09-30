"""A fixed-destination Responses API gateway isolated from the agent process."""

import argparse
import http.client
import http.server
import json
import os
import threading
import time
from urllib.parse import urlsplit

from .gateway import Server


MAX_BODY = 16 * 1024 * 1024
MAX_OUTPUT_TOKENS = 65536
ALLOWED_PATHS = {"/v1/responses", "/v1/responses/compact"}
CLIENT_TOOL_TYPES = {"custom", "function", "local_shell"}
PROVIDERS = {
    "openai": ("api.openai.com", "OPENAI_API_KEY"),
    "deepseek": ("api.deepseek.com", "DEEPSEEK_API_KEY"),
    "openrouter": ("openrouter.ai", "OPENROUTER_API_KEY"),
    # smoke: confirm the Kilo Code provider host and path prefix against the real
    # binary before any trial (docs/kilocode.md, "Open validation items").
    "kilocode": ("kilocode.ai", "KILOCODE_API_KEY"),
}


def upstream_destination(provider, path, native_client=None):
    host, _ = PROVIDERS[provider]
    if provider in {"deepseek", "openrouter"}:
        if urlsplit(path).path not in ({"/v1/responses", "/v1/chat/completions"} if native_client in {"grok", "zcode", "kimi", "kilocode"} else {"/v1/responses"}):
            raise ValueError("Provider supports the Responses endpoint only")
        path = path.removeprefix("/v1") if provider == "deepseek" else "/api" + path
    elif provider == "kilocode":
        # The Kilo Code hosted provider is OpenAI Chat Completions compatible.
        if urlsplit(path).path != "/v1/chat/completions":
            raise ValueError("Kilo provider supports the Chat Completions endpoint only")
        # smoke: confirm the upstream path prefix (Kilo proxies via /api/...).
        path = "/api/openrouter" + path
    return host, path


def privacy_skill_advertised(data):
    tools = data.get("tools", [])
    normalized = [tool.get("function", tool) for tool in tools]
    skill_tools = [tool for tool in normalized if str(tool.get("name", "")).lower() == "skill"]
    if not skill_tools:
        return False
    # OpenCode versions advertise skills either in the tool description or in
    # system messages. A skill name appearing only in a user prompt is not discovery.
    contexts = [tool.get("description", "") for tool in skill_tools]
    contexts.append(data.get("instructions", ""))
    items = data.get("input", data.get("messages", []))
    if isinstance(items, list):
        contexts.extend(item for item in items if isinstance(item, dict)
                        and item.get("role") in {"system", "developer"})
    return any(name in json.dumps(context) for context in contexts
               for name in ("privacy-protection", "dataset-download", "workspace-cleanup"))


def validate_client_tools(tools):
    if not isinstance(tools, list) or len(tools) > 256:
        raise ValueError("Tool list is outside the gateway bounds")
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("Only client-executed tools are available")
        kind = tool.get("type")
        if kind in CLIENT_TOOL_TYPES:
            continue
        if kind == "namespace":
            children = tool.get("tools")
            if not isinstance(children, list) or not children or len(children) > 256:
                raise ValueError("Tool namespace is outside the gateway bounds")
            if any(not isinstance(child, dict) or child.get("type") not in CLIENT_TOOL_TYPES
                   for child in children):
                raise ValueError("Only client-executed namespaced tools are available")
            continue
        raise ValueError("Only client-executed tools are available")


def validate_request(path, body, expected_model, provider="openai", native_client=None):
    parsed = urlsplit(path)
    allowed = {"/v1/chat/completions"} if native_client in {"grok", "zcode", "kimi", "kilocode"} else ALLOWED_PATHS
    if parsed.scheme or parsed.netloc or parsed.path not in allowed:
        raise ValueError("Endpoint is not available")
    if len(body) > MAX_BODY:
        raise ValueError("Request is too large")
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("messages" if native_client in {"grok", "zcode", "kimi", "kilocode"} else "input"), (str, list)):
        raise ValueError("Expected a Responses API request")
    if data.get("model") != expected_model:
        raise ValueError("Request model differs from the configured experiment model")
    if provider == "openrouter" and any(data.get(field) for field in ("models", "route", "plugins")):
        raise ValueError("Model overrides and hosted plugins are unavailable")
    tokens = data.get("max_tokens", data.get("max_completion_tokens")) if native_client in {"grok", "zcode", "kimi", "kilocode"} else data.get("max_output_tokens")
    token_limit = 131072 if native_client in {"zcode", "kimi", "kilocode"} else MAX_OUTPUT_TOKENS
    if tokens is not None and (type(tokens) is not int or not 1 <= tokens <= token_limit):
        raise ValueError("Output token limit is outside the gateway bounds")
    if data.get("store") is True:
        raise ValueError("Server-side response storage is unavailable")
    # Codex executes these tool calls locally. Hosted tools would escape Docker's
    # network boundary through the upstream API and are therefore rejected.
    validate_client_tools(data.get("tools", []))
    return parsed.path + ("?" + parsed.query if parsed.query else "")


_STATUS_LOCK = threading.Lock()


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def status(self, kind, request_id, **fields):
        if getattr(self.server, "log_request_status", False):
            with _STATUS_LOCK:
                print(json.dumps({"kind": kind, "observed_ns": time.time_ns(),
                                  "request_id": request_id, **fields}), flush=True)

    def fail(self, status, message):
        payload = json.dumps({"error": {
            "type": "invalid_request_error", "message": message,
        }}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_GET(self):
        if getattr(self.server, 'native_client', None) != 'muse' or self.path != '/v1/models':
            self.fail(404, 'Endpoint is not available')
            return
        from .muse_transport import catalog
        body = json.dumps(catalog(self.server.expected_model)).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.connection.settimeout(60)
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("Chunked request bodies are not supported")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1:
                raise ValueError("A single content length is required")
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY:
                raise ValueError("Invalid request size")
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Incomplete request")
            provider = getattr(self.server, "provider", "openai")
            native_client = getattr(self.server, 'native_client', None)
            aliases = {}
            if native_client == 'muse':
                from .muse_transport import prepare
                body, aliases = prepare(body)
            path = validate_request(self.path, body, self.server.expected_model, provider=provider,
                                    native_client=native_client)
            host, path = upstream_destination(provider, path, native_client=native_client)
        except (ValueError, OSError, TypeError):
            self.fail(400, "Request rejected by experiment gateway")
            return
        with self.server.request_lock:
            if self.server.remaining is not None and self.server.remaining <= 0:
                # This fixed local budget cannot recover through retries. 429
                # makes native clients back off repeatedly as if it were a
                # temporary upstream rate limit.
                self.fail(400, "Experiment request limit reached")
                return
            if self.server.remaining is not None:
                self.server.remaining -= 1
            request_id = getattr(self.server, "requests_started", 0) + 1
            self.server.requests_started = request_id
        data = json.loads(body)
        tools = data.get("tools", [])
        advertised = privacy_skill_advertised(data)
        self.status("gateway_request", request_id, provider=provider,
                    model=self.server.expected_model, client_tools_present=bool(tools),
                    skill_advertised=advertised)
        if getattr(self.server, 'muse_tool_evidence', False) and request_id == 1:
            self.status('muse_tool_catalog', request_id, tools=tools)
        if getattr(self.server, "log_request_bodies", False):
            # Opt-in only for the isolated compaction experiment. Never log
            # headers or upstream credentials. This proves actual carry-forward.
            self.status("gateway_request_body", request_id, path=self.path, body=data)
        headers = {
            "Authorization": "Bearer " + self.server.api_key,
            "Content-Type": "application/json",
            "Accept": self.headers.get("Accept", "text/event-stream"),
        }
        if self.headers.get("OpenAI-Beta"):
            headers["OpenAI-Beta"] = self.headers["OpenAI-Beta"]
        sent_headers = False
        upstream = http.client.HTTPSConnection(host, timeout=60)
        try:
            upstream.request("POST", path, body=body, headers=headers)
            response = upstream.getresponse()
            self.status("gateway_response", request_id, status=response.status)
            if response.status in {401, 403}:
                response.read()
                self.fail(response.status, "Upstream authentication failed")
                return
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            self.send_header("Connection", "close")
            if response.getheader("x-request-id"):
                self.send_header("x-request-id", response.getheader("x-request-id"))
            self.end_headers()
            sent_headers = True
            transferred = 0
            if native_client == 'muse' and 'text/event-stream' in response.getheader('Content-Type', ''):
                from .muse_transport import stream_events
                chunks = stream_events(response, aliases)
            else:
                chunks = iter(lambda: response.read1(65536), b'')
            for chunk in chunks:
                if native_client == 'muse' and getattr(self.server, 'muse_tool_evidence', False):
                    for line in chunk.splitlines():
                        if not line.startswith(b'data: '):
                            continue
                        try:
                            event = json.loads(line[6:])
                        except ValueError:
                            continue
                        item = event.get('item', {})
                        if event.get('type') == 'response.output_item.done' and item.get('type') == 'function_call':
                            self.status('muse_tool_call', request_id, call_id=item.get('call_id'),
                                        name=item.get('name'), arguments=item.get('arguments'))
                self.wfile.write(chunk)
                self.wfile.flush()
                transferred += len(chunk)
            self.status("gateway_finished", request_id, bytes_forwarded=transferred)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            self.status("gateway_error", request_id, error_type=type(exc).__name__)
            if not sent_headers:
                self.fail(502, "Upstream request failed")
        finally:
            upstream.close()
            self.close_connection = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-requests", type=int, default=60)
    parser.add_argument("--expected-model", required=True)
    parser.add_argument("--provider", choices=PROVIDERS, default="openai")
    parser.add_argument("--log-request-status", action="store_true",
                        default=os.environ.get("TRACE_LAB_LOG_REQUEST_STATUS") == "1")
    parser.add_argument("--log-request-bodies", action="store_true",
                        help="record synthetic model inputs for compaction carry-forward verification")
    parser.add_argument("--native-client", choices=["muse", "grok", "zcode", "kimi", "kilocode"])
    parser.add_argument('--muse-tool-evidence', action='store_true')
    args = parser.parse_args()
    _, credential = PROVIDERS[args.provider]
    if args.native_client == 'muse' and args.provider == 'openrouter' and os.environ.get('MUSE_OPENROUTER_API_KEY'):
        credential = 'MUSE_OPENROUTER_API_KEY'
    api_key = os.environ.get(credential)
    if not api_key:
        raise SystemExit(credential + " must be provided to the gateway")
    with Server("/relay/api.sock", Handler) as server:
        os.chmod("/relay/api.sock", 0o666)
        server.api_key = api_key
        server.provider = args.provider
        server.native_client = args.native_client
        server.muse_tool_evidence = args.muse_tool_evidence
        server.log_request_status = args.log_request_status
        if args.log_request_bodies and not args.log_request_status:
            parser.error("--log-request-bodies requires --log-request-status")
        server.log_request_bodies = args.log_request_bodies
        server.expected_model = args.expected_model
        server.request_lock = threading.Lock()
        server.remaining = None if args.max_requests == 0 else args.max_requests
        print("gateway ready", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
