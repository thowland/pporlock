"""A module's report, served by the daemon — OI-29, REQ MOD-023, API-004.

A module that accumulates something worth reading had nowhere to put it.
`ctx.store_*` is persistent but no API can read it, so the first working
version of `gpc-audit` answered a magic URL of its own through the proxy. That
URL is unreachable from the web UI — the control origin is not proxied traffic
— so a report about browsing could only be read *while browsing*, by someone
who remembered the path. It was a feature nobody could find.

`on_report` lets the module render, and the daemon serve.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from pporlock.capture.ring import RingBuffer
from pporlock.config import Config
from pporlock.control.app import OFFLOAD_ROUTES, ControlApp
from pporlock.engine.modules.loader import LoadedModule
from pporlock.engine.modules.registry import ModuleRegistry


def _registry_with(python: object, tmp_path: Path, *, name: str = "rep") -> ModuleRegistry:
    registry = ModuleRegistry(tmp_path / "modules")
    module = LoadedModule(name=name, path=tmp_path / "modules" / name, python=python)
    registry._modules[name] = module
    registry._contexts[name] = types.SimpleNamespace(name=name)
    return registry


@pytest.fixture
def app(tmp_path: Path) -> ControlApp:
    config = Config()
    config.state_dir = str(tmp_path)
    return ControlApp(config, ring=RingBuffer())


def _client(app: ControlApp) -> tuple[TestClient, dict[str, str]]:
    token = app.tokens.ensure()
    return TestClient(app.asgi), {"Authorization": f"Bearer {token}", "X-Pporlock-Client": "cli"}


def test_a_module_report_is_served(app: ControlApp, tmp_path: Path) -> None:
    """The whole point: readable from the control origin, no browsing required."""
    python = types.SimpleNamespace(
        on_report=lambda ctx: {"content_type": "text/html", "body": "<p>hello</p>"}
    )
    app.registry = _registry_with(python, tmp_path)
    client, headers = _client(app)

    response = client.get("/modules/rep/report", headers=headers)

    assert response.status_code == 200
    assert response.text == "<p>hello</p>"
    assert response.headers["content-type"].startswith("text/html")


def test_the_report_is_sandboxed(app: ControlApp, tmp_path: Path) -> None:
    """The body is module-authored and this origin holds the bearer token.

    A `sandbox` CSP puts it in a unique opaque origin with no script and no
    same-origin access. Module code is trusted and could reach the token by
    other means, so this is not a boundary — it is a refusal to add a
    convenient one, and it costs nothing.
    """
    python = types.SimpleNamespace(on_report=lambda ctx: {"body": "x"})
    app.registry = _registry_with(python, tmp_path)
    client, headers = _client(app)

    response = client.get("/modules/rep/report", headers=headers)

    assert response.headers["content-security-policy"] == "sandbox"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_a_module_without_a_report_is_a_404_not_an_error(app: ControlApp, tmp_path: Path) -> None:
    """Most modules have nothing to report and that is not a failure."""
    app.registry = _registry_with(types.SimpleNamespace(), tmp_path)
    client, headers = _client(app)

    assert client.get("/modules/rep/report", headers=headers).status_code == 404


def test_an_unknown_module_is_a_404(app: ControlApp, tmp_path: Path) -> None:
    app.registry = _registry_with(types.SimpleNamespace(), tmp_path)
    client, headers = _client(app)

    assert client.get("/modules/nope/report", headers=headers).status_code == 404


def test_a_raising_report_does_not_take_down_the_daemon(app: ControlApp, tmp_path: Path) -> None:
    """A broken report is not a reason to stop a module modifying traffic.

    502 names the module rather than surfacing a stack trace, and the daemon
    keeps serving.
    """

    def boom(ctx: object) -> None:
        raise RuntimeError("no")

    app.registry = _registry_with(types.SimpleNamespace(on_report=boom), tmp_path)
    client, headers = _client(app)

    response = client.get("/modules/rep/report", headers=headers)

    assert response.status_code == 502
    assert "rep" in response.json()["error"]["message"]
    assert client.get("/state/health").status_code == 200


def test_an_unsupported_content_type_is_refused(app: ControlApp, tmp_path: Path) -> None:
    """The daemon serves this from the origin that also serves the web UI.

    Letting a module pick an arbitrary content type there — a script, a
    manifest, anything the browser treats specially — is how a report becomes
    an attack surface. Four text-ish types are allowed and the rest are 502.
    """
    python = types.SimpleNamespace(
        on_report=lambda ctx: {"content_type": "application/javascript", "body": "alert(1)"}
    )
    app.registry = _registry_with(python, tmp_path)
    client, headers = _client(app)

    assert client.get("/modules/rep/report", headers=headers).status_code == 502


def test_a_bare_string_is_served_as_plain_text(app: ControlApp, tmp_path: Path) -> None:
    """The simplest possible report should need no ceremony."""
    app.registry = _registry_with(
        types.SimpleNamespace(on_report=lambda ctx: "just text"), tmp_path
    )
    client, headers = _client(app)

    response = client.get("/modules/rep/report", headers=headers)

    assert response.status_code == 200
    assert response.text == "just text"
    assert response.headers["content-type"].startswith("text/plain")


def test_the_route_is_offloaded(app: ControlApp) -> None:
    """REQ DD-3. It runs module code, which must not happen on the proxy's loop."""
    assert "/modules/{name}/report" in OFFLOAD_ROUTES


def test_the_report_needs_a_token(app: ControlApp, tmp_path: Path) -> None:
    """It is module output about the user's own browsing. Not public."""
    app.registry = _registry_with(types.SimpleNamespace(on_report=lambda ctx: "secret"), tmp_path)
    client = TestClient(app.asgi)

    assert client.get("/modules/rep/report").status_code == 401
