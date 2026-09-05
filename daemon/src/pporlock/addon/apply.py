"""pporlock -> mitmproxy application — SPEC-1 §3.3.

The other half of the adapter boundary. Nothing outside this module touches a
mitmproxy mutation API.

Body assignment goes through ``.text``/``.content``, which handles gzip,
deflate, and brotli decode and re-encode according to Content-Encoding
(REQ PXY-023). Doing it by hand anywhere else would mean re-implementing that,
badly, in three places.
"""

from __future__ import annotations

from typing import Any

from ..engine.models import (
    RedirectSpec,
    RequestMutation,
    ResponseMutation,
    SyntheticResponse,
)


def _apply_header_ops(headers: Any, mutation: Any) -> bool:
    """Apply the mutation's header operations to a mitmproxy Headers object.

    In declaration order, which is the whole point. This used to apply every
    remove, then every set, then every add, on the reasoning that ordering
    between *categories* did not matter. It does: rule 1 adding ``X-Review`` and
    rule 2 removing it must leave the header gone, and the reverse declaration
    order must leave it present. Regrouping by operation type made both orders
    produce the same wire result while provenance faithfully reported two rules
    applied in the order written — the exact class of silent disagreement this
    system exists to prevent (SEP_5_REVIEW F-02, REQ MOD-012, PXY-036).

    ``changed`` reflects the header collection, not the declaration: removing an
    absent header or setting one to the value it already holds is not a change.
    """
    changed = False

    for op, name, value in _ops_of(mutation):
        if op == "remove":
            if name in headers:
                del headers[name]
                changed = True
        elif op == "set":
            if headers.get(name) != value:
                headers[name] = value
                changed = True
        else:
            headers.add(name, value)
            changed = True

    return changed


def _ops_of(mutation: Any) -> list[tuple[str, str, str]]:
    """The mutation's ordered operations.

    A mutation built by the engine or through ``set``/``add``/``remove`` carries
    ``ops``. A module is trusted code and may hand back any object with the
    documented attributes, so a source without ``ops`` is still honoured — in
    the old fixed order, which is the only order it expresses.
    """
    ops = getattr(mutation, "ops", None)
    if ops is not None:
        return [(op, name, value) for op, name, value in ops]
    return [
        *(("remove", name, "") for name in getattr(mutation, "remove_headers", ())),
        *(("set", name, value) for name, value in getattr(mutation, "set_headers", {}).items()),
        *(("add", name, value) for name, value in getattr(mutation, "add_headers", ())),
    ]


def apply_redirect(request: Any, spec: RedirectSpec) -> bool:
    """Rewrite URL components in place (REQ PXY-035)."""
    if spec.is_empty():
        return False

    if spec.scheme is not None:
        request.scheme = spec.scheme
    if spec.host is not None:
        request.host = spec.host
    if spec.port is not None:
        request.port = spec.port
    if spec.path is not None:
        request.path = spec.path
    if spec.query is not None:
        base = request.path.split("?", 1)[0]
        request.path = f"{base}?{spec.query}" if spec.query else base
    return True


def build_response(synthetic: SyntheticResponse) -> Any:
    """Turn a SyntheticResponse into a mitmproxy Response.

    Imported lazily so that importing this module does not require mitmproxy —
    which keeps the adapter unit-testable with a stub flow.
    """
    from mitmproxy import http

    response = http.Response.make(
        synthetic.status,
        synthetic.body,
        dict(synthetic.headers),
    )
    return response


def apply_synthetic(flow: Any, synthetic: SyntheticResponse) -> None:
    """Short-circuit a flow with a manufactured response.

    The request never leaves the machine. Attribution travels with it: the
    origin rule is recorded so a synthesized response is never mistaken for
    something the network produced.
    """
    flow.response = build_response(synthetic)


def apply_request_mutation(flow: Any, mutation: RequestMutation) -> bool:
    """Apply an accumulated request mutation. Returns whether anything changed."""
    if mutation.is_empty():
        return False

    changed = False

    if mutation.redirect is not None:
        changed |= apply_redirect(flow.request, mutation.redirect)

    changed |= _apply_header_ops(flow.request.headers, mutation)

    if mutation.body is not None:
        flow.request.content = mutation.body
        changed = True

    if mutation.short_circuit is not None:
        apply_synthetic(flow, mutation.short_circuit)
        changed = True

    return changed


def apply_response_mutation(flow: Any, mutation: ResponseMutation) -> bool:
    """Apply an accumulated response mutation. Returns whether anything changed."""
    if mutation.is_empty() or flow.response is None:
        return False

    changed = False

    if mutation.status is not None and flow.response.status_code != mutation.status:
        flow.response.status_code = mutation.status
        changed = True

    changed |= _apply_header_ops(flow.response.headers, mutation)

    if mutation.body is not None:
        # Assigning .content re-encodes per Content-Encoding (REQ PXY-023).
        flow.response.content = mutation.body
        changed = True

    return changed


def set_stream(flow: Any, stream: bool) -> None:
    """Set the streaming flag on a response.

    Only meaningful from the ``responseheaders`` hook — after that the decision
    has already been made for us (REQ PXY-021).
    """
    if flow.response is not None:
        flow.response.stream = stream
