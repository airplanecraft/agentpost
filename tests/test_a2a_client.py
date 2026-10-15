"""A2A client against a local mock agent server: discovery via agent
card, message/send envelope, structured data part mapping, and the
error taxonomy the rest of the system depends on."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from agentposthub import tripplanner


class _Agent:
    def __init__(self):
        self.card = {"name": "mock planner", "url": None}
        self.responses = []          # list of dicts to return as result
        self.rpc_errors = []         # list of error objects
        self.requests = []

    def serve(self):
        agent = self

        class H(BaseHTTPRequestHandler):
            def _send(self, obj, status=200):
                b = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path.endswith("agent-card.json"):
                    self._send({**agent.card,
                                "url": agent.card["url"] or
                                       f"http://127.0.0.1:{self.server.server_port}/a2a"})
                else:
                    self._send({}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                agent.requests.append(body)
                if agent.rpc_errors:
                    self._send({"jsonrpc": "2.0", "id": body.get("id"),
                                "error": agent.rpc_errors.pop(0)})
                    return
                self._send({"jsonrpc": "2.0", "id": body.get("id"),
                            "result": agent.responses.pop(0)})

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv


@pytest.fixture()
def agent(monkeypatch):
    a = _Agent()
    srv = a.serve()
    base = f"http://127.0.0.1:{srv.server_port}"
    a.base = base
    tripplanner._card_cache.clear()
    monkeypatch.setattr("agentposthub.config.get_settings.__wrapped__",
                        lambda: None, raising=False)  # noop guard
    from agentposthub.config import Settings
    monkeypatch.setattr(tripplanner, "get_settings",
                        lambda: Settings(reference_site=base))
    yield a
    srv.shutdown()


def test_discovery_and_structured_roundtrip(agent):
    agent.responses.append({
        "kind": "message", "role": "agent", "messageId": "m1",
        "parts": [
            {"kind": "text", "text": "Beijing 10d plan, budget tier downgraded."},
            {"kind": "data", "data": {"totalUsd": 759,
                                      "routeNames": ["Beijing", "Tianjin"],
                                      "breakdown": {"rooms": 2}}},
        ]})
    r = tripplanner.plan_trip("北京10日 1500美金", "zh")
    assert r["ok"] is True
    assert r["draft"]["total_usd"] == 759
    assert r["draft"]["route_names"] == ["Beijing", "Tianjin"]
    assert r["draft"]["breakdown"] == {"rooms": 2}
    # the request was a proper A2A envelope with a text part
    req = agent.requests[0]
    assert req["method"] == "message/send"
    assert req["params"]["message"]["parts"][0]["kind"] == "text"
    assert req["params"]["metadata"]["locale"] == "zh"
    # discovery cached the endpoint from the card
    assert tripplanner._card_cache[agent.base][1] == "mock planner"


def test_text_only_reply_is_no_draft(agent):
    agent.responses.append({
        "kind": "message", "role": "agent",
        "parts": [{"kind": "text", "text": "Which cities do you prefer?"}]})
    r = tripplanner.plan_trip("trip please")
    assert r["ok"] is False
    assert r["error"] == "no_draft"
    assert "cities" in r["reply"]


def test_rpc_rate_limit_maps_to_taxonomy(agent):
    agent.rpc_errors.append({"code": -32002, "message": "rate_limited"})
    r = tripplanner.plan_trip("trip please")
    assert r == {"ok": False, "error": "rate_limited"}


def test_card_absent_falls_back_to_default_endpoint(monkeypatch):
    from agentposthub.config import Settings
    monkeypatch.setattr(tripplanner, "get_settings",
                        lambda: Settings(reference_site="https://nonexistent.example"))
    tripplanner._card_cache.clear()
    endpoint, name = tripplanner._discover_endpoint("https://nonexistent.example")
    assert endpoint == "https://nonexistent.example/a2a"
    assert name == "fallback-agent"
