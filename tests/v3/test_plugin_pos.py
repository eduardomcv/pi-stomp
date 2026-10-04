from tests.fake_board_fetcher import FakeBoardFetcher
from tests.types import SystemFixture
from tests.v3.test_dynamic_pedalboard import _EXTRA_CHORUS_INFO, _EXTRA_CHORUS_URI, _effect_get_side_effect


def _ids(system: SystemFixture) -> list[str]:
    return [p.instance_id for p in system.handler.current.pedalboard.plugins]


def test_plugin_pos_moves_and_resorts_a_plugin(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    board = system.handler.current.pedalboard
    first = board.plugins[0].instance_id
    last_x = board.plugins[-1].canvas_x

    system.ws_bridge.inject(f"plugin_pos /graph/{first} {int(last_x) + 100} 5")
    system.handler.poll_ws_messages()

    moved = board.find_plugin(first)
    assert moved is not None
    assert (moved.canvas_x, moved.canvas_y) == (last_x + 100, 5.0)
    assert board.plugins[-1].instance_id == first
    xs = [p.canvas_x for p in board.plugins]
    assert xs == sorted(xs)


def test_plugin_pos_that_changes_the_order_rebinds_and_redraws(parallel_beths_system: SystemFixture, monkeypatch):
    system = parallel_beths_system
    handler = system.handler
    first = _ids(system)[0]
    calls: list[str] = []
    monkeypatch.setattr(handler, "bind_current_pedalboard", lambda: calls.append("bind"))
    monkeypatch.setattr(handler.lcd, "draw_main_panel", lambda: calls.append("draw"))

    system.ws_bridge.inject(f"plugin_pos /graph/{first} 5000 5")
    handler.poll_ws_messages()

    assert calls == ["bind", "draw"]


def test_plugin_pos_that_keeps_the_order_updates_the_position_without_a_redraw(
    parallel_beths_system: SystemFixture, monkeypatch
):
    system = parallel_beths_system
    handler = system.handler
    before = _ids(system)
    last = handler.current.pedalboard.plugins[-1]
    target_x = last.canvas_x + 10
    calls: list[str] = []
    monkeypatch.setattr(handler, "bind_current_pedalboard", lambda: calls.append("bind"))
    monkeypatch.setattr(handler.lcd, "draw_main_panel", lambda: calls.append("draw"))

    system.ws_bridge.inject(f"plugin_pos /graph/{last.instance_id} {int(target_x)} 5")
    handler.poll_ws_messages()

    assert (last.canvas_x, last.canvas_y) == (target_x, 5.0)
    assert _ids(system) == before
    assert calls == []


def test_plugin_pos_for_an_unknown_instance_is_ignored(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    before = _ids(system)

    system.ws_bridge.inject("plugin_pos /graph/NoSuch 10 10")
    system.handler.poll_ws_messages()

    assert _ids(system) == before


def test_a_live_added_plugin_carries_the_instance_number(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    system.mock_get.side_effect = _effect_get_side_effect({_EXTRA_CHORUS_URI: _EXTRA_CHORUS_INFO})

    system.ws_bridge.inject(f"add /graph/Extra {_EXTRA_CHORUS_URI} 900.0 50.0 0 1 1 7")
    system.handler.poll_ws_messages()

    added = system.handler.current.pedalboard.find_plugin("Extra")
    assert added is not None
    assert added.instance_number == 7


def test_a_plugin_pos_for_a_pending_add_is_replayed_once_the_plugin_exists(parallel_beths_system: SystemFixture):
    system = parallel_beths_system
    system.mock_get.side_effect = _effect_get_side_effect({_EXTRA_CHORUS_URI: _EXTRA_CHORUS_INFO})
    fetcher = system.handler.board_fetcher
    assert isinstance(fetcher, FakeBoardFetcher)
    fetcher.hold = True
    first = _ids(system)[0]

    system.ws_bridge.inject(f"add /graph/Extra {_EXTRA_CHORUS_URI} 900.0 50.0 0 1 1")
    system.ws_bridge.inject("plugin_pos /graph/Extra 50 5")
    system.handler.poll_ws_messages()
    assert "Extra" not in _ids(system)

    fetcher.release()
    system.handler.poll_ws_messages()

    added = system.handler.current.pedalboard.find_plugin("Extra")
    assert added is not None
    assert (added.canvas_x, added.canvas_y) == (50.0, 5.0)
    assert _ids(system)[0] == "Extra"
    assert _ids(system)[1] == first
