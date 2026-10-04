import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import common.token as Token
from common.parameter import BYPASS_SYMBOL, Symbol
from modalapi.board_sync import UNTITLED, SyncState
from modalapi.pedalboard import Pedalboard
from modalapi.ws_protocol import CONNECTED_MARKER
from tests.board_window import play_window, serve_board
from tests.fake_board_fetcher import FakeBoardFetcher
from tests.replay_helpers import load_plugin_info, load_replay
from tests.types import SystemFixture
from tests.v3.test_dynamic_pedalboard import (
    _EXTRA_CHORUS_INFO,
    _EXTRA_CHORUS_URI,
    _effect_get_side_effect,
)
from uilib.misc import InputEvent

BUNDLE = "/path/to/replay.pedalboard"
SNAPSHOTS = {"0": "Clean", "1": "Lead"}
DRIVE_URI = "http://example.com/fixture/drive"
FIXTURE_IDS = ["drive", "neural_amp_modeler_lv2_1", "verb"]


@pytest.fixture(autouse=True)
def popen():
    with patch("modalapi.modhandler.subprocess.Popen") as mock:
        yield mock


@pytest.fixture(autouse=True)
def jack_is_up(monkeypatch):
    monkeypatch.setattr("modalapi.jack_mute.JackMute.is_muted", lambda self: False)


@pytest.fixture(autouse=True)
def board_sync_never_swallows_an_error(caplog):
    """The handler logs and carries on when a sync step raises; a passing test must not hide one."""
    yield
    swallowed = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith(("board sync step failed", "Error handling"))
    ]
    assert swallowed == []


def _stamps(popen) -> list:
    return [c for c in popen.call_args_list if c.args and c.args[0][:2] == ["pistomp-stamp", "stamp"]]


def _fetcher(system: SystemFixture) -> FakeBoardFetcher:
    fetcher = system.handler.board_fetcher
    assert isinstance(fetcher, FakeBoardFetcher)
    return fetcher


def _ids(system: SystemFixture) -> list[str]:
    return [p.instance_id for p in system.handler.current.pedalboard.plugins]


def _install_fixture_board(system: SystemFixture) -> list[str]:
    """A LOAD window with board_load.txt, as mod-ui sends when the user picks a board."""
    system.handler.plugin_dict.update(load_plugin_info())
    serve_board(system, bundle=BUNDLE, snapshots=SNAPSHOTS)
    lines = load_replay("board_load.txt")
    play_window(system, lines)
    assert _ids(system) == FIXTURE_IDS
    assert system.handler.current.pedalboard.bundle == BUNDLE
    return lines


def _open_a_plugin_panel(system: SystemFixture):
    handler = system.handler
    lcd = handler.lcd
    lcd.main_panel.sel_widget(lcd.w_plugins[0])
    lcd.main_panel.input_event(InputEvent.LONG_CLICK)
    handler.poll_lcd_updates()
    panel = lcd.pstack.current
    assert panel is not lcd.main_panel
    return panel


def _move_off_the_blend_board(system: SystemFixture) -> None:
    handler = system.handler
    other = Pedalboard.empty(handler.customizer)
    other.title, other.bundle = "Elsewhere", "/path/to/new.pedalboard"
    handler.install_board(other, {0: "Default"}, 0)
    assert handler.blend_modes == {}


def _gets(system: SystemFixture, fragment: str) -> list[str]:
    return [c.args[0] for c in system.mock_get.call_args_list if fragment in c.args[0]]


def test_new_board_after_remove_all_shows_an_empty_untitled_board(v3_system, snapshot):
    system = v3_system
    handler = system.handler
    _install_fixture_board(system)
    board = handler.current.pedalboard
    assert board.plugins
    reinits = []
    original = handler.hardware.reinit
    handler.hardware.reinit = lambda cfg: reinits.append(cfg) or original(cfg)
    system.ws_bridge.sent.clear()

    system.ws_bridge.inject("remove :all")
    handler.poll_ws_messages()

    assert handler.current.pedalboard is board
    assert board.plugins == []
    assert board.connections == []
    assert board.title == UNTITLED
    assert board.bundle is None
    assert handler.current.presets == {0: "Default"}
    assert handler._board_sync.state is SyncState.IDLE
    assert reinits == []
    assert system.ws_bridge.sent == []
    snapshot("untitled")


def test_same_bundle_reload_with_the_same_structure_keeps_the_open_panel(v3_system, snapshot):
    system = v3_system
    handler = system.handler
    lines = _install_fixture_board(system)
    plugins = list(handler.current.pedalboard.plugins)
    panel = _open_a_plugin_panel(system)
    snapshot("panel_kept")

    play_window(system, [CONNECTED_MARKER, *lines])

    assert handler._board_sync.state is SyncState.IDLE
    assert handler._board_sync.applied == 2
    assert len(handler.current.pedalboard.plugins) == len(plugins)
    assert all(a is b for a, b in zip(handler.current.pedalboard.plugins, plugins))
    assert handler.lcd.pstack.current is panel
    snapshot("panel_kept")


def test_a_changed_structure_swaps_and_pops_the_panel(v3_system, snapshot):
    system = v3_system
    handler = system.handler
    lines = _install_fixture_board(system)
    plugins = list(handler.current.pedalboard.plugins)
    panel = _open_a_plugin_panel(system)
    first_connect = next(i for i, line in enumerate(lines) if line.startswith("connect "))
    extra = f"add /graph/drive_2 {DRIVE_URI} 400.0 150.0 0 0_0_1_0 0"
    changed = [*lines[:first_connect], extra, *lines[first_connect:]]

    play_window(system, [CONNECTED_MARKER, *changed])

    assert handler.current.pedalboard.bundle == BUNDLE
    assert _ids(system) == ["drive", "drive_2", "neural_amp_modeler_lv2_1", "verb"]
    assert not any(new is old for new in handler.current.pedalboard.plugins for old in plugins)
    assert handler.lcd.pstack.current is not panel
    assert panel not in handler.lcd.pstack.stack
    assert handler.lcd.pstack.current is handler.lcd.main_panel
    snapshot("swapped")


def test_reconnect_with_unsaved_edits_applies_the_live_board_without_blend_stamp_or_snapshot_load(blend_system, popen):
    system = blend_system
    handler = system.handler
    bundle = Path(handler.current.pedalboard.bundle)
    snapshots_file = bundle / "snapshots.json"
    stored = json.loads(snapshots_file.read_text())
    stored["snapshots"] = [s for s in stored["snapshots"] if s["name"] != "Blend"]
    snapshots_file.write_text(json.dumps(stored))
    _move_off_the_blend_board(system)
    on_disk = snapshots_file.read_bytes()
    handler.plugin_dict.update(load_plugin_info())
    serve_board(system, bundle=str(bundle), snapshots={"0": "Clean", "1": "Lead", "2": "Blend"})
    # The uninstalled plugin would make the worker POST effect/bulk; the window is
    # reduced to the plugins whose metadata is already known.
    lines = [line for line in load_replay("connect_dump_unsaved.txt") if "mystery" not in line]
    assert any(line.startswith("loading_start 1 1") for line in lines)
    system.mock_get.reset_mock(side_effect=False)
    system.mock_post.reset_mock(side_effect=False)

    play_window(system, [CONNECTED_MARKER, *lines])

    assert _ids(system) == ["drive", "drive_2"]
    assert handler.current.pedalboard.bundle == str(bundle)
    assert _gets(system, "snapshot/load") == []
    assert system.mock_post.call_args_list == []
    assert _stamps(popen) == []
    assert handler.blend_modes == {}
    assert handler.active_blend_mode is None
    assert snapshots_file.read_bytes() == on_disk


def test_a_load_window_with_a_bundle_syncs_blend_and_stamps(blend_system, popen):
    system = blend_system
    handler = system.handler
    blend_bundle = handler.current.pedalboard.bundle
    _move_off_the_blend_board(system)
    handler.plugin_dict.update(load_plugin_info())
    serve_board(system, bundle=blend_bundle, snapshots={"0": "Clean", "1": "Lead", "2": "Blend"})
    popen.reset_mock()
    system.mock_get.reset_mock(side_effect=False)

    play_window(system, load_replay("board_load.txt"))

    assert handler.current.pedalboard.bundle == blend_bundle
    assert list(handler.blend_modes) == ["Blend"]
    assert handler.active_blend_mode is handler.blend_modes["Blend"]
    assert handler.current.preset_index == 2
    assert len(_gets(system, "snapshot/load?id=2")) == 1
    assert len(_stamps(popen)) == 1
    assert _stamps(popen)[0].args[0] == ["pistomp-stamp", "stamp", blend_bundle]


def test_startup_with_no_stream_shows_an_empty_board_then_applies_a_late_stream(v3_system, snapshot):
    system = v3_system
    handler = system.handler
    handler.plugin_dict.update(load_plugin_info())
    serve_board(system, bundle=BUNDLE, snapshots=SNAPSHOTS)
    handler._current = None

    handler.await_initial_board(timeout_s=0.0, sleep=lambda s: None)

    assert handler.current.pedalboard.plugins == []
    assert handler.current.pedalboard.title == UNTITLED
    assert handler.current.pedalboard.bundle is None
    assert handler._board_sync.applied == 0
    snapshot("empty")

    play_window(system, [CONNECTED_MARKER, *load_replay("board_load.txt")])

    assert handler._board_sync.applied == 1
    assert _ids(system) == FIXTURE_IDS
    assert handler.current.pedalboard.title == "Fixture Rig"
    assert handler.current.pedalboard.bundle == BUNDLE
    assert handler.current.presets == {0: "Clean", 1: "Lead"}
    snapshot("late")


def test_metadata_failure_builds_a_bypass_only_tile_and_never_exits(v3_system, snapshot, monkeypatch):
    system = v3_system
    handler = system.handler
    exits = []
    monkeypatch.setattr("sys.exit", lambda *args: exits.append(args))
    serve_board(system, bundle=BUNDLE, snapshots=SNAPSHOTS)
    assert handler.plugin_dict == {}

    play_window(system, load_replay("board_load.txt"))

    assert exits == []
    assert handler._board_sync.state is SyncState.IDLE
    assert handler.current.pedalboard.bundle == BUNDLE
    assert _ids(system) == FIXTURE_IDS
    for plugin in handler.current.pedalboard.plugins:
        assert list(plugin.parameters) == [BYPASS_SYMBOL]
    assert handler.current.pedalboard.plugins[2].is_bypassed()
    assert len(handler.lcd.w_plugins) == len(FIXTURE_IDS)
    snapshot("bypass_only")


def test_a_pending_live_add_replays_its_buffered_messages(v3_system):
    system = v3_system
    handler = system.handler
    _install_fixture_board(system)
    system.mock_get.side_effect = _effect_get_side_effect({_EXTRA_CHORUS_URI: _EXTRA_CHORUS_INFO})
    fetcher = _fetcher(system)
    fetcher.hold = True
    anchor = FIXTURE_IDS[-1]

    for line in (
        f"add /graph/ExtraChorus {_EXTRA_CHORUS_URI} 1100.0 50.0 1 1 1",
        "param_set /graph/ExtraChorus rate 3.500000",
        "param_set /graph/ExtraChorus :bypass 0.000000",
        f"connect /graph/ExtraChorus/out_L /graph/{anchor}/in_L",
    ):
        system.ws_bridge.inject(line)
    handler.poll_ws_messages()

    assert "ExtraChorus" not in _ids(system)
    assert fetcher.requests == [(_EXTRA_CHORUS_URI,)]
    assert all(c.src.id != "ExtraChorus" for c in handler.current.pedalboard.connections)

    fetcher.release()
    handler.poll_ws_messages()

    added = handler.current.pedalboard.find_plugin("ExtraChorus")
    assert added is not None
    assert added.parameters[Symbol("rate")].value == pytest.approx(3.5)
    assert not added.is_bypassed()
    assert any(c.src.id == "ExtraChorus" for c in handler.current.pedalboard.connections)
    assert [i for i in _ids(system) if i in FIXTURE_IDS] == FIXTURE_IDS
    assert not handler._pending_adds


def test_save_as_rebases_bundle_title_and_config(v3_system, tmp_path, popen):
    system = v3_system
    handler = system.handler
    hw = system.hw
    _install_fixture_board(system)
    plugins = list(handler.current.pedalboard.plugins)
    footswitch = hw.footswitches[1]
    assert footswitch.midi_CC == 61

    saved = tmp_path / "SavedAs.pedalboard"
    saved.mkdir()
    (saved / "config.yml").write_text(yaml.dump({"hardware": {"footswitches": [{"id": 1, "preset": 0}]}}))
    serve_board(
        system,
        bundle=str(saved),
        snapshots=SNAPSHOTS,
        boards=[
            {Token.TITLE: "Integration Rig", Token.BUNDLE: "/path/to/rig.pedalboard"},
            {Token.TITLE: "Saved As", Token.BUNDLE: str(saved)},
        ],
    )
    reinits = []
    original = hw.reinit
    hw.reinit = lambda cfg: reinits.append(cfg) or original(cfg)
    popen.reset_mock()
    system.mock_get.reset_mock(side_effect=False)
    last_json = Path(handler.data_dir) / "last.json"
    last_json.write_text(json.dumps({"pedalboard": str(saved)}))
    os.utime(last_json, (9999, 9999))

    handler.poll_modui_changes()
    handler.poll_modui_changes()

    board = handler.current.pedalboard
    assert board.bundle == str(saved)
    assert board.title == "Saved As"
    assert len(board.plugins) == len(plugins)
    assert all(a is b for a, b in zip(board.plugins, plugins))
    assert len(reinits) == 1
    assert footswitch.midi_CC is None
    assert len(_gets(system, "pedalboard/list")) == 1
    assert [p.title for p in handler.pedalboard_list] == ["Integration Rig", "Saved As"]
    assert str(saved) in handler.known_bundles()
    assert len(_stamps(popen)) == 1
    assert handler._board_sync.state is SyncState.IDLE
