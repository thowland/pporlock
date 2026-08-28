"""Candidate-module validation — REQ API-027, MCP-012.

Covers ``engine/modules/validate.py`` and the ``POST /validate`` route it backs.
The shape asserted here is the one ``web/src/api/types.ts`` already reads and
``mcp/src/pporlock_mcp/tools.py`` already sends.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from pporlock.capture.ring import RingBuffer
from pporlock.config import Config
from pporlock.control.app import OFFLOAD_ROUTES, ControlApp
from pporlock.engine.modules.loader import load_module
from pporlock.engine.modules.validate import validate_module_files

GOOD_MANIFEST = """\
name: tidy
version: 1.0.0
pporlock_api: "1"
rules:
  - name: strip csp
    action: headers
    match:
      host: example.com
    response:
      remove: [content-security-policy]
"""

GOOD_PYTHON = """\
def on_request(ctx, request):
    return None
"""


class TestValidateModuleFiles:
    def test_a_valid_module_has_no_errors(self) -> None:  # REQ API-027
        report = validate_module_files(
            "tidy", {"module.yaml": GOOD_MANIFEST, "module.py": GOOD_PYTHON}
        )
        assert report.ok
        assert report.errors == ()

    def test_a_manifest_is_required(self) -> None:  # REQ API-027
        report = validate_module_files("tidy", {"module.py": GOOD_PYTHON})
        assert not report.ok
        assert report.errors[0].code == "module_missing_manifest"

    def test_broken_yaml_reports_a_line_and_column(self) -> None:  # REQ API-027
        """The web UI turns line/column into editor markers, so they must be real."""
        report = validate_module_files("tidy", {"module.yaml": "name: tidy\n  bad: [1, 2\n"})
        issue = report.errors[0]
        assert issue.code == "module_invalid_yaml"
        assert issue.file == "module.yaml"
        assert issue.line is not None and issue.line >= 1
        assert issue.column is not None

    def test_a_syntax_error_reports_the_python_line(self) -> None:  # REQ API-027
        report = validate_module_files(
            "tidy", {"module.yaml": GOOD_MANIFEST, "module.py": "def on_request(:\n    pass\n"}
        )
        issue = next(i for i in report.errors if i.file == "module.py")
        assert issue.code == "module_syntax_error"
        assert issue.line == 1

    def test_python_is_not_executed(self, tmp_path: Path) -> None:  # REQ API-027
        """Validation must not run the candidate's code.

        Validation is the step an author performs *before* agreeing to run a
        module. A validator that executed it would have already done the thing
        being decided about.
        """
        marker = tmp_path / "executed"
        source = f"open({str(marker)!r}, 'w').write('yes')\n\ndef on_request(ctx, r):\n    pass\n"
        report = validate_module_files("tidy", {"module.yaml": GOOD_MANIFEST, "module.py": source})
        assert report.ok
        assert not marker.exists()

    def test_an_unknown_manifest_key_is_placed_on_its_line(self) -> None:  # REQ MOD-014
        manifest = GOOD_MANIFEST.replace("version:", "verison:")
        report = validate_module_files("tidy", {"module.yaml": manifest})
        issue = next(i for i in report.errors if i.code == "module_unknown_key")
        assert issue.line == 2

    def test_a_name_mismatch_is_an_error(self) -> None:  # REQ MOD-005
        report = validate_module_files("other", {"module.yaml": GOOD_MANIFEST})
        assert any(i.code == "module_name_mismatch" for i in report.errors)

    def test_an_unsupported_api_version_is_an_error(self) -> None:  # REQ MOD-026
        manifest = GOOD_MANIFEST.replace('pporlock_api: "1"', 'pporlock_api: "9"')
        report = validate_module_files("tidy", {"module.yaml": manifest})
        assert any(i.code == "module_api_unsupported" for i in report.errors)

    def test_a_bad_rule_is_reported_with_the_rule_name(self) -> None:  # REQ MOD-014
        manifest = GOOD_MANIFEST.replace("action: headers", "action: teleport")
        report = validate_module_files("tidy", {"module.yaml": manifest})
        issue = report.errors[0]
        assert "strip csp" in issue.message
        assert issue.line is not None

    def test_rules_must_be_a_list(self) -> None:
        report = validate_module_files(
            "tidy", {"module.yaml": 'name: tidy\npporlock_api: "1"\nrules: nope\n'}
        )
        assert any(i.code == "module_invalid_rules" for i in report.errors)

    def test_a_non_mapping_rule_is_reported(self) -> None:
        report = validate_module_files(
            "tidy", {"module.yaml": 'name: tidy\npporlock_api: "1"\nrules: [3]\n'}
        )
        assert any(i.code == "rule_invalid" for i in report.errors)

    def test_a_non_mapping_manifest_is_reported(self) -> None:
        report = validate_module_files("tidy", {"module.yaml": "- a\n- b\n"})
        assert report.errors[0].code == "module_invalid_manifest"

    def test_a_non_integer_priority_is_reported(self) -> None:
        manifest = 'name: tidy\npporlock_api: "1"\npriority: soon\nrules: []\n'
        report = validate_module_files("tidy", {"module.yaml": manifest})
        assert any(i.code == "module_invalid_manifest" for i in report.errors)

    def test_an_invalid_module_name_is_reported(self) -> None:
        report = validate_module_files("Not A Name", {"module.yaml": GOOD_MANIFEST})
        assert any(i.code == "module_invalid_name" for i in report.errors)

    def test_a_file_the_loader_never_reads_is_reported(self) -> None:
        report = validate_module_files(
            "tidy", {"module.yaml": GOOD_MANIFEST, "helpers.py": "x = 1\n"}
        )
        assert any(i.code == "module_unknown_file" for i in report.errors)

    def test_python_with_no_hooks_is_a_warning_not_an_error(self) -> None:
        report = validate_module_files(
            "tidy", {"module.yaml": GOOD_MANIFEST, "module.py": "VALUE = 1\n"}
        )
        assert report.ok
        assert [i.code for i in report.warnings] == ["module_no_hooks"]

    def test_a_null_byte_is_reported_rather_than_raised(self) -> None:
        report = validate_module_files(
            "tidy", {"module.yaml": GOOD_MANIFEST, "module.py": "x = 1\x00\n"}
        )
        assert any(i.code == "module_syntax_error" for i in report.errors)


class TestValidateAgreesWithTheLoader:
    def test_a_module_that_validates_clean_loads_clean(self, tmp_path: Path) -> None:
        """REQ CAP-031's promise, one step earlier: validation must not accept
        something the loader then refuses."""
        report = validate_module_files(
            "tidy", {"module.yaml": GOOD_MANIFEST, "module.py": GOOD_PYTHON}
        )
        assert report.ok

        directory = tmp_path / "tidy"
        directory.mkdir()
        (directory / "module.yaml").write_text(GOOD_MANIFEST)
        (directory / "module.py").write_text(GOOD_PYTHON)
        loaded = load_module(directory)
        assert loaded.state == "loaded"
        assert loaded.error is None

    def test_a_module_the_loader_refuses_does_not_validate(self, tmp_path: Path) -> None:
        manifest = GOOD_MANIFEST.replace("action: headers", "action: teleport")
        report = validate_module_files("tidy", {"module.yaml": manifest})
        assert not report.ok

        directory = tmp_path / "tidy"
        directory.mkdir()
        (directory / "module.yaml").write_text(manifest)
        assert load_module(directory).state == "load_error"


@pytest.fixture
def app(tmp_path: Path) -> ControlApp:
    config = Config(state_dir=str(tmp_path))
    return ControlApp(config, ring=RingBuffer())


@pytest.fixture
def client(app: ControlApp) -> TestClient:
    return TestClient(app.asgi)


@pytest.fixture
def headers(app: ControlApp) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {app.tokens.ensure()}",
        "X-Pporlock-Client": "ui",
    }


class TestValidateRoute:
    def test_returns_the_shape_the_web_ui_reads(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ API-027
        response = client.post(
            "/validate",
            headers=headers,
            json={"name": "tidy", "files": {"module.yaml": GOOD_MANIFEST}},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        # `valid` is what contracts/openapi.yaml declares; `ok` is what the web
        # UI reads. Both are emitted and both mean the same thing.
        assert body["valid"] is True
        assert body["errors"] == []
        assert body["warnings"] == []

    def test_errors_carry_editor_marker_fields(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ API-027
        response = client.post(
            "/validate",
            headers=headers,
            json={"name": "tidy", "files": {"module.yaml": "name: tidy\n", "module.py": "def (\n"}},
        )
        body = response.json()
        assert body["ok"] is False
        for issue in body["errors"]:
            assert set(issue) >= {"code", "message", "file", "line", "column", "severity"}

    def test_files_are_required(self, client: TestClient, headers: dict[str, str]) -> None:
        response = client.post("/validate", headers=headers, json={"name": "tidy"})
        assert response.status_code == 400

    def test_a_non_mapping_body_is_refused(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:
        response = client.post("/validate", headers=headers, json=["files"])
        assert response.status_code == 400

    def test_it_installs_nothing(
        self, app: ControlApp, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ API-027
        client.post(
            "/validate",
            headers=headers,
            json={"name": "tidy", "files": {"module.yaml": GOOD_MANIFEST}},
        )
        assert not (Path(app.config.modules.root) / "tidy").exists()

    def test_the_route_is_classified_as_offloaded(self) -> None:  # REQ API-002
        assert "/validate" in OFFLOAD_ROUTES


class TestNameIsOptional:
    def test_the_manifests_own_name_is_used_when_none_is_given(self) -> None:
        """The web UI's editor validates a file it has not yet named. Defaulting
        to a made-up name would report a mismatch the caller never caused."""
        report = validate_module_files(None, {"module.yaml": GOOD_MANIFEST})
        assert report.ok

    def test_a_manifest_with_no_name_is_still_reported(self) -> None:
        report = validate_module_files(None, {"module.yaml": 'pporlock_api: "1"\n'})
        assert any(i.code == "module_invalid_name" for i in report.errors)

    def test_the_web_ui_body_with_only_files_validates_clean(
        self, client: TestClient, headers: dict[str, str]
    ) -> None:  # REQ API-027
        """web/src/api/client.ts::validateModule posts {files} and nothing else."""
        response = client.post(
            "/validate", headers=headers, json={"files": {"module.yaml": GOOD_MANIFEST}}
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True
