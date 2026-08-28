"""Test doubles for the control API.

Every test in this suite runs against an ``httpx.MockTransport``. Nothing here
starts a daemon, opens a socket, or touches the state directory — which is the
point: the MCP server is an HTTP client, so a recorded transport is a complete
test of everything it does (SPEC-1 §11.1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from pporlock_mcp.client import ControlClient
from pporlock_mcp.server import PporlockMCP

MASKED_COOKIE = "«redacted:sha1=9f2a,len=64»"


def flow(
    flow_id: str = "f1",
    *,
    host: str = "example.com",
    status: int = 200,
    provenance: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A flow record shaped like SPEC-0 §3.4, redacted like a real one."""
    record: dict[str, Any] = {
        "flow_id": flow_id,
        "request": {
            "url": f"https://{host}/thing",
            "host": host,
            "method": "GET",
            "headers": {"cookie": MASKED_COOKIE},
        },
        "response": {"status": status, "headers": {}},
        "modified": False,
        "blocked": False,
        "provenance": provenance
        if provenance is not None
        else {
            "profile": "default",
            "evaluated_modules": [],
            "entries": [],
            "notes": [],
            "total_ms": 0.2,
        },
    }
    record.update(extra)
    return record


@dataclass
class RecordedRequest:
    method: str
    path: str
    params: dict[str, str]
    headers: dict[str, str]
    json_body: Any


@dataclass
class FakeDaemon:
    """A canned control API. Routes are ``(METHOD, path)`` keys."""

    routes: dict[tuple[str, str], Any] = field(default_factory=dict)
    requests: list[RecordedRequest] = field(default_factory=list)
    status: dict[tuple[str, str], int] = field(default_factory=dict)

    def route(self, method: str, path: str, payload: Any, status: int = 200) -> None:
        self.routes[(method.upper(), path)] = payload
        self.status[(method.upper(), path)] = status

    def handle(self, request: httpx.Request) -> httpx.Response:
        body: Any = None
        if request.content:
            body = json.loads(request.content)
        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.url.path,
                params=dict(request.url.params),
                headers={k.lower(): v for k, v in request.headers.items()},
                json_body=body,
            )
        )
        key = (request.method.upper(), request.url.path)
        if key not in self.routes:
            return httpx.Response(
                404,
                json={"error": {"code": "not_found", "message": f"no route {key}", "detail": {}}},
            )
        status = self.status.get(key, 200)
        payload = self.routes[key]
        if payload is None:
            return httpx.Response(204)
        return httpx.Response(status, json=payload)

    @property
    def last(self) -> RecordedRequest:
        return self.requests[-1]


@pytest.fixture
def daemon() -> FakeDaemon:
    return FakeDaemon()


@pytest.fixture
def client(daemon: FakeDaemon) -> ControlClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(daemon.handle))
    return ControlClient("http://127.0.0.1:8081", "test-token", http=http)


@pytest.fixture
def server(client: ControlClient) -> PporlockMCP:
    return PporlockMCP(client=client)


@pytest.fixture
def readonly_server(client: ControlClient) -> PporlockMCP:
    return PporlockMCP(read_only=True, client=client)


def result_payload(result: Any) -> Any:
    """Decode the JSON a tool result carries."""
    return json.loads(result.content[0].text)
