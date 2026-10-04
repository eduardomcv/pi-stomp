from modalapi.board_sync import SyncState
from modalapi.connections import Connection, Endpoint, EndpointKind
from modalapi.pedalboard import Pedalboard
from modalapi.ws_protocol import CONNECTED_MARKER
from tests.board_window import play_window, serve_board
from tests.fake_board_fetcher import FakeBoardFetcher
from tests.replay_helpers import board_to_replay_lines, load_plugin_info, load_replay
from uilib.misc import InputEvent

BUNDLE = "/path/to/new.pedalboard"


def test_a_window_swaps_the_board_and_the_old_one_is_untouched_until_resolution(v3_system):
    system = v3_system
    handler = system.handler
    handler.plugin_dict.update(load_plugin_info())
    serve_board(system, bundle=BUNDLE, snapshots={"0": "Clean", "1": "Lead"})
    old_board = handler.current.pedalboard
    fetcher = handler.board_fetcher
    assert isinstance(fetcher, FakeBoardFetcher)
    fetcher.hold = True

    for line in load_replay("connect_dump_saved.txt"):
        system.ws_bridge.inject(line)
    handler.poll_ws_messages()
    assert handler.current.pedalboard is old_board
    assert handler._board_sync.state is SyncState.RESOLVING

    fetcher.release()
    handler.poll_ws_messages()
    assert handler.current.pedalboard is not old_board
    assert handler.current.pedalboard.bundle == BUNDLE
    assert handler.current.presets == {0: "Clean", 1: "Lead"}


def test_loading_start_raises_the_flag_and_loading_end_clears_it_before_resolution(v3_system):
    system = v3_system
    handler = system.handler
    fetcher = handler.board_fetcher
    assert isinstance(fetcher, FakeBoardFetcher)
    fetcher.hold = True
    system.ws_bridge.inject("loading_start 0 0")
    handler.poll_ws_messages()
    assert handler._is_pedalboard_loading is True
    system.ws_bridge.inject("loading_end 0 Rig")
    handler.poll_ws_messages()
    assert handler._is_pedalboard_loading is False
    assert handler._board_sync.state is SyncState.RESOLVING


def test_last_json_change_never_rebuilds_plugins(parallel_beths_system):
    system = parallel_beths_system
    handler = system.handler
    fetcher = handler.board_fetcher
    assert isinstance(fetcher, FakeBoardFetcher)
    serve_board(system, bundle=handler.current.pedalboard.bundle)
    plugins = list(handler.current.pedalboard.plugins)
    handler.last_json_monitor.check_for_change = lambda: True
    handler.poll_modui_changes()
    handler.poll_modui_changes()
    assert fetcher.board_jobs
    assert len(handler.current.pedalboard.plugins) == len(plugins)
    assert all(a is b for a, b in zip(handler.current.pedalboard.plugins, plugins))


def _two_plugin_board(system, make_plugin):
    handler = system.handler
    board = handler.current.pedalboard
    drive = make_plugin("drive", uri="http://uri")
    delay = make_plugin("delay", category="Delay", uri="http://uri")
    delay.canvas_x = 100.0
    board.plugins = [drive, delay]
    board.connections = [
        Connection(
            src=Endpoint(kind=EndpointKind.PLUGIN, id="drive", port_symbol="", port_idx=0),
            dst=Endpoint(kind=EndpointKind.PLUGIN, id="delay", port_symbol="", port_idx=0),
        )
    ]
    handler.lcd.link_data(handler.pedalboard_list, handler.current, system.hw.footswitches)
    handler.lcd.draw_main_panel()
    serve_board(system, bundle=board.bundle, snapshots={"0": "Clean", "1": "Lead"})
    return board


def test_a_same_structure_replay_keeps_the_main_panel_selection(v3_system, make_plugin):
    board = _two_plugin_board(v3_system, make_plugin)
    lcd = v3_system.handler.lcd
    tile = lcd.w_plugins[1]
    lcd.main_panel.sel_widget(tile)
    plugins = list(board.plugins)

    play_window(v3_system, [CONNECTED_MARKER, *board_to_replay_lines(board, False, False, 0)])

    assert v3_system.handler.current.pedalboard.plugins == plugins
    assert lcd.main_panel.sel_ref is tile


def test_a_same_structure_replay_keeps_the_open_panel(v3_system, make_plugin):
    board = _two_plugin_board(v3_system, make_plugin)
    handler = v3_system.handler
    lcd = handler.lcd
    lcd.main_panel.sel_widget(lcd.w_plugins[0])
    lcd.main_panel.input_event(InputEvent.LONG_CLICK)
    handler.poll_lcd_updates()
    panel = lcd.pstack.current
    assert panel is not lcd.main_panel

    play_window(v3_system, [CONNECTED_MARKER, *board_to_replay_lines(board, False, False, 0)])

    assert lcd.pstack.current is panel


def test_a_window_without_loading_end_is_abandoned_by_the_tick_watchdog(v3_system):
    handler = v3_system.handler
    now = [0.0]
    handler._board_sync._clock = lambda: now[0]
    v3_system.ws_bridge.inject("loading_start 0 0")
    handler.poll_ws_messages()
    assert handler._is_pedalboard_loading is True

    now[0] = 31.0
    handler.poll_ws_messages()

    assert handler._board_sync.state is SyncState.IDLE
    assert handler._is_pedalboard_loading is False


def test_a_raising_board_sync_step_is_logged_and_the_loop_carries_on(v3_system, make_plugin, monkeypatch, caplog):
    handler = v3_system.handler
    plugin = make_plugin("fuzz", bypassed=False)
    handler.current.pedalboard.plugins = [plugin]

    def boom(*_args):
        raise RuntimeError("boom")

    monkeypatch.setattr(handler._board_sync, "poll", boom)
    monkeypatch.setattr(handler, "install_board", boom)
    serve_board(v3_system, bundle="/path/to/new.pedalboard")
    play_window(v3_system, ["loading_start 0 0", "loading_end 0 New Rig"])

    assert caplog.text.count("board sync step failed") >= 2
    assert handler._board_sync.state is SyncState.IDLE
    v3_system.ws_bridge.inject("param_set /graph/fuzz :bypass 1.0")
    handler.poll_ws_messages()
    assert plugin.is_bypassed()


def test_a_board_that_fails_to_build_is_logged_once_and_live_echoes_still_land(
    v3_system, make_plugin, monkeypatch, caplog
):
    handler = v3_system.handler
    plugin = make_plugin("fuzz", bypassed=False)
    handler.current.pedalboard.plugins = [plugin]
    now = [0.0]
    handler._board_sync._clock = lambda: now[0]

    def broken(*_args, **_kwargs):
        raise RuntimeError("from_spec failed")

    monkeypatch.setattr(Pedalboard, "from_spec", broken)
    serve_board(v3_system, bundle="/path/to/new.pedalboard")
    play_window(v3_system, ["loading_start 0 0", "loading_end 0 New Rig"])
    now[0] = 61.0
    handler.poll_ws_messages()
    handler.poll_ws_messages()

    assert caplog.text.count("board sync step failed") == 1
    v3_system.ws_bridge.inject("param_set /graph/fuzz :bypass 1.0")
    handler.poll_ws_messages()
    assert plugin.is_bypassed()


def _same_blend_board_window(system, snapshot_id: int) -> list[str]:
    board = system.handler.current.pedalboard
    serve_board(system, bundle=board.bundle, snapshots={"0": "Clean", "1": "Lead", "2": "Blend"})
    return [CONNECTED_MARKER, *board_to_replay_lines(board, False, False, snapshot_id)]


def _with_uris(system) -> None:
    for plugin in system.handler.current.pedalboard.plugins:
        plugin.uri = f"urn:pistomp:test:{plugin.instance_id}"


def test_a_reconciled_snapshot_index_leaves_the_blend_mode(blend_system):
    handler = blend_system.handler
    _with_uris(blend_system)
    plugins = list(handler.current.pedalboard.plugins)
    assert handler.active_blend_mode is not None
    assert handler.current.preset_index == 2

    play_window(blend_system, _same_blend_board_window(blend_system, 0))

    assert handler.current.pedalboard.plugins == plugins
    assert handler.current.preset_index == 0
    assert handler.active_blend_mode is None


def test_a_reconciled_unchanged_index_keeps_the_blend_mode(blend_system, monkeypatch):
    handler = blend_system.handler
    _with_uris(blend_system)
    active = handler.active_blend_mode
    assert active is not None
    toggles = []
    monkeypatch.setattr(active, "deactivate", lambda: toggles.append("deactivate"))
    monkeypatch.setattr(active, "activate", lambda: toggles.append("activate"))

    play_window(blend_system, _same_blend_board_window(blend_system, 2))

    assert handler.current.preset_index == 2
    assert handler.active_blend_mode is active
    assert toggles == []
