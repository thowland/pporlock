"""Dry run — SPEC-1 §6.5, REQ CAP-030, CAP-031, CAP-032, CAP-033.

Replays captured flows — from a recorded session or from the live ring — through
a candidate module set, and reports what *would* have happened.

The single load-bearing property is that this predicts live behaviour, and the
only way to guarantee that is to refuse to have a second implementation:

* the evaluator is built by ``Evaluator.clone_with`` from the running one, so
  every buffering bound, stub, transform and asset root is the live one
  (REQ CAP-031);
* candidate modules are materialised into a directory and loaded through the
  ordinary ``ModuleLoader``/``ModuleRegistry`` path, so a module that dry-runs
  cleanly loads cleanly (REQ CAP-031);
* **Python hooks execute** (REQ CAP-032). Deliberately, and documented in
  ``docs/module-authoring.md``: dry-running an agent-authored module runs that
  agent's code, and a dry run that skipped the Python tier would report that a
  module does nothing when it does the most.

What is isolated is *state*, not code: the transform registry is copied so a
candidate's ``on_load`` cannot extend the live one, the module store lives in
the temporary directory, and nothing here touches the running proxy.

The run is blocking — a temporary directory, module import, and body diffing —
so the control route offloads it (REQ API-002).
"""

from __future__ import annotations

import difflib
import hashlib
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..engine.evaluator import Evaluator, TimeBudget
from ..engine.models import NormalizedRequest, NormalizedResponse
from ..engine.modules.loader import WRITABLE_FILES, unload_python
from ..engine.modules.registry import ModuleRegistry
from ..engine.provenance import ProvenanceBuilder
from ..errors import ConfigError
from .records import FlowRecord

if TYPE_CHECKING:
    from .redact import Redactor

#: Per-flow cap on diff text. A dry run over five hundred flows that returned
#: every byte of every changed body would be larger than the session it read.
DEFAULT_MAX_DIFF_CHARS = 20_000

#: Default and ceiling for how many flows one run evaluates.
DEFAULT_LIMIT = 500
MAX_LIMIT = 5_000

#: Content types whose body diff is shown as text. Anything else gets a
#: length-and-hash summary, because a unified diff of a PNG is noise.
TEXTUAL_HINTS = ("text/", "javascript", "json", "xml", "html", "css", "svg")


@dataclass(frozen=True, slots=True)
class CandidateModule:
    """An uninstalled module: a name and its file contents."""

    name: str
    files: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class DryRunRequest:
    candidate_modules: tuple[CandidateModule, ...] = ()
    use_installed: tuple[str, ...] = ()
    profile: str | None = None
    limit: int = DEFAULT_LIMIT
    include_diffs: bool = True

    @classmethod
    def from_dict(cls, body: Any) -> DryRunRequest:
        """Parse the wire shape of SPEC-0 §6.8. Strict about what it accepts."""
        if not isinstance(body, dict):
            raise ConfigError("the dry-run request body must be a mapping")

        candidates: list[CandidateModule] = []
        raw_modules = body.get("modules") or []
        if not isinstance(raw_modules, list):
            raise ConfigError("'modules' must be a list of {name, files}")
        for entry in raw_modules:
            if not isinstance(entry, dict):
                raise ConfigError("each entry of 'modules' must be a mapping")
            name = str(entry.get("name") or "").strip()
            files = entry.get("files")
            if not name:
                raise ConfigError("a candidate module needs a 'name'")
            if not isinstance(files, dict) or not files:
                raise ConfigError(
                    f"candidate module {name!r} needs a non-empty 'files' mapping", module=name
                )
            unknown = sorted(set(files) - set(WRITABLE_FILES))
            if unknown:
                raise ConfigError(
                    f"cannot materialise {', '.join(unknown)} for {name!r}", module=name
                )
            candidates.append(
                CandidateModule(name=name, files={k: str(v) for k, v in files.items()})
            )

        raw_installed = body.get("use_installed") or []
        if not isinstance(raw_installed, list):
            raise ConfigError("'use_installed' must be a list of module names")

        if not candidates and not raw_installed:
            raise ConfigError(
                "a dry run needs at least one candidate module or one installed module name"
            )

        profile = body.get("profile")
        try:
            limit = int(body.get("limit", DEFAULT_LIMIT))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"limit must be an integer: {body.get('limit')!r}") from exc

        return cls(
            candidate_modules=tuple(candidates),
            use_installed=tuple(str(n) for n in raw_installed),
            profile=str(profile) if profile is not None else None,
            limit=max(1, min(limit, MAX_LIMIT)),
            include_diffs=bool(body.get("include_diffs", True)),
        )


@dataclass(slots=True)
class _Tally:
    """Accumulates the aggregate half of REQ CAP-033."""

    evaluated: int = 0
    skipped: int = 0
    matched: int = 0
    modified: int = 0
    blocked: int = 0
    errors: int = 0
    durations: list[float] = field(default_factory=list)
    by_module: dict[str, int] = field(default_factory=dict)
    by_rule: dict[str, int] = field(default_factory=dict)
    by_note: dict[str, int] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        ordered = sorted(self.durations)
        return {
            "flows_evaluated": self.evaluated,
            "flows_skipped": self.skipped,
            "matched": self.matched,
            "modified": self.modified,
            "blocked": self.blocked,
            "errors": self.errors,
            "avg_ms": round(sum(ordered) / len(ordered), 3) if ordered else 0.0,
            "p95_ms": round(_percentile(ordered, 0.95), 3),
            "by_module": dict(sorted(self.by_module.items(), key=lambda kv: (-kv[1], kv[0]))),
            "by_rule": dict(sorted(self.by_rule.items(), key=lambda kv: (-kv[1], kv[0]))),
            "by_note": dict(sorted(self.by_note.items(), key=lambda kv: (-kv[1], kv[0]))),
        }


def _percentile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, round(fraction * (len(ordered) - 1)))
    return ordered[index]


class DryRunner:
    """Replays flows through a candidate module set."""

    __slots__ = ("_evaluator", "budget_ms", "installed_root", "max_diff_chars", "redactor")

    def __init__(
        self,
        evaluator: Evaluator,
        *,
        installed_root: Path,
        redactor: Redactor | None = None,
        max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS,
        budget_ms: float = 250.0,
    ) -> None:
        #: The *live* evaluator. Never evaluated against directly — cloned, so
        #: the dry run inherits its configuration and none of its rules.
        self._evaluator = evaluator
        self.installed_root = installed_root
        self.redactor = redactor
        self.max_diff_chars = max_diff_chars
        self.budget_ms = budget_ms

    # -- module set ------------------------------------------------------

    def _materialise(self, root: Path, request: DryRunRequest) -> None:
        for candidate in request.candidate_modules:
            directory = root / candidate.name
            directory.mkdir(parents=True, exist_ok=True)
            for filename, contents in candidate.files.items():
                # Names were checked against WRITABLE_FILES at parse time, so
                # there is no caller-controlled path component here.
                (directory / filename).write_text(contents)

        for name in request.use_installed:
            source = self.installed_root / name
            if not source.is_dir():
                raise ConfigError(f"no installed module {name!r} to dry-run", module=name)
            if (root / name).exists():
                # A candidate of the same name is the newer version of it, and
                # is what the caller is asking about.
                continue
            shutil.copytree(source, root / name, symlinks=False)

    def run(self, flows: Iterable[FlowRecord], request: DryRunRequest) -> dict[str, Any]:
        """Evaluate ``flows`` against the request's module set.

        Blocking. Always called through ``ControlApp.offload``.
        """
        root = Path(tempfile.mkdtemp(prefix="pporlock-dryrun-"))
        profile = request.profile or "default"
        names: list[str] = []
        try:
            self._materialise(root, request)
            registry = ModuleRegistry(root, store_path=root / "module-store.db")
            # A copy, so a candidate's on_load cannot extend the live registry.
            transforms = self._evaluator.transforms.copy()
            reload_result = registry.reload(transforms, profile)
            names = [m.name for m in registry.modules]

            # Everything under test is enabled for the run regardless of its
            # manifest or its live enablement. The question a dry run answers is
            # "what would this module do", and a disabled module doing nothing
            # is not an answer (REQ MCP-030 governs installation, not this).
            for module in registry.modules:
                registry.set_enabled(module.name, True)

            ruleset = registry.build_ruleset()
            evaluator = self._evaluator.clone_with(ruleset=ruleset, registry=registry)
            result = self._replay(flows, evaluator, request, profile)
            result["modules"] = {
                "loaded": reload_result.loaded,
                "errors": [
                    m.error.to_dict() | {"module": m.name}
                    for m in reload_result.errors
                    if m.error is not None
                ],
                "rules": len(ruleset),
            }
            return result
        finally:
            # The candidate's Python is dropped from sys.modules so a later live
            # reload re-executes the installed file rather than reusing the
            # candidate's, and so a dry run leaves nothing behind.
            for name in names:
                unload_python(name)
            shutil.rmtree(root, ignore_errors=True)

    # -- replay ----------------------------------------------------------

    def _replay(
        self,
        flows: Iterable[FlowRecord],
        evaluator: Evaluator,
        request: DryRunRequest,
        profile: str,
    ) -> dict[str, Any]:
        tally = _Tally()
        results: list[dict[str, Any]] = []

        for record in flows:
            if tally.evaluated + tally.skipped >= request.limit:
                break
            if record.request is None:
                # A tunnelled connection was never decrypted, so no rule after
                # the ClientHello phase could ever have seen it.
                tally.skipped += 1
                continue
            entry = self._replay_one(record, record.request, evaluator, profile, request)
            tally.evaluated += 1
            tally.durations.append(float(entry.pop("_duration_ms")))
            affected = bool(entry.pop("_affected"))
            if not affected:
                continue

            tally.matched += 1
            if entry.get("blocked"):
                tally.blocked += 1
            if entry.get("modified"):
                tally.modified += 1
            provenance = entry["provenance"]
            errors = sum(1 for e in provenance["entries"] if e.get("outcome") == "error")
            tally.errors += errors
            for module in provenance["evaluated_modules"]:
                tally.by_module[module] = tally.by_module.get(module, 0) + 1
            for compiled in {e.get("rule_id") for e in provenance["entries"] if e.get("rule_id")}:
                tally.by_rule[str(compiled)] = tally.by_rule.get(str(compiled), 0) + 1
            for code in {str(n.get("code")) for n in provenance["notes"]}:
                tally.by_note[code] = tally.by_note.get(code, 0) + 1
            results.append(entry)

        return {"summary": tally.summary(), "results": results}

    def _replay_one(
        self,
        record: FlowRecord,
        req: NormalizedRequest,
        evaluator: Evaluator,
        profile: str,
        request: DryRunRequest,
    ) -> dict[str, Any]:
        builder = ProvenanceBuilder(profile)
        budget = TimeBudget(self.budget_ms)

        request_decision = evaluator.evaluate_request(req, builder, budget)
        response_decision = None
        if not request_decision.blocked and record.response is not None:
            response_decision = evaluator.evaluate_response(req, record.response, builder, budget)
        provenance = builder.build()

        headers = self._header_ops(request_decision, response_decision)
        body = None
        if response_decision is not None and record.response is not None:
            body = self._body_diff(record.response, response_decision.mutation.body)

        modified = bool(headers) or body is not None
        entry: dict[str, Any] = {
            "flow_id": record.flow_id,
            "url": req.url,
            "method": req.method,
            "status": record.status,
            "blocked": request_decision.blocked,
            "modified": modified,
            "provenance": provenance.to_dict(),
            "_duration_ms": provenance.total_ms or budget.spent,
            # A flow is affected when something fired on it. Provenance is the
            # authority for that, not the diff: a rule that matched and made no
            # change is a finding, and collapsing it into "unaffected" is how a
            # module that is doing nothing looks like a module that is working.
            "_affected": bool(provenance.entries) or bool(provenance.notes),
        }
        if request.include_diffs:
            entry["diff"] = {"headers": headers, "body": body}
        return entry

    # -- diffs (REQ CAP-033) ---------------------------------------------

    def _mask(self, name: str, value: str | None) -> str | None:
        """Mask a header value the redaction policy covers.

        Live ring records are stored unredacted so the UI can unmask one value
        at a time (REQ CAP-043); a dry-run diff has no such affordance, so a
        secret must not appear in one (REQ CAP-040).
        """
        if value is None or self.redactor is None or not self.redactor.enabled:
            return value
        if self.redactor.masks_header(name):
            from .redact import mask

            return mask(value)
        return value

    def _header_ops(self, request_decision: Any, response_decision: Any) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        for phase, decision in (("request", request_decision), ("response", response_decision)):
            if decision is None:
                continue
            mutation = decision.mutation
            for name in sorted(mutation.remove_headers):
                ops.append({"op": "remove", "name": name, "value": None, "phase": phase})
            for name, value in sorted(mutation.set_headers.items()):
                ops.append(
                    {
                        "op": "replace",
                        "name": name,
                        "value": self._mask(name, value),
                        "phase": phase,
                    }
                )
            for name, value in mutation.add_headers:
                ops.append(
                    {"op": "add", "name": name, "value": self._mask(name, value), "phase": phase}
                )
        return ops

    def _body_diff(
        self, response: NormalizedResponse, new_body: bytes | None
    ) -> dict[str, Any] | None:
        if new_body is None:
            return None
        original = response.body or b""
        if original == new_body:
            return None

        if not _is_textual(response.content_type):
            return {
                "kind": "binary",
                "text": (
                    f"{len(original)} bytes -> {len(new_body)} bytes; "
                    f"sha256 {_digest(original)} -> {_digest(new_body)}"
                ),
                "truncated": False,
            }

        before = self._redact_text(original)
        after = self._redact_text(new_body)
        lines = difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="before",
            tofile="after",
            n=2,
        )
        text = "".join(lines)
        truncated = len(text) > self.max_diff_chars
        return {
            "kind": "unified",
            "text": text[: self.max_diff_chars],
            "truncated": truncated,
        }

    def _redact_text(self, body: bytes) -> str:
        """Redact both sides of a diff identically before comparing them.

        Redacting after diffing would leak; redacting only one side would
        manufacture differences that the module did not cause.
        """
        if self.redactor is not None and self.redactor.enabled:
            redacted, _ = self.redactor.redact_json_body(body)
            if redacted is not None:
                body = redacted
        return body.decode("utf-8", errors="replace")


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()[:16]


def _is_textual(content_type: str | None) -> bool:
    if not content_type:
        return False
    lowered = content_type.lower()
    return any(hint in lowered for hint in TEXTUAL_HINTS)


def empty_result() -> dict[str, Any]:
    return {"summary": _Tally().summary(), "results": [], "modules": {}}


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "CandidateModule",
    "DryRunRequest",
    "DryRunner",
    "empty_result",
]
