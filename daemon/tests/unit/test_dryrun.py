"""Dry run — REQ CAP-030, CAP-031, CAP-032, CAP-033.

The tests that matter here are the ones about *sameness*: the dry runner must
use the live evaluator's configuration and the ordinary module loading path, or
its output stops predicting live behaviour and the feature is worthless.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from pporlock.capture.dryrun import (
    MAX_LIMIT,
    CandidateModule,
    DryRunner,
    DryRunRequest,
    empty_result,
)
from pporlock.capture.records import FlowRecord
from pporlock.capture.redact import Redactor, is_masked
from pporlock.config import RedactionConfig
from pporlock.engine.evaluator import Evaluator
from pporlock.engine.models import NormalizedRequest, NormalizedResponse
from pporlock.engine.ruleset import RuleSet
from pporlock.errors import ConfigError

BLOCK_MODULE = """\
name: blocker
version: 1.0.0
pporlock_api: "1"
rules:
  - name: block the tracker
    action: block
    match:
      host: tracker.example.com
"""

CSP_MODULE = """\
name: csp-strip
version: 1.0.0
pporlock_api: "1"
rules:
  - name: strip csp
    action: headers
    match:
      host: app.example.com
    response:
      remove: [content-security-policy]
"""

HOOK_MODULE_YAML = """\
name: hooker
version: 1.0.0
pporlock_api: "1"
rules: []
"""

HOOK_MODULE_PY = """\
from pporlock.engine.models import ResponseMutation


def on_response(request, response, ctx):
    ctx.note("script_injected", "the hook ran")
    mutation = ResponseMutation()
    mutation.set("x-hooked", "yes")
    return mutation
"""


def flow(
    flow_id: str = "f1",
    host: str = "app.example.com",
    content_type: str = "text/html",
    body: bytes = b"<html><head></head></html>",
    response_headers: tuple[tuple[str, str], ...] | None = None,
) -> FlowRecord:
    request = NormalizedRequest(
        flow_id=flow_id,
        timestamp="2026-08-27T14:00:00.000Z",
        scheme="https",
        method="GET",
        host=host,
        port=443,
        path="/index.html",
        url=f"https://{host}/index.html",
        headers=(("accept", "*/*"),),
    )
    headers = response_headers or (
        ("content-type", content_type),
        ("content-security-policy", "default-src 'self'"),
    )
    response = NormalizedResponse(
        flow_id=flow_id,
        timestamp="2026-08-27T14:00:01.000Z",
        status=200,
        headers=headers,
        body=body,
    )
    return FlowRecord(
        flow_id=flow_id,
        kind="http",
        started_at="2026-08-27T14:00:00.000Z",
        completed_at="2026-08-27T14:00:01.000Z",
        request=request,
        response=response,
    )


@pytest.fixture
def runner(tmp_path: Path) -> DryRunner:
    installed = tmp_path / "modules"
    installed.mkdir()
    return DryRunner(Evaluator(), installed_root=installed)


def candidate(name: str, manifest: str, python: str | None = None) -> DryRunRequest:
    files = {"module.yaml": manifest}
    if python is not None:
        files["module.py"] = python
    return DryRunRequest(candidate_modules=(CandidateModule(name=name, files=files),))


class TestDryRunRequest:
    def test_parses_the_spec_0_shape(self) -> None:  # SPEC-0 §6.8
        request = DryRunRequest.from_dict(
            {
                "modules": [{"name": "candidate", "files": {"module.yaml": BLOCK_MODULE}}],
                "use_installed": ["strip-sri"],
                "profile": None,
                "limit": 100,
                "include_diffs": True,
            }
        )
        assert request.candidate_modules[0].name == "candidate"
        assert request.use_installed == ("strip-sri",)
        assert request.limit == 100
        assert request.include_diffs is True

    def test_limit_is_clamped(self) -> None:
        request = DryRunRequest.from_dict({"use_installed": ["a"], "limit": MAX_LIMIT * 10})
        assert request.limit == MAX_LIMIT
        assert DryRunRequest.from_dict({"use_installed": ["a"], "limit": -5}).limit == 1

    @pytest.mark.parametrize(
        "body",
        [
            [],
            {},
            {"modules": "not a list"},
            {"modules": ["not a mapping"]},
            {"modules": [{"files": {"module.yaml": "x"}}]},
            {"modules": [{"name": "a"}]},
            {"modules": [{"name": "a", "files": {"evil.sh": "rm -rf /"}}]},
            {"use_installed": "not a list"},
            {"use_installed": ["a"], "limit": "soon"},
        ],
    )
    def test_refuses_a_malformed_request(self, body: Any) -> None:
        with pytest.raises(ConfigError):
            DryRunRequest.from_dict(body)

    def test_a_candidate_may_not_smuggle_an_arbitrary_filename(self) -> None:
        """Only the files the loader reads may be materialised, so there is no
        caller-controlled path component to escape the temporary directory."""
        with pytest.raises(ConfigError):
            DryRunRequest.from_dict(
                {"modules": [{"name": "a", "files": {"../../etc/passwd": "x"}}]}
            )


class TestSameCodePath:
    def test_the_evaluator_is_cloned_from_the_live_one(self, tmp_path: Path) -> None:
        """REQ CAP-031 — dry run must inherit the live evaluator's settings."""
        live = Evaluator(
            RuleSet(),
            asset_root=tmp_path / "assets",
            max_buffer_bytes=999,
            offload_threshold=123,
            buffer_types=("text/plain",),
        )
        clone = live.clone_with(ruleset=RuleSet(), registry=None)
        assert clone.asset_root == live.asset_root
        assert clone.max_buffer_bytes == 999
        assert clone.offload_threshold == 123
        assert clone.buffer_types == ("text/plain",)
        assert clone.stubs is live.stubs
        assert clone.exclusions is live.exclusions

    def test_clone_with_covers_every_configured_attribute(self) -> None:
        """A new Evaluator setting that clone_with forgot would silently make
        the dry run stop predicting live behaviour. Enumerated, so adding one
        without extending the clone fails here rather than in production."""
        configured = set(Evaluator.__slots__)
        parameters = set(inspect.signature(Evaluator.__init__).parameters) - {"self"}
        assert configured == parameters, (
            "every constructor argument is a configured attribute and vice versa"
        )
        source = inspect.getsource(Evaluator.clone_with)
        for name in configured:
            assert name in source, f"clone_with does not carry {name} over"

    def test_candidate_modules_load_through_the_ordinary_loader(
        self, runner: DryRunner
    ) -> None:  # REQ CAP-031
        result = runner.run([flow()], candidate("csp-strip", CSP_MODULE))
        assert result["modules"]["loaded"] == 1
        assert result["modules"]["errors"] == []
        assert result["modules"]["rules"] == 1

    def test_a_module_that_will_not_load_is_reported_not_swallowed(
        self, runner: DryRunner
    ) -> None:  # REQ MOD-005
        result = runner.run([flow()], candidate("csp-strip", "name: csp-strip\n"))
        assert result["modules"]["errors"]
        assert result["summary"]["matched"] == 0


class TestPythonHooksExecute:
    def test_hooks_run(self, runner: DryRunner) -> None:  # REQ CAP-032
        """Deliberate, documented, and not a bug: dry-running an
        agent-authored module runs that agent's code."""
        result = runner.run([flow()], candidate("hooker", HOOK_MODULE_YAML, HOOK_MODULE_PY))
        assert result["summary"]["matched"] == 1
        ops = result["results"][0]["diff"]["headers"]
        assert {"op": "replace", "name": "x-hooked", "value": "yes", "phase": "response"} in ops

    def test_the_candidate_python_does_not_stay_in_sys_modules(self, runner: DryRunner) -> None:
        import sys

        runner.run([flow()], candidate("hooker", HOOK_MODULE_YAML, HOOK_MODULE_PY))
        assert "pporlock_module_hooker" not in sys.modules

    def test_a_candidates_on_load_cannot_extend_the_live_transform_registry(
        self, tmp_path: Path
    ) -> None:
        """State is isolated even though the code path is shared."""
        live = Evaluator()
        before = live.transforms.names
        runner = DryRunner(live, installed_root=tmp_path)
        python = (
            "def on_load(ctx):\n    ctx.register_transform('sneaky', lambda text, params: text)\n"
        )
        runner.run([flow()], candidate("hooker", HOOK_MODULE_YAML, python))
        assert live.transforms.names == before


class TestSummaryAndDiffs:
    def test_counts_blocked_flows(self, runner: DryRunner) -> None:  # REQ CAP-033
        flows = [flow("f1", host="tracker.example.com"), flow("f2")]
        result = runner.run(flows, candidate("blocker", BLOCK_MODULE))
        assert result["summary"]["flows_evaluated"] == 2
        assert result["summary"]["matched"] == 1
        assert result["summary"]["blocked"] == 1
        assert result["results"][0]["flow_id"] == "f1"

    def test_unaffected_flows_are_collapsed_out_of_the_results(
        self, runner: DryRunner
    ) -> None:  # REQ CAP-033, WUI-010
        flows = [flow(f"f{i}", host="other.example.com") for i in range(5)]
        result = runner.run(flows, candidate("blocker", BLOCK_MODULE))
        assert result["summary"]["flows_evaluated"] == 5
        assert result["results"] == []

    def test_aggregates_by_module_rule_and_note(self, runner: DryRunner) -> None:  # REQ CAP-033
        result = runner.run([flow(), flow("f2")], candidate("csp-strip", CSP_MODULE))
        summary = result["summary"]
        assert summary["by_module"] == {"csp-strip": 2}
        assert summary["by_rule"] == {"csp-strip:0": 2}
        assert summary["by_note"]["csp_modified"] == 2

    def test_header_diff_is_an_op_list(self, runner: DryRunner) -> None:  # REQ CAP-033
        result = runner.run([flow()], candidate("csp-strip", CSP_MODULE))
        ops = result["results"][0]["diff"]["headers"]
        assert {
            "op": "remove",
            "name": "content-security-policy",
            "value": None,
            "phase": "response",
        } in ops

    def test_body_diff_is_unified_for_text(self, tmp_path: Path) -> None:  # REQ CAP-033
        manifest = """\
name: injector
version: 1.0.0
pporlock_api: "1"
rules:
  - name: inject
    action: body
    match:
      host: app.example.com
    transforms:
      - kind: inject_script
        position: head_end
        inline: "x()"
"""
        runner = DryRunner(Evaluator(), installed_root=tmp_path)
        result = runner.run([flow()], candidate("injector", manifest))
        body = result["results"][0]["diff"]["body"]
        assert body["kind"] == "unified"
        assert "x()" in body["text"]
        assert body["truncated"] is False

    def test_a_long_body_diff_is_truncated_and_says_so(self, tmp_path: Path) -> None:
        manifest = """\
name: injector
version: 1.0.0
pporlock_api: "1"
rules:
  - name: inject
    action: body
    match:
      host: app.example.com
    transforms:
      - kind: inject_script
        position: head_end
        inline: "x()"
"""
        runner = DryRunner(Evaluator(), installed_root=tmp_path, max_diff_chars=20)
        result = runner.run([flow()], candidate("injector", manifest))
        body = result["results"][0]["diff"]["body"]
        assert body["truncated"] is True
        assert len(body["text"]) == 20

    def test_include_diffs_false_omits_them(self, runner: DryRunner) -> None:  # REQ MCP-005
        request = candidate("csp-strip", CSP_MODULE)
        request = DryRunRequest(candidate_modules=request.candidate_modules, include_diffs=False)
        result = runner.run([flow()], request)
        assert "diff" not in result["results"][0]

    def test_the_limit_bounds_the_work(self, runner: DryRunner) -> None:
        flows = [flow(f"f{i}") for i in range(10)]
        request = DryRunRequest(
            candidate_modules=candidate("csp-strip", CSP_MODULE).candidate_modules, limit=3
        )
        result = runner.run(flows, request)
        assert result["summary"]["flows_evaluated"] == 3

    def test_a_tunnelled_flow_is_skipped_not_counted_as_evaluated(self, runner: DryRunner) -> None:
        passthrough = FlowRecord(
            flow_id="p1",
            kind="passthrough",
            started_at="2026-08-27T14:00:00.000Z",
            passthrough_host="updates.apple.com",
        )
        result = runner.run([passthrough, flow()], candidate("csp-strip", CSP_MODULE))
        assert result["summary"]["flows_skipped"] == 1
        assert result["summary"]["flows_evaluated"] == 1

    def test_percentiles_are_reported(self, runner: DryRunner) -> None:  # REQ CAP-033
        result = runner.run([flow(f"f{i}") for i in range(20)], candidate("csp-strip", CSP_MODULE))
        assert result["summary"]["p95_ms"] >= 0.0
        assert result["summary"]["avg_ms"] >= 0.0

    def test_an_empty_result_has_the_same_shape(self) -> None:
        result = empty_result()
        assert result["summary"]["flows_evaluated"] == 0
        assert result["results"] == []


class TestRedaction:
    def test_a_masked_header_value_does_not_appear_in_a_diff(
        self, tmp_path: Path
    ) -> None:  # REQ CAP-040
        """Ring records are stored unredacted so one value can be unmasked
        (REQ CAP-043). A dry-run diff has no such affordance, so a secret must
        not reach one."""
        manifest = """\
name: setter
version: 1.0.0
pporlock_api: "1"
rules:
  - name: set a cookie
    action: headers
    match:
      host: app.example.com
    response:
      set:
        set-cookie: "session=super-secret-value"
"""
        runner = DryRunner(
            Evaluator(), installed_root=tmp_path, redactor=Redactor(RedactionConfig())
        )
        result = runner.run([flow()], candidate("setter", manifest))
        op = next(o for o in result["results"][0]["diff"]["headers"] if o["name"] == "set-cookie")
        assert is_masked(str(op["value"]))
        assert "super-secret-value" not in str(op["value"])

    def test_a_binary_body_gets_a_length_and_hash_summary(self, tmp_path: Path) -> None:
        """A unified diff of a PNG is noise; a length and a hash is a fact."""
        runner = DryRunner(Evaluator(), installed_root=tmp_path)
        record = flow(content_type="image/png", body=b"\x89PNG\r\n")
        assert record.response is not None
        diff = runner._body_diff(record.response, b"\x89PNG\r\n\x00\x01")
        assert diff is not None
        assert diff["kind"] == "binary"
        assert "6 bytes -> 8 bytes" in diff["text"]
        assert diff["truncated"] is False

    def test_an_unchanged_body_produces_no_diff(self, tmp_path: Path) -> None:
        runner = DryRunner(Evaluator(), installed_root=tmp_path)
        record = flow()
        assert record.response is not None
        assert runner._body_diff(record.response, record.response.body) is None
        assert runner._body_diff(record.response, None) is None


class TestInstalledModules:
    def test_an_installed_module_is_copied_and_run(self, tmp_path: Path) -> None:
        installed = tmp_path / "modules"
        (installed / "csp-strip").mkdir(parents=True)
        (installed / "csp-strip" / "module.yaml").write_text(CSP_MODULE)
        runner = DryRunner(Evaluator(), installed_root=installed)
        result = runner.run([flow()], DryRunRequest(use_installed=("csp-strip",)))
        assert result["summary"]["matched"] == 1

    def test_an_installed_module_runs_even_when_disabled_live(self, tmp_path: Path) -> None:
        """The question a dry run answers is "what would this do", and a
        disabled module doing nothing is not an answer."""
        installed = tmp_path / "modules"
        (installed / "csp-strip").mkdir(parents=True)
        (installed / "csp-strip" / "module.yaml").write_text(
            CSP_MODULE.replace("rules:", "enabled: false\nrules:")
        )
        runner = DryRunner(Evaluator(), installed_root=installed)
        result = runner.run([flow()], DryRunRequest(use_installed=("csp-strip",)))
        assert result["summary"]["matched"] == 1

    def test_a_missing_installed_module_is_refused(self, runner: DryRunner) -> None:
        with pytest.raises(ConfigError):
            runner.run([flow()], DryRunRequest(use_installed=("nope",)))

    def test_a_candidate_of_the_same_name_wins(self, tmp_path: Path) -> None:
        installed = tmp_path / "modules"
        (installed / "csp-strip").mkdir(parents=True)
        (installed / "csp-strip" / "module.yaml").write_text(
            CSP_MODULE.replace("host: app.example.com", "host: nowhere.example.com")
        )
        runner = DryRunner(Evaluator(), installed_root=installed)
        request = DryRunRequest(
            candidate_modules=(
                CandidateModule(name="csp-strip", files={"module.yaml": CSP_MODULE}),
            ),
            use_installed=("csp-strip",),
        )
        result = runner.run([flow()], request)
        assert result["summary"]["matched"] == 1

    def test_the_temporary_directory_is_removed(self, tmp_path: Path) -> None:
        import tempfile

        before = set(Path(tempfile.gettempdir()).glob("pporlock-dryrun-*"))
        runner = DryRunner(Evaluator(), installed_root=tmp_path)
        runner.run([flow()], candidate("csp-strip", CSP_MODULE))
        after = set(Path(tempfile.gettempdir()).glob("pporlock-dryrun-*"))
        assert after <= before
