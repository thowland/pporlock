"""The control API against its OpenAPI specification — REQ TST-005.

`contracts/openapi.yaml` is the source of truth for the wire shapes three
clients consume: the web UI, the extension, and the MCP server. Nothing else
keeps the daemon honest about it. The schema-conformance suite checks the
*JSON Schema* files; this one checks the OpenAPI document, which declares the
routes, the status codes, and the response bodies.

The tests drive the real ``ControlApp`` through Starlette's ``TestClient`` — the
same app, middleware and auth path the browser hits — and validate what actually
comes back against what the document says will. A contract test that constructed
its own responses would be testing the test.

Three distinct failures are checked, because they fail in different ways:

* a route the daemon serves that the document does not declare (clients cannot
  discover it, and generated types will not cover it);
* a route the document declares that the daemon does not serve (a client is
  generated against a 404);
* a response whose body does not validate against the declared schema (the worst
  kind, because everything looks like it is working).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from pporlock.capture.ring import RingBuffer
from pporlock.config import Config
from pporlock.control.app import ControlApp
from pporlock.control.events import EventHub
from pporlock.engine.modules.registry import ModuleRegistry
from pporlock.engine.profiles import ProfileManager

from .test_ring import make_record

OPENAPI_PATH = Path(__file__).resolve().parents[3] / "contracts" / "openapi.yaml"

#: Routes the daemon serves that ``contracts/openapi.yaml`` does not declare.
#:
#: Every entry here is a real contract gap, not an exemption on principle. They
#: are listed rather than asserted away so that this test still catches the
#: *next* undeclared route — an empty allowlist would have to be a bare
#: xfail, which catches nothing.
#:
#: ``/rules``      — GET and PUT are implemented, tested, and used by the web
#:                   UI's rule editor. SPEC-0 §6 describes them. No OpenAPI
#:                   entry exists.
#: ``/pair/begin`` — implemented and driven by `pporlock pair`. ``/pair`` (the
#:                   redemption half) is declared; the half that mints the code
#:                   is not.
#:
#: Owned by whoever owns ``contracts/``; reported by Sprint 16.
UNDECLARED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/rules"),
        ("PUT", "/rules"),
        ("POST", "/pair/begin"),
    }
)

#: Paths served by the app that are not API surface at all.
NON_API_PATHS: frozenset[str] = frozenset({"/"})


def load_spec() -> dict[str, Any]:
    return yaml.safe_load(OPENAPI_PATH.read_text())  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return load_spec()


SCHEMA_DIR = OPENAPI_PATH.parent / "schemas"


def _schema_registry() -> Registry:  # type: ignore[type-arg]
    """Resolve both kinds of ``$ref`` the document uses.

    ``contracts/openapi.yaml`` points two ways: inward at
    ``#/components/schemas/X``, and outward at ``./schemas/flow.schema.json``,
    which is the *same* JSON Schema file the daemon's structures are checked
    against in ``test_schema_conformance``. Registering the external files under
    both their relative path and their ``$id`` is what makes a response body
    validated here be validated against exactly those schemas, rather than
    against a copy that could drift.
    """
    registry: Registry = Registry()  # type: ignore[type-arg]
    for path in SCHEMA_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(f"./schemas/{path.name}", resource)
        if "$id" in schema:
            registry = registry.with_resource(schema["$id"], resource)
    return registry


def _resolved_validator(spec: dict[str, Any], schema: dict[str, Any]) -> Draft202012Validator:
    """A validator whose ``$ref``s resolve, inward and outward.

    The OpenAPI document is one file, so ``#/components/schemas/X`` is a pointer
    into itself: handing jsonschema the whole document as the root resource is
    what makes those resolve without rewriting them. The external schema files
    come from the registry.
    """
    root = dict(spec)
    root.update(schema)
    return Draft202012Validator(root, registry=_schema_registry())


def schema_for(spec: dict[str, Any], method: str, path: str, status: int) -> dict[str, Any] | None:
    """The declared response schema, or None where the document declares no body."""
    operation = spec["paths"].get(path, {}).get(method.lower())
    if operation is None:
        return None
    response = operation.get("responses", {}).get(str(status))
    if response is None:
        return None
    content = response.get("content", {}).get("application/json")
    if content is None:
        return None
    return content.get("schema")  # type: ignore[no-any-return]


def assert_matches(spec: dict[str, Any], method: str, path: str, response: Any) -> None:
    """Validate one live response against its declared schema.

    A missing declaration for the status that actually came back is itself a
    failure. Silently passing when the document says nothing about a 200 would
    make this suite grade the daemon against an empty rubric.
    """
    declared = spec["paths"].get(path, {}).get(method.lower())
    assert declared is not None, f"{method} {path} is not declared in openapi.yaml"
    responses = declared.get("responses", {})
    assert str(response.status_code) in responses, (
        f"{method} {path} answered {response.status_code}; "
        f"openapi.yaml declares only {sorted(responses)}"
    )

    schema = schema_for(spec, method, path, response.status_code)
    if schema is None:
        return
    body = response.json() if response.content else None
    errors = sorted(
        _resolved_validator(spec, schema).iter_errors(body), key=lambda e: list(e.absolute_path)
    )
    if errors:
        first = errors[0]
        location = "/".join(str(p) for p in first.absolute_path) or "(root)"
        raise AssertionError(
            f"{method} {path} -> {response.status_code}: response does not match its "
            f"declared schema at {location}: {first.message}\n"
            f"body: {json.dumps(body)[:600]}"
        )


# ------------------------------------------------------------------- harness --


@pytest.fixture
def app(tmp_path: Path) -> ControlApp:
    """The app the daemon serves, with a registry and profiles wired in.

    A ControlApp built without a registry answers 404 on every module route
    (OI-11), which would let this suite pass while validating nothing about the
    module surface.
    """
    config = Config()
    config.state_dir = str(tmp_path)
    config.modules.root = str(tmp_path / "modules")
    (tmp_path / "modules").mkdir(parents=True, exist_ok=True)

    ring = RingBuffer()
    ring.add(make_record("f0", host="a.example", path="/one.js"))
    ring.add(make_record("f1", host="b.example", path="/two.css", status=404))

    registry = ModuleRegistry(Path(config.modules.root))
    registry.reload()
    profiles = ProfileManager(tmp_path / "profiles")

    return ControlApp(
        config,
        ring=ring,
        interceptor=None,
        events=EventHub(),
        registry=registry,
        profiles=profiles,
    )


@pytest.fixture
def client(app: ControlApp) -> TestClient:
    return TestClient(app.asgi)


@pytest.fixture
def headers(app: ControlApp) -> dict[str, str]:
    # X-Pporlock-Client is stamped on every authenticated request, reads
    # included — not only mutations.
    return {"Authorization": f"Bearer {app.tokens.ensure()}", "X-Pporlock-Client": "ui"}


def served_routes(app: ControlApp) -> set[tuple[str, str]]:
    """Every (method, path) the ASGI app actually routes, minus HEAD/OPTIONS."""
    out: set[tuple[str, str]] = set()
    for route in app.asgi.routes:
        if isinstance(route, Mount) or not isinstance(route, Route):
            continue
        for method in route.methods or ():
            if method in {"HEAD", "OPTIONS"}:
                continue
            out.add((method, route.path))
    return out


# ------------------------------------------------------------- route coverage --


class TestRouteCoverage:
    """REQ TST-005 — the document and the app describe the same surface."""

    def test_every_served_route_is_declared(self, app: ControlApp, spec: dict[str, Any]) -> None:
        declared = {
            (method.upper(), path)
            for path, operations in spec["paths"].items()
            for method in operations
            if method in {"get", "post", "put", "patch", "delete"}
        }
        served = {
            (method, path) for method, path in served_routes(app) if path not in NON_API_PATHS
        }
        missing = served - declared - UNDECLARED_ROUTES
        assert not missing, (
            f"these routes are served but not declared in contracts/openapi.yaml: {sorted(missing)}"
        )

    def test_every_declared_route_is_served(self, app: ControlApp, spec: dict[str, Any]) -> None:
        """A declared route the daemon does not serve generates a client for a 404."""
        declared = {
            (method.upper(), path)
            for path, operations in spec["paths"].items()
            for method in operations
            if method in {"get", "post", "put", "patch", "delete"}
        }
        served = served_routes(app)
        missing = declared - served
        assert not missing, f"declared in openapi.yaml but not served: {sorted(missing)}"

    def test_the_undeclared_list_is_still_accurate(
        self, app: ControlApp, spec: dict[str, Any]
    ) -> None:
        """When the OpenAPI catches up, this fails and the allowlist comes out.

        An allowlist nobody is forced to revisit becomes permanent. This is the
        forcing function.
        """
        declared = {
            (method.upper(), path)
            for path, operations in spec["paths"].items()
            for method in operations
            if method in {"get", "post", "put", "patch", "delete"}
        }
        now_declared = UNDECLARED_ROUTES & declared
        assert not now_declared, (
            f"{sorted(now_declared)} are now declared in openapi.yaml — "
            "remove them from UNDECLARED_ROUTES"
        )


# ------------------------------------------------------------ response bodies --


class TestReadRoutes:
    """Every GET, validated against its declared response schema."""

    def test_state_health(self, client: TestClient, spec: dict[str, Any]) -> None:
        assert_matches(spec, "GET", "/state/health", client.get("/state/health"))

    def test_state(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "GET", "/state", client.get("/state", headers=headers))

    def test_state_unauthorized_body(self, client: TestClient, spec: Any) -> None:
        """The 401 body is part of the contract; clients branch on error.code."""
        assert_matches(spec, "GET", "/state", client.get("/state"))

    def test_flows(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "GET", "/flows", client.get("/flows", headers=headers))

    def test_flow_detail(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "GET", "/flows/{flow_id}", client.get("/flows/f0", headers=headers))

    def test_flow_not_found(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "GET", "/flows/{flow_id}", client.get("/flows/nope", headers=headers))

    def test_modules(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "GET", "/modules", client.get("/modules", headers=headers))

    def test_profiles(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "GET", "/profiles", client.get("/profiles", headers=headers))

    def test_profile_detail(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(
            spec, "GET", "/profiles/{name}", client.get("/profiles/default", headers=headers)
        )

    def test_sessions(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "GET", "/sessions", client.get("/sessions", headers=headers))

    def test_config(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "GET", "/config", client.get("/config", headers=headers))

    def test_exclusions(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "GET", "/exclusions", client.get("/exclusions", headers=headers))

    def test_metrics(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "GET", "/metrics", client.get("/metrics", headers=headers))

    def test_audit(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "GET", "/audit", client.get("/audit", headers=headers))


class TestWriteRoutes:
    """Mutating routes, validated against their declared bodies and codes."""

    def test_post_state(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        response = client.post("/state", headers=headers, json={"dev_toggles": {"anticache": True}})
        assert_matches(spec, "POST", "/state", response)

    def test_put_config(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        current = client.get("/config", headers=headers).json()
        response = client.put("/config", headers=headers, json={"redaction": current["redaction"]})
        assert_matches(spec, "PUT", "/config", response)

    def test_put_exclusions(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        response = client.put(
            "/exclusions",
            headers=headers,
            json={"entries": [{"pattern": "*.apple.com", "comment": "OS update"}]},
        )
        assert_matches(spec, "PUT", "/exclusions", response)

    def test_suggest_rule(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        response = client.post(
            "/flows/f0/suggest-rule", headers=headers, json={"intent": "headers"}
        )
        assert_matches(spec, "POST", "/flows/{flow_id}/suggest-rule", response)

    def test_suggest_rule_rejects_an_unknown_intent(
        self, client: TestClient, headers: dict[str, str], spec: Any
    ) -> None:
        """A contract gap, recorded rather than asserted away.

        ``intent`` is required and validated, so this route can answer 400 —
        but ``openapi.yaml`` declares only 200 for it, and a client generated
        from the document has no error case to handle. The body is still the
        SPEC-0 §6.2 envelope, which is what this checks; the missing
        declaration belongs to whoever owns ``contracts/``.
        """
        response = client.post("/flows/f0/suggest-rule", headers=headers, json={"intent": "nope"})
        assert response.status_code == 400
        assert "400" not in spec["paths"]["/flows/{flow_id}/suggest-rule"]["post"]["responses"], (
            "openapi.yaml now declares the 400 — fold this back into assert_matches"
        )
        error_schema = spec["components"]["schemas"]["Error"]
        errors = list(_resolved_validator(spec, error_schema).iter_errors(response.json()))
        assert not errors, errors[0].message if errors else ""

    def test_validate(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        response = client.post(
            "/validate",
            headers=headers,
            json={"name": "tidy", "files": {"module.yaml": "name: tidy\npporlock_api: '1'\n"}},
        )
        assert_matches(spec, "POST", "/validate", response)

    def test_validate_reports_a_broken_module(
        self, client: TestClient, headers: dict[str, str], spec: Any
    ) -> None:
        """The issue list is the half of ValidationResult that carries detail."""
        response = client.post(
            "/validate", headers=headers, json={"name": "bad", "files": {"module.yaml": "{{{"}}
        )
        assert_matches(spec, "POST", "/validate", response)
        assert response.json()["ok"] is False

    def test_module_lifecycle(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        """Create, read, patch, reload, delete — the whole ModuleStatus surface."""
        created = client.post(
            "/modules",
            headers=headers,
            json={
                "name": "contract-mod",
                "files": {
                    "module.yaml": (
                        "name: contract-mod\npporlock_api: '1'\n"
                        "rules:\n"
                        "  - name: strip\n"
                        "    action: headers\n"
                        "    match: {host: a.example}\n"
                        "    response: {remove: [content-security-policy]}\n"
                    )
                },
            },
        )
        assert_matches(spec, "POST", "/modules", created)

        assert_matches(
            spec, "GET", "/modules/{name}", client.get("/modules/contract-mod", headers=headers)
        )
        assert_matches(
            spec,
            "PATCH",
            "/modules/{name}",
            client.patch("/modules/contract-mod", headers=headers, json={"enabled": True}),
        )
        assert_matches(
            spec,
            "POST",
            "/modules/reload",
            client.post("/modules/reload", headers=headers, json={}),
        )
        assert_matches(
            spec,
            "DELETE",
            "/modules/{name}",
            client.delete("/modules/contract-mod", headers=headers),
        )

    def test_module_not_found(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(
            spec, "GET", "/modules/{name}", client.get("/modules/absent", headers=headers)
        )

    def test_profile_lifecycle(
        self, client: TestClient, headers: dict[str, str], spec: Any
    ) -> None:
        created = client.post(
            "/profiles", headers=headers, json={"name": "contract-profile", "modules": []}
        )
        assert_matches(spec, "POST", "/profiles", created)
        assert_matches(
            spec,
            "PUT",
            "/profiles/{name}",
            client.put(
                "/profiles/contract-profile",
                headers=headers,
                json={"modules": [], "description": "x"},
            ),
        )
        assert_matches(
            spec,
            "POST",
            "/profiles/{name}/activate",
            client.post("/profiles/contract-profile/activate", headers=headers, json={}),
        )
        # The active profile cannot be deleted — a declared 409, and the only
        # place in the document where a conflict body is described.
        assert_matches(
            spec,
            "DELETE",
            "/profiles/{name}",
            client.delete("/profiles/contract-profile", headers=headers),
        )

    def test_session_lifecycle(
        self, client: TestClient, headers: dict[str, str], spec: Any
    ) -> None:
        created = client.post("/sessions", headers=headers, json={"name": "contract"})
        assert_matches(spec, "POST", "/sessions", created)
        session_id = created.json()["session_id"]

        assert_matches(
            spec,
            "GET",
            "/sessions/{session_id}",
            client.get(f"/sessions/{session_id}", headers=headers),
        )
        assert_matches(
            spec,
            "PATCH",
            "/sessions/{session_id}",
            client.patch(f"/sessions/{session_id}", headers=headers, json={"name": "renamed"}),
        )
        assert_matches(
            spec,
            "POST",
            "/sessions/{session_id}/stop",
            client.post(f"/sessions/{session_id}/stop", headers=headers, json={}),
        )
        assert_matches(
            spec,
            "GET",
            "/sessions/{session_id}/flows",
            client.get(f"/sessions/{session_id}/flows", headers=headers),
        )
        assert_matches(
            spec,
            "GET",
            "/sessions/{session_id}/export",
            client.get(f"/sessions/{session_id}/export", headers=headers),
        )
        assert_matches(
            spec,
            "DELETE",
            "/sessions/{session_id}",
            client.delete(f"/sessions/{session_id}", headers=headers),
        )

    def test_dryrun_against_the_live_ring(
        self, client: TestClient, headers: dict[str, str], spec: Any
    ) -> None:
        response = client.post(
            "/sessions/live/dryrun",
            headers=headers,
            json={
                "modules": [
                    {
                        "name": "contract-dry",
                        "files": {
                            "module.yaml": (
                                "name: contract-dry\npporlock_api: '1'\n"
                                "rules:\n"
                                "  - name: tag\n"
                                "    action: headers\n"
                                "    match: {host: a.example}\n"
                                "    response: {set: {x-contract: '1'}}\n"
                            )
                        },
                    }
                ]
            },
        )
        assert_matches(spec, "POST", "/sessions/{session_id}/dryrun", response)

    def test_attribution(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        response = client.post(
            "/attribution",
            headers={**headers, "X-Pporlock-Client": "extension"},
            json={
                "entries": [
                    {
                        "tab_id": 7,
                        "url": "https://a.example/one.js",
                        "method": "GET",
                        "timestamp": "2026-08-27T14:00:00.000Z",
                    }
                ]
            },
        )
        assert_matches(spec, "POST", "/attribution", response)

    def test_delete_flows(self, client: TestClient, headers: dict[str, str], spec: Any) -> None:
        assert_matches(spec, "DELETE", "/flows", client.delete("/flows", headers=headers))


class TestErrorBodies:
    """SPEC-0 §6.2's error envelope, wherever the document declares one."""

    def test_pair_rejects_a_wrong_code(
        self, client: TestClient, headers: dict[str, str], spec: Any
    ) -> None:
        response = client.post(
            "/pair", headers={"X-Pporlock-Client": "extension"}, json={"code": "nope"}
        )
        assert_matches(spec, "POST", "/pair", response)

    def test_post_state_rejects_a_cross_origin_form(
        self, client: TestClient, headers: dict[str, str], spec: Any
    ) -> None:
        """§2.5, A01: a form POST from an ordinary page must be refused.

        The declared 403 is what a client renders, so its shape is contract.
        """
        response = client.post(
            "/state",
            headers={**headers, "Origin": "https://evil.example"},
            json={"dev_toggles": {}},
        )
        assert response.status_code == 403
        assert_matches(spec, "POST", "/state", response)
