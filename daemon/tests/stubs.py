"""Stub mitmproxy objects for adapter and addon tests.

Shared test infrastructure, not a test module. These stand in for mitmproxy's
Headers, Request, Response, and Flow so the adapter can be tested for shape
translation without a live proxy — which is the whole point of having an adapter
layer (SPEC-1 §3.2).
"""

from __future__ import annotations

from typing import Any


class StubHeaders:
    """Stands in for mitmproxy's Headers: ordered, repeating, case-insensitive."""

    def __init__(self, fields: list[tuple[bytes, bytes]] | None = None) -> None:
        self.fields = list(fields or [])

    def __contains__(self, name: str) -> bool:
        return any(k.decode().lower() == name.lower() for k, _ in self.fields)

    def __delitem__(self, name: str) -> None:
        self.fields = [f for f in self.fields if f[0].decode().lower() != name.lower()]

    def __setitem__(self, name: str, value: str) -> None:
        del self[name]
        self.fields.append((name.encode(), value.encode()))

    def get(self, name: str, default: str | None = None) -> str | None:
        for k, v in self.fields:
            if k.decode().lower() == name.lower():
                return v.decode()
        return default

    def add(self, name: str, value: str) -> None:
        self.fields.append((name.encode(), value.encode()))


class StubQuery:
    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def items(self, multi: bool = False) -> list[tuple[str, str]]:
        return list(self._pairs)


class StubRequest:
    def __init__(self, **kwargs: Any) -> None:
        self.scheme = kwargs.get("scheme", "https")
        self.method = kwargs.get("method", "get")
        self.host = kwargs.get("host", "cdn.example.com")
        self.pretty_host = kwargs.get("pretty_host", self.host)
        self.port = kwargs.get("port", 443)
        self.path = kwargs.get("path", "/a/analytics.js?v=3")
        self.url = kwargs.get("url", "https://cdn.example.com/a/analytics.js?v=3")
        self.pretty_url = kwargs.get("pretty_url", self.url)
        self.http_version = kwargs.get("http_version", "HTTP/2")
        self.headers = kwargs.get("headers", StubHeaders())
        self.content = kwargs.get("content", b"")
        self.query = kwargs.get("query", StubQuery([("v", "3")]))
        self.timestamp_start = kwargs.get("timestamp_start", 1756300000.123)


class StubResponse:
    def __init__(self, **kwargs: Any) -> None:
        self.status_code = kwargs.get("status_code", 200)
        self.reason = kwargs.get("reason", "OK")
        self.http_version = kwargs.get("http_version", "HTTP/2")
        self.headers = kwargs.get("headers", StubHeaders())
        self.content = kwargs.get("content", b"body")
        self.stream = kwargs.get("stream", False)
        self.timestamp_start = kwargs.get("timestamp_start", 1756300000.456)


class StubFlow:
    def __init__(self, request: Any = None, response: Any = None) -> None:
        self.id = "01JB2K7Q9X4M8Z0V3T5R7W1Y2A"
        self.request = request or StubRequest()
        self.response = response
        self.metadata: dict[str, Any] = {}
