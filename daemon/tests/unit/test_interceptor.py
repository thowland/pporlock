"""The addon. SPEC-1 §3.1."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from pporlock.addon.interceptor import Counters, Interceptor, NullSink
from pporlock.config import Config
from pporlock.engine.exclusions import ExclusionEntry, ExclusionList
from pporlock.engine.provenance import NoteCode
from tests.stubs import StubFlow, StubHeaders, StubRequest, StubResponse


class RecordingSink(NullSink):
    def __init__(self) -> None:
        super().__init__()
        self.http_records: list[tuple[Any, Any, Any, Any]] = []
        self.passthrough_records: list[tuple[Any, Any, Any, Any]] = []
        self.ws_records: list[Any] = []

    def record_http(self, request: Any, response: Any, provenance: Any, timing: Any) -> None:
        super().record_http(request, response, provenance, timing)
        self.http_records.append((request, response, provenance, timing))

    def record_passthrough(self, host: Any, ip: Any, provenance: Any, timing: Any) -> None:
        super().record_passthrough(host, ip, provenance, timing)
        self.passthrough_records.append((host, ip, provenance, timing))

    def record_websocket_message(self, message: Any) -> None:
        super().record_websocket_message(message)
        self.ws_records.append(message)


def client_hello(sni: str | None, ip: str | None = None) -> Any:
    class Data:
        ignore_connection = False

    data = Data()
    data.client_hello = type("C", (), {"sni": sni})()
    server = type("S", (), {"address": (ip, 443) if ip else None})()
    data.context = type("Ctx", (), {"server": server})()
    return data


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def interceptor(sink: RecordingSink) -> Interceptor:
    exclusions = ExclusionList([ExclusionEntry("*.apple.com", "update: OS updates", "default")])
    return Interceptor(Config(), sink=sink, exclusions=exclusions)


class TestCounters:
    def test_starts_at_zero(self) -> None:
        assert Counters().to_dict() == {
            "flows_total": 0,
            "blocked": 0,
            "modified": 0,
            "passthrough": 0,
            "errors": 0,
        }


class TestExclusionHook:
    def test_excluded_connection_is_tunneled(self, interceptor: Interceptor) -> None:
        """REQ PXY-013 — ignore_connection means we never see the bytes."""
        data = client_hello("swscan.apple.com")
        interceptor.tls_clienthello(data)
        assert data.ignore_connection is True

    def test_non_excluded_connection_is_decrypted(self, interceptor: Interceptor) -> None:
        data = client_hello("example.com")
        interceptor.tls_clienthello(data)
        assert data.ignore_connection is False

    def test_exclusion_is_recorded_as_a_passthrough(
        self, interceptor: Interceptor, sink: RecordingSink
    ) -> None:
        """REQ PXY-015. Silence would make excluded traffic invisible, which is a
        different failure from the one exclusion is solving."""
        interceptor.tls_clienthello(client_hello("swscan.apple.com"))
        assert len(sink.passthrough_records) == 1
        host, _ip, provenance, _timing = sink.passthrough_records[0]
        assert host == "swscan.apple.com"
        assert provenance.has_note(NoteCode.PASSTHROUGH_EXCLUDED)

    def test_passthrough_note_carries_the_reason(
        self, interceptor: Interceptor, sink: RecordingSink
    ) -> None:
        interceptor.tls_clienthello(client_hello("swscan.apple.com"))
        note = sink.passthrough_records[0][2].notes[0]
        assert note.detail["pattern"] == "*.apple.com"
        assert "OS updates" in note.detail["reason"]

    def test_passthrough_increments_the_counter(self, interceptor: Interceptor) -> None:
        interceptor.tls_clienthello(client_hello("swscan.apple.com"))
        assert interceptor.counters.passthrough == 1

    def test_non_excluded_records_nothing(
        self, interceptor: Interceptor, sink: RecordingSink
    ) -> None:
        interceptor.tls_clienthello(client_hello("example.com"))
        assert sink.passthrough_records == []
        assert interceptor.counters.passthrough == 0

    def test_no_sni_falls_back_to_ip(self, sink: RecordingSink) -> None:
        exclusions = ExclusionList([ExclusionEntry("10.0.0.0/8", "private", "user")])
        interceptor = Interceptor(Config(), sink=sink, exclusions=exclusions)
        data = client_hello(None, "10.1.2.3")
        interceptor.tls_clienthello(data)
        assert data.ignore_connection is True


class TestRequestResponseCycle:
    def test_request_establishes_provenance_and_counts(self, interceptor: Interceptor) -> None:
        flow = StubFlow()
        interceptor.request(flow)
        assert interceptor.counters.flows_total == 1
        assert "pporlock.builder" in flow.metadata
        assert "pporlock.request" in flow.metadata

    async def test_response_records_the_flow(
        self, interceptor: Interceptor, sink: RecordingSink
    ) -> None:
        flow = StubFlow(response=StubResponse())
        interceptor.request(flow)
        await interceptor.response(flow)
        assert len(sink.http_records) == 1
        request, response, provenance, timing = sink.http_records[0]
        assert request.method == "GET"
        assert response.status == 200
        assert provenance.profile == "default"
        assert timing["pporlock_ms"] >= 0

    async def test_every_flow_carries_provenance(
        self, interceptor: Interceptor, sink: RecordingSink
    ) -> None:
        """REQ CAP-013 — including a flow that matched nothing at all."""
        flow = StubFlow(response=StubResponse())
        interceptor.request(flow)
        await interceptor.response(flow)
        assert sink.http_records[0][2] is not None

    async def test_response_without_a_prior_request_still_records(
        self, interceptor: Interceptor, sink: RecordingSink
    ) -> None:
        """A hook can fire without its partner — a replayed flow, or a restart
        mid-connection. Recording a partial flow beats dropping it."""
        await interceptor.response(StubFlow(response=StubResponse()))
        assert len(sink.http_records) == 1

    async def test_the_buffering_guard_streams_a_body_no_rule_wants(
        self, interceptor: Interceptor, sink: RecordingSink
    ) -> None:
        """REQ PXY-021/022 — and it says so, rather than doing nothing quietly."""
        flow = StubFlow(response=StubResponse())
        interceptor.request(flow)
        interceptor.responseheaders(flow)
        assert flow.response.stream is True
        await interceptor.response(flow)
        provenance = sink.http_records[0][2]
        assert provenance.has_note(NoteCode.RESPONSE_STREAMED)

    def test_responseheaders_is_safe_without_a_prior_request(
        self, interceptor: Interceptor
    ) -> None:
        """A hook can fire without its partner — a replayed flow, or a restart
        mid-connection."""
        interceptor.responseheaders(StubFlow(response=StubResponse()))

    def test_responseheaders_without_a_response_is_safe(self, interceptor: Interceptor) -> None:
        interceptor.responseheaders(StubFlow())

    async def test_streamed_response_carries_no_body(
        self, interceptor: Interceptor, sink: RecordingSink
    ) -> None:
        flow = StubFlow(response=StubResponse(stream=True, content=b"never buffered"))
        interceptor.request(flow)
        await interceptor.response(flow)
        assert sink.http_records[0][1].body is None

    async def test_metadata_is_cleaned_up(self, interceptor: Interceptor) -> None:
        """Per-flow state must not accumulate on long-lived flows."""
        flow = StubFlow(response=StubResponse())
        interceptor.request(flow)
        await interceptor.response(flow)
        assert not [k for k in flow.metadata if k.startswith("pporlock.")]

    def test_error_hook_counts(self, interceptor: Interceptor) -> None:
        interceptor.error(StubFlow())
        assert interceptor.counters.errors == 1

    async def test_sec_fetch_dest_survives_the_round_trip(
        self, interceptor: Interceptor, sink: RecordingSink
    ) -> None:
        request = StubRequest(headers=StubHeaders([(b"Sec-Fetch-Dest", b"script")]))
        flow = StubFlow(request, StubResponse())
        interceptor.request(flow)
        await interceptor.response(flow)
        assert sink.http_records[0][0].dest == "script"


class TestWebSockets:
    def test_message_is_captured(self, interceptor: Interceptor, sink: RecordingSink) -> None:
        flow = StubFlow()
        message = type(
            "M", (), {"content": b"hello", "from_client": True, "is_text": True, "timestamp": 1.0}
        )()
        flow.websocket = type("W", (), {"messages": [message]})()
        interceptor.websocket_message(flow)
        assert len(sink.ws_records) == 1
        assert sink.ws_records[0].direction == "outbound"
        assert sink.ws_records[0].opcode == "text"

    def test_indexes_increment_per_flow(
        self, interceptor: Interceptor, sink: RecordingSink
    ) -> None:
        flow = StubFlow()
        message = type(
            "M", (), {"content": b"x", "from_client": False, "is_text": False, "timestamp": 1.0}
        )()
        flow.websocket = type("W", (), {"messages": [message]})()
        interceptor.websocket_message(flow)
        interceptor.websocket_message(flow)
        assert [m.index for m in sink.ws_records] == [0, 1]

    def test_index_state_is_released_on_close(self, interceptor: Interceptor) -> None:
        flow = StubFlow()
        message = type(
            "M", (), {"content": b"x", "from_client": True, "is_text": True, "timestamp": 1.0}
        )()
        flow.websocket = type("W", (), {"messages": [message]})()
        interceptor.websocket_message(flow)
        interceptor.websocket_end(flow)
        assert flow.id not in interceptor._ws_indexes

    def test_flow_without_websocket_is_ignored(self, interceptor: Interceptor) -> None:
        flow = StubFlow()
        flow.websocket = None
        interceptor.websocket_message(flow)

    def test_flow_with_no_messages_is_ignored(self, interceptor: Interceptor) -> None:
        flow = StubFlow()
        flow.websocket = type("W", (), {"messages": []})()
        interceptor.websocket_message(flow)


class TestLifecycle:
    def test_hooks_are_safe_to_call(self, interceptor: Interceptor) -> None:
        interceptor.running()
        interceptor.responseheaders(StubFlow(response=StubResponse()))
        interceptor.done()

    def test_uptime_advances(self, interceptor: Interceptor) -> None:
        assert interceptor.uptime_s >= 0

    def test_defaults_load_the_shipped_exclusions(self) -> None:
        assert len(Interceptor().exclusions) > 0

    def test_null_sink_counts(self) -> None:
        sink = NullSink()
        sink.record_http(None, None, None, {})
        sink.record_passthrough(None, None, None, {})
        sink.record_websocket_message(None)
        assert (sink.http, sink.passthrough, sink.websocket_messages) == (1, 1, 1)


class TestProxyListenerControl:
    """OI-3 — start/stop must report what actually happened, not what was asked.

    The listener really moving is covered by
    ``tests/integration/test_proxy_control.py`` against a real DumpMaster; what
    is covered here is the failure path, which is the half that used to lie.
    """

    def test_reports_listening_when_there_is_no_master_to_ask(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pporlock.addon import interceptor as module

        monkeypatch.setattr(module.Interceptor, "_proxyserver", lambda self: None)
        assert Interceptor(Config()).proxy_listening is True

    async def test_refuses_when_no_master_manages_the_listener(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from pporlock.addon import interceptor as module
        from pporlock.errors import ProxyControlError

        monkeypatch.setattr(module.Interceptor, "_proxyserver", lambda self: None)
        with pytest.raises(ProxyControlError):
            await Interceptor(Config()).set_proxy_running(False)

    async def test_reports_a_listener_that_never_reaches_the_asked_for_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A timeout is a failure, not a 200. This is the exact shape of OI-3."""
        from pporlock.addon import interceptor as module
        from pporlock.errors import ProxyControlError

        class StuckAddon:
            def listen_addrs(self) -> list[str]:
                return ["127.0.0.1:8080"]

        class Options:
            def update(self, **_: object) -> None:
                return None

        class Master:
            options = Options()

        interceptor = Interceptor(Config())
        monkeypatch.setattr(module.Interceptor, "_proxyserver", lambda self: Master())
        monkeypatch.setattr(module, "_proxyserver_addon", lambda master: StuckAddon())
        monkeypatch.setattr(module, "PROXY_STATE_POLLS", 2)
        monkeypatch.setattr(module, "PROXY_STATE_POLL_INTERVAL_S", 0.001)

        with pytest.raises(ProxyControlError, match="did not stop"):
            await interceptor.set_proxy_running(False)

    async def test_it_waits_for_the_listener_addon_to_accept_changes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OI-32 — the bug that made this a permanent failure, not a slow one.

        mitmproxy's `Master.run` binds the listener in `setup_servers()` and only
        *then* triggers the `running` hook, which is what sets
        `Proxyserver.is_running`. `Proxyserver.configure` drops an option change
        outright while that flag is False — no queue, no retry. So a stop
        commanded in that window vanished, and because the port was already
        accepting there was nothing outside mitmproxy that could see the
        difference: the listener stayed up for ever and we reported it as a
        1.0s timeout, which sent everyone looking at the budget.

        This fake reproduces exactly that contract.
        """
        from pporlock.addon import interceptor as module

        class LateAddon:
            """Bound and accepting, but not yet listening for option changes."""

            def __init__(self) -> None:
                self.addrs = ["127.0.0.1:8080"]
                self.polls = 0

            @property
            def is_running(self) -> bool:
                # The `running` hook fires a little after the bind; reading the
                # flag is what advances the clock here.
                self.polls += 1
                return self.polls >= 3

            def listen_addrs(self) -> list[str]:
                return self.addrs

        addon = LateAddon()

        class Options:
            def update(self, **kwargs: object) -> None:
                # mitmproxy's own behaviour: ignored unless the addon is running.
                if addon.is_running and kwargs.get("server") is False:
                    addon.addrs = []

        class Master:
            options = Options()

        interceptor = Interceptor(Config())
        monkeypatch.setattr(module.Interceptor, "_proxyserver", lambda self: Master())
        monkeypatch.setattr(module, "_proxyserver_addon", lambda master: addon)
        monkeypatch.setattr(module, "PROXY_STATE_POLL_INTERVAL_S", 0.001)

        assert await interceptor.set_proxy_running(False) is True
        assert addon.addrs == [], "the command was issued into the window and lost"

    async def test_it_refuses_rather_than_commanding_a_listener_that_never_readies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """And says which of the two failures it is.

        "Not delivered" and "not honoured" want different things done about
        them, so they do not share a message.
        """
        from pporlock.addon import interceptor as module
        from pporlock.errors import ProxyControlError

        class NeverReadyAddon:
            is_running = False

            def listen_addrs(self) -> list[str]:
                return ["127.0.0.1:8080"]

        class Options:
            def update(self, **_: object) -> None:
                raise AssertionError("must not command an addon that cannot hear it")

        class Master:
            options = Options()

        interceptor = Interceptor(Config())
        monkeypatch.setattr(module.Interceptor, "_proxyserver", lambda self: Master())
        monkeypatch.setattr(module, "_proxyserver_addon", lambda master: NeverReadyAddon())
        monkeypatch.setattr(module, "PROXY_READY_POLLS", 2)
        monkeypatch.setattr(module, "PROXY_STATE_POLL_INTERVAL_S", 0.001)

        with pytest.raises(ProxyControlError, match="not yet accepting"):
            await interceptor.set_proxy_running(False)

    async def test_a_version_without_the_flag_is_not_blocked_by_the_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SPEC-1 §2.1 — an attribute that disappears costs us the guard, not
        the feature. This is the adapter; absorbing that churn is its job."""
        from pporlock.addon import interceptor as module

        class OlderAddon:
            def __init__(self) -> None:
                self.addrs: list[str] = ["127.0.0.1:8080"]

            def listen_addrs(self) -> list[str]:
                return self.addrs

        addon = OlderAddon()

        class Options:
            def update(self, **kwargs: object) -> None:
                if kwargs.get("server") is False:
                    addon.addrs = []

        class Master:
            options = Options()

        interceptor = Interceptor(Config())
        monkeypatch.setattr(module.Interceptor, "_proxyserver", lambda self: Master())
        monkeypatch.setattr(module, "_proxyserver_addon", lambda master: addon)
        monkeypatch.setattr(module, "PROXY_STATE_POLL_INTERVAL_S", 0.001)

        assert await interceptor.set_proxy_running(False) is True

    def test_a_listen_addrs_property_is_read_as_well_as_a_method(self) -> None:
        """mitmproxy 12 exposes it as a method; other releases as a property.
        Absorbing that here is what the adapter is for (SPEC-1 §2.1)."""
        from pporlock.addon.interceptor import _has_listeners

        class AsProperty:
            listen_addrs: ClassVar[list[str]] = ["127.0.0.1:8080"]

        class AsMethod:
            def listen_addrs(self) -> list[str]:
                return []

        assert _has_listeners(AsProperty()) is True
        assert _has_listeners(AsMethod()) is False

    def test_no_addon_means_no_control(self) -> None:
        from pporlock.addon.interceptor import _proxyserver_addon

        assert _proxyserver_addon(None) is None
        assert _proxyserver_addon(object()) is None
