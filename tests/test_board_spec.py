"""BoardBuilder folds mod-ui's board-scoped stream into a frozen BoardSpec."""

import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

from modalapi.board_spec import BoardBuilder, PluginSpec
from modalapi.ws_protocol import CONNECTED_MARKER, parse_message
from tests.replay_helpers import feed, spec_of

PROJECT_ROOT = Path(__file__).parent.parent
DRIVE = "http://example.com/fixture/drive"


def _add(instance: str, x: float = 0.0, y: float = 0.0, *, bypassed: int = 0, number: int | None = None) -> str:
    suffix = "" if number is None else f" {number}"
    return f"add /graph/{instance} {DRIVE} {x:.1f} {y:.1f} {bypassed} 0_0_1_0 0{suffix}"


def test_fresh_builder_is_an_empty_untitled_board():
    spec = BoardBuilder().freeze()
    assert dict(spec.plugins) == {}
    assert spec.connections == ()
    assert dict(spec.transport_midi) == {}
    assert (spec.empty, spec.modified, spec.snapshot_id, spec.title) == (True, False, 0, "")


def test_window_records_its_flags_and_title():
    spec = spec_of("loading_start 0 1", "loading_end 3 Fixture Rig")
    assert (spec.empty, spec.modified, spec.snapshot_id, spec.title) == (False, True, 3, "Fixture Rig")


def test_add_records_plugins_in_add_order():
    spec = spec_of(_add("b", 300.0, 20.0, bypassed=1), _add("a", 100.0, 10.0, number=4))
    assert list(spec.plugins) == ["b", "a"]
    assert spec.plugins["a"] == PluginSpec(
        instance="a",
        uri=DRIVE,
        x=100.0,
        y=10.0,
        bypassed=False,
        instance_number=4,
        values=MappingProxyType({}),
        midi=MappingProxyType({}),
        patches=MappingProxyType({}),
    )
    assert spec.plugins["b"].bypassed is True
    assert spec.plugins["b"].instance_number is None


def test_start_replaces_the_previous_window():
    builder = feed(BoardBuilder(), ["loading_start 0 0", _add("old"), "loading_end 0 Old"])
    spec = feed(builder, ["loading_start 1 1", _add("new"), "loading_end 0 "]).freeze()
    assert list(spec.plugins) == ["new"]
    assert (spec.empty, spec.modified, spec.title) == (True, True, "")


def test_remove_all_mid_window_clears_the_graph_and_the_window_continues():
    spec = spec_of(
        "loading_start 0 0",
        _add("a"),
        "connect /graph/capture_1 /graph/a/in",
        "remove :all",
        _add("b"),
        "connect /graph/capture_1 /graph/b/in",
        "loading_end 1 Rig",
    )
    assert list(spec.plugins) == ["b"]
    assert spec.connections == (("/graph/capture_1", "/graph/b/in"),)
    assert (spec.snapshot_id, spec.title) == (1, "Rig")


def test_remove_all_outside_a_window_resets_to_the_empty_board():
    builder = feed(BoardBuilder(), ["loading_start 0 1", _add("a"), "loading_end 2 Rig"])
    spec = feed(builder, ["remove :all"]).freeze()
    assert dict(spec.plugins) == {}
    assert (spec.empty, spec.modified, spec.snapshot_id, spec.title) == (True, False, 0, "")


def test_remove_drops_the_plugin_and_every_edge_touching_it():
    spec = spec_of(
        _add("a"),
        _add("b"),
        "connect /graph/capture_1 /graph/a/in",
        "connect /graph/a/out /graph/b/in",
        "connect /graph/b/out /graph/playback_1",
        "remove /graph/a",
    )
    assert list(spec.plugins) == ["b"]
    assert spec.connections == (("/graph/b/out", "/graph/playback_1"),)


def test_connect_naming_an_unknown_plugin_is_dropped():
    spec = spec_of(
        _add("a"),
        _add("b"),
        "remove /graph/a",
        "connect /graph/a/out /graph/b/in",
        "connect /graph/ghost/out /graph/playback_1",
        "connect /graph/capture_1 /graph/playback_1",
    )
    assert spec.connections == (("/graph/capture_1", "/graph/playback_1"),)


def test_disconnect_and_duplicate_connect():
    spec = spec_of(
        _add("a"),
        "connect /graph/capture_1 /graph/a/in",
        "connect /graph/capture_1 /graph/a/in",
        "connect /graph/a/out /graph/playback_1",
        "disconnect /graph/capture_1 /graph/a/in",
        "disconnect /graph/a/out /graph/playback_2",
    )
    assert spec.connections == (("/graph/a/out", "/graph/playback_1"),)


def test_freeze_is_a_snapshot_the_builder_cannot_reach():
    builder = feed(BoardBuilder(), [_add("a")])
    spec = builder.freeze()
    builder.apply(parse_message(_add("b")))
    builder.apply(parse_message("param_set /graph/a gain 0.900000"))
    assert list(spec.plugins) == ["a"]
    assert dict(spec.plugins["a"].values) == {}
    assert isinstance(spec.plugins, MappingProxyType)
    assert isinstance(spec.plugins["a"].values, MappingProxyType)


def test_messages_outside_the_board_vocabulary_are_ignored():
    spec = spec_of(
        "sys_stats 391152 1500000 48312",
        "transport 1 4.000000 96.000000 none",
        "add_hw_port /graph/capture_1 audio 0 Capture_1 1",
        "size 1240 620",
        "pedal_snapshot 1 Lead",
        CONNECTED_MARKER,
    )
    assert spec == BoardBuilder().freeze()


def test_board_spec_does_not_import_pedalboard():
    code = "import sys, modalapi.board_spec\nassert 'modalapi.pedalboard' not in sys.modules\n"
    subprocess.run([sys.executable, "-c", code], check=True, cwd=PROJECT_ROOT)
