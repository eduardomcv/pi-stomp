from unittest.mock import patch

import pytest

from common.parameter import BYPASS_SYMBOL, Symbol
from tests.fake_board_fetcher import FakeBoardFetcher
from tests.types import SystemFixture
from tests.v3.test_dynamic_pedalboard import (
    _EXTRA_CHORUS_INFO,
    _EXTRA_CHORUS_URI,
    _EXTRA_VERB_INFO,
    _EXTRA_VERB_URI,
    _effect_get_side_effect,
)

CHORUS_ADD = f"add /graph/ExtraChorus {_EXTRA_CHORUS_URI} 900.0 50.0 0 1 1"


def _serve(system: SystemFixture) -> None:
    system.mock_get.side_effect = _effect_get_side_effect(
        {_EXTRA_CHORUS_URI: _EXTRA_CHORUS_INFO, _EXTRA_VERB_URI: _EXTRA_VERB_INFO}
    )


def _fetcher(system: SystemFixture) -> FakeBoardFetcher:
    fetcher = system.handler.board_fetcher
    assert isinstance(fetcher, FakeBoardFetcher)
    return fetcher


def _ids(system: SystemFixture) -> list[str]:
    return [p.instance_id for p in system.handler.current.pedalboard.plugins]


def test_add_waits_for_metadata_without_blocking_the_tick(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    _serve(system)
    fetcher = _fetcher(system)
    fetcher.hold = True

    system.ws_bridge.inject(CHORUS_ADD)
    system.handler.poll_ws_messages()

    assert "ExtraChorus" not in _ids(system)
    assert fetcher.requests == [(_EXTRA_CHORUS_URI,)]
    assert not any("effect/get" in c.args[0] for c in system.mock_get.call_args_list)

    fetcher.release()
    system.handler.poll_ws_messages()
    assert "ExtraChorus" in _ids(system)


def test_the_handler_issues_no_rest_call_of_its_own_on_the_live_path(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    _serve(system)
    fetcher = _fetcher(system)
    fetcher.hold = True

    with patch("pistomp.httpclient.get", side_effect=system.mock_get.side_effect) as spy:
        system.ws_bridge.inject(CHORUS_ADD)
        system.handler.poll_ws_messages()

    assert not any("effect/get" in str(c.args[0]) for c in spy.call_args_list)
    assert "ExtraChorus" not in _ids(system)


def test_messages_for_a_pending_plugin_are_replayed_once_it_exists(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    _serve(system)
    fetcher = _fetcher(system)
    anchor = _ids(system)[-1]
    fetcher.hold = True

    for line in (
        f"add /graph/ExtraChorus {_EXTRA_CHORUS_URI} 900.0 50.0 1 1 1",
        "param_set /graph/ExtraChorus rate 3.500000",
        "param_set /graph/ExtraChorus :bypass 0.000000",
        "midi_map /graph/ExtraChorus rate 13 60 0.0 5.0",
        f"connect /graph/ExtraChorus/out_L /graph/{anchor}/in_L",
    ):
        system.ws_bridge.inject(line)
    system.handler.poll_ws_messages()
    fetcher.release()
    system.handler.poll_ws_messages()

    added = next(p for p in system.handler.current.pedalboard.plugins if p.instance_id == "ExtraChorus")
    assert added.parameters[Symbol("rate")].value == pytest.approx(3.5)
    assert not added.is_bypassed()
    assert added.parameters[Symbol("rate")].binding == "13:60"
    assert any(c.src.id == "ExtraChorus" for c in system.handler.current.pedalboard.connections)


def test_remove_while_pending_cancels_the_add(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    _serve(system)
    fetcher = _fetcher(system)
    fetcher.hold = True

    system.ws_bridge.inject(CHORUS_ADD)
    system.ws_bridge.inject("param_set /graph/ExtraChorus rate 3.500000")
    system.ws_bridge.inject("remove /graph/ExtraChorus")
    system.handler.poll_ws_messages()
    fetcher.release()
    system.handler.poll_ws_messages()

    assert "ExtraChorus" not in _ids(system)
    assert not system.handler._pending_adds


def test_board_reload_while_pending_does_not_resurrect_the_plugin(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    _serve(system)
    fetcher = _fetcher(system)
    fetcher.hold = True

    system.ws_bridge.inject(CHORUS_ADD)
    system.ws_bridge.inject("loading_start 0 0")
    system.ws_bridge.inject("loading_end 0 Other")
    system.handler.poll_ws_messages()
    fetcher.release()
    system.handler.poll_ws_messages()

    assert "ExtraChorus" not in _ids(system)
    assert not system.handler._pending_adds


def test_two_adds_of_one_uri_fetch_once_and_both_appear(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    _serve(system)
    fetcher = _fetcher(system)
    fetcher.hold = True

    system.ws_bridge.inject(CHORUS_ADD)
    system.ws_bridge.inject(f"add /graph/ExtraChorus2 {_EXTRA_CHORUS_URI} 950.0 50.0 0 1 1")
    system.handler.poll_ws_messages()
    assert fetcher.requests == [(_EXTRA_CHORUS_URI,)]

    fetcher.release()
    system.handler.poll_ws_messages()
    assert {"ExtraChorus", "ExtraChorus2"} <= set(_ids(system))


def test_connect_between_two_pending_plugins_appears_once(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    _serve(system)
    fetcher = _fetcher(system)
    fetcher.hold = True

    system.ws_bridge.inject(CHORUS_ADD)
    system.ws_bridge.inject(f"add /graph/ExtraVerb {_EXTRA_VERB_URI} 950.0 50.0 0 1 1")
    system.ws_bridge.inject("connect /graph/ExtraChorus/out_L /graph/ExtraVerb/in_L")
    system.handler.poll_ws_messages()
    fetcher.release()
    system.handler.poll_ws_messages()

    arcs = [
        c
        for c in system.handler.current.pedalboard.connections
        if c.src.id == "ExtraChorus" and c.dst.id == "ExtraVerb"
    ]
    assert len(arcs) == 1


def test_unavailable_metadata_adds_a_bypass_only_tile(parallel_beths_system: SystemFixture):
    system = parallel_beths_system

    system.ws_bridge.inject("add /graph/Unknown http://not.registered/plugin 900.0 50.0 0 1 1")
    system.ws_bridge.inject("param_set /graph/Unknown rate 3.500000")
    system.handler.poll_ws_messages()

    added = next(p for p in system.handler.current.pedalboard.plugins if p.instance_id == "Unknown")
    assert list(added.parameters) == [BYPASS_SYMBOL]


def test_a_cached_uri_is_added_synchronously(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    _serve(system)
    fetcher = _fetcher(system)

    system.ws_bridge.inject(CHORUS_ADD)
    system.handler.poll_ws_messages()
    system.ws_bridge.inject(f"add /graph/ExtraChorus2 {_EXTRA_CHORUS_URI} 950.0 50.0 0 1 1")
    fetcher.hold = True
    system.handler.poll_ws_messages()

    assert "ExtraChorus2" in _ids(system)
    assert fetcher.requests == [(_EXTRA_CHORUS_URI,)]


def test_handler_cleanup_closes_the_fetcher(parallel_beths_system: SystemFixture):
    fetcher = _fetcher(parallel_beths_system)
    parallel_beths_system.handler.cleanup()
    assert fetcher.closed


def test_one_failed_build_does_not_lose_the_other_resolved_plugin(parallel_beths_system: SystemFixture, monkeypatch):
    system = parallel_beths_system
    _serve(system)
    fetcher = _fetcher(system)
    fetcher.hold = True
    handler = system.handler
    insert = handler._insert_plugin

    def flaky(msg, info):
        if msg.instance == "ExtraChorus":
            raise RuntimeError("customizer blew up")
        insert(msg, info)

    monkeypatch.setattr(handler, "_insert_plugin", flaky)

    system.ws_bridge.inject(CHORUS_ADD)
    system.ws_bridge.inject(f"add /graph/ExtraChorus2 {_EXTRA_CHORUS_URI} 950.0 50.0 0 1 1")
    system.ws_bridge.inject("param_set /graph/ExtraChorus2 rate 3.500000")
    handler.poll_ws_messages()
    fetcher.release()
    handler.poll_ws_messages()

    assert "ExtraChorus" not in _ids(system)
    added = next(p for p in handler.current.pedalboard.plugins if p.instance_id == "ExtraChorus2")
    assert added.parameters[Symbol("rate")].value == pytest.approx(3.5)


def test_a_failed_redraw_does_not_escape_the_tick(parallel_beths_system: SystemFixture, monkeypatch):
    system = parallel_beths_system
    _serve(system)
    fetcher = _fetcher(system)
    fetcher.hold = True
    handler = system.handler

    def broken_draw():
        raise RuntimeError("lcd gone")

    monkeypatch.setattr(handler.lcd, "draw_main_panel", broken_draw)

    system.ws_bridge.inject(CHORUS_ADD)
    system.ws_bridge.inject("param_set /graph/ExtraChorus rate 3.500000")
    handler.poll_ws_messages()
    fetcher.release()
    handler.poll_ws_messages()

    assert not handler._pending_adds
    added = next(p for p in handler.current.pedalboard.plugins if p.instance_id == "ExtraChorus")
    assert added.parameters[Symbol("rate")].value == pytest.approx(3.5)
