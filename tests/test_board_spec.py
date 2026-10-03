"""BoardBuilder folds mod-ui's board-scoped stream into a frozen BoardSpec."""

import dataclasses
import subprocess
import sys
from pathlib import Path
from types import MappingProxyType

import pytest

from common.parameter import BYPASS_SYMBOL, Symbol
from modalapi.board_spec import BoardBuilder, BoardSpec, MidiMapSpec, PluginSpec
from modalapi.ws_protocol import CONNECTED_MARKER, UnknownMessage, parse_message
from tests.replay_helpers import feed, load_replay, spec_of

PROJECT_ROOT = Path(__file__).parent.parent
DRIVE = "http://example.com/fixture/drive"
MODEL = "http://github.com/mikeoliphant/neural-amp-modeler-lv2#model"
BPM = Symbol(":bpm")
NAM_PATH = "/home/pistomp/data/user-files/NAM Models/Clean (G1 L0 B1 T1).nam"


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
    builder = feed(
        BoardBuilder(),
        [
            _add("a"),
            "param_set /graph/a gain 0.250000",
            "midi_map /graph/a gain 0 61 0.0 1.0",
            f"patch_set /graph/a 1 {MODEL} p /x.nam",
            "midi_map /pedalboard :bpm 0 70 20.000000 280.000000",
        ],
    )
    spec = builder.freeze()
    feed(
        builder,
        [
            _add("b"),
            "param_set /graph/a gain 0.900000",
            "midi_map /graph/a gain 0 62 0.0 1.0",
            "midi_map /graph/a tone 0 63 0.0 1.0",
            f"patch_set /graph/a 1 {MODEL} p /y.nam",
            "midi_map /pedalboard :bpm -1 -1 0.0 1.0",
        ],
    )
    a = spec.plugins["a"]
    assert list(spec.plugins) == ["a"]
    assert dict(a.values) == {Symbol("gain"): 0.25}
    assert dict(a.midi) == {Symbol("gain"): MidiMapSpec(0, 61, 0.0, 1.0)}
    assert dict(a.patches) == {MODEL: "/x.nam"}
    assert dict(spec.transport_midi) == {BPM: MidiMapSpec(0, 70, 20.0, 280.0)}
    assert isinstance(spec.plugins, MappingProxyType)
    assert isinstance(spec.transport_midi, MappingProxyType)
    assert isinstance(a.values, MappingProxyType)
    assert isinstance(a.midi, MappingProxyType)
    assert isinstance(a.patches, MappingProxyType)


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


def test_bypass_follows_the_add_then_param_set_bypass():
    spec = spec_of(_add("a", bypassed=1), "param_set /graph/a :bypass 0.000000")
    assert spec.plugins["a"].bypassed is False


def test_param_set_records_the_last_value_per_symbol():
    spec = spec_of(
        _add("a"),
        "param_set /graph/a gain 0.200000",
        "param_set /graph/a gain 0.750000",
        "param_set /graph/a tone 0.500000",
    )
    assert dict(spec.plugins["a"].values) == {Symbol("gain"): 0.75, Symbol("tone"): 0.5}


def test_patch_set_keeps_the_raw_value_with_spaces():
    spec = spec_of(_add("nam"), f"patch_set /graph/nam 1 {MODEL} p /data/NAM Models/Clean (G1).nam")
    assert dict(spec.plugins["nam"].patches) == {MODEL: "/data/NAM Models/Clean (G1).nam"}


def test_plugin_pos_moves_the_plugin():
    spec = spec_of(_add("a", 100.0, 10.0), "plugin_pos /graph/a 900 40")
    assert (spec.plugins["a"].x, spec.plugins["a"].y) == (900.0, 40.0)


def test_midi_map_records_and_minus_one_unmaps():
    mapped = spec_of(
        _add("a"),
        "midi_map /graph/a gain 0 61 0.200000 0.800000",
        "midi_map /graph/a :bypass 0 60 0.0 1.0",
    )
    midi = mapped.plugins["a"].midi
    assert midi[Symbol("gain")] == MidiMapSpec(channel=0, controller=61, minimum=0.2, maximum=0.8)
    assert midi[Symbol("gain")].binding == "0:61"
    assert midi[Symbol("gain")].binding_range == (0.2, 0.8)
    assert midi[BYPASS_SYMBOL].binding == "0:60"

    unmapped = spec_of(
        _add("a"),
        "midi_map /graph/a gain 0 61 0.200000 0.800000",
        "midi_map /graph/a :bypass 0 60 0.0 1.0",
        "midi_map /graph/a gain -1 -1 0.0 1.0",
        "midi_map /graph/a :bypass -1 -1 0.0 1.0",
    )
    assert dict(unmapped.plugins["a"].midi) == {}


def test_degenerate_midi_range_is_no_binding_range():
    assert MidiMapSpec(channel=0, controller=1, minimum=1.0, maximum=1.0).binding_range is None


def test_transport_pseudo_instance_keeps_only_its_midi_maps():
    spec = spec_of(
        "midi_map /pedalboard :bpm 0 70 20.000000 280.000000",
        "midi_map /pedalboard :rolling 0 71 0.000000 1.000000",
        "param_set /pedalboard :bpm 96.000000",
        "midi_map /pedalboard :rolling -1 -1 0.0 1.0",
    )
    assert dict(spec.plugins) == {}
    assert dict(spec.transport_midi) == {BPM: MidiMapSpec(channel=0, controller=70, minimum=20.0, maximum=280.0)}


def test_a_new_window_drops_the_previous_transport_maps():
    builder = feed(BoardBuilder(), ["midi_map /pedalboard :bpm 0 70 20.000000 280.000000"])
    spec = feed(builder, ["loading_start 0 0", "loading_end 0 Rig"]).freeze()
    assert dict(spec.transport_midi) == {}


def test_value_messages_for_an_instance_not_on_the_board_are_dropped():
    spec = spec_of(
        _add("a"),
        "remove /graph/a",
        "param_set /graph/a gain 0.500000",
        "param_set /graph/a :bypass 1.000000",
        f"patch_set /graph/a 1 {MODEL} p /x.nam",
        "plugin_pos /graph/a 1 2",
        "midi_map /graph/a gain 0 61 0.0 1.0",
    )
    assert dict(spec.plugins) == {}


def test_pedalboard_reexports_the_transport_instance_id():
    import modalapi.board_spec as board_spec
    import modalapi.pedalboard as pedalboard

    assert pedalboard.TRANSPORT_INSTANCE_ID == board_spec.TRANSPORT_INSTANCE_ID == "pedalboard"


FIXTURES = ["connect_dump_saved.txt", "connect_dump_unsaved.txt", "board_load.txt", "reset_then_load.txt"]


def _replayed(*names: str) -> BoardSpec:
    builder = BoardBuilder()
    for name in names:
        feed(builder, load_replay(name))
    return builder.freeze()


@pytest.mark.parametrize("name", FIXTURES)
def test_every_fixture_line_parses(name: str):
    unknown = [line for line in load_replay(name) if isinstance(parse_message(line), UnknownMessage)]
    assert all(line.startswith(("sys_stats ", "stats ")) for line in unknown), unknown


def test_saved_connect_dump():
    spec = _replayed("connect_dump_saved.txt")
    assert list(spec.plugins) == ["drive", "neural_amp_modeler_lv2_1", "verb"]
    assert (spec.empty, spec.modified, spec.snapshot_id, spec.title) == (False, False, 0, "Fixture Rig")
    drive = spec.plugins["drive"]
    assert dict(drive.values) == {Symbol("gain"): 0.75, Symbol("tone"): 0.5, Symbol("level"): 0.0}
    assert set(drive.midi) == {BYPASS_SYMBOL, Symbol("gain")}
    assert spec.plugins["verb"].bypassed is True
    assert spec.plugins["neural_amp_modeler_lv2_1"].patches[MODEL] == NAM_PATH
    assert dict(spec.transport_midi) == {BPM: MidiMapSpec(channel=0, controller=70, minimum=20.0, maximum=280.0)}
    assert len(spec.connections) == 5


def test_board_load_streams_only_non_default_values():
    loaded = _replayed("board_load.txt")
    saved = _replayed("connect_dump_saved.txt")
    assert dict(loaded.plugins["drive"].values) == {Symbol("gain"): 0.75}
    assert dict(loaded.plugins["verb"].values) == {}
    assert loaded.connections == saved.connections
    assert {i: p.midi for i, p in loaded.plugins.items()} == {i: p.midi for i, p in saved.plugins.items()}


def test_unsaved_connect_dump_of_an_untitled_board():
    spec = _replayed("connect_dump_unsaved.txt")
    assert (spec.empty, spec.modified, spec.snapshot_id, spec.title) == (True, True, 0, "")
    assert [p.instance_number for p in spec.plugins.values()] == [0, 1, 2]
    assert spec.plugins["drive_2"].bypassed is True
    assert dict(spec.plugins["mystery"].values) == {Symbol("amount"): 0.25}


def test_reset_then_load_replaces_the_scratch_board():
    spec = _replayed("connect_dump_unsaved.txt", "reset_then_load.txt")
    assert spec == dataclasses.replace(_replayed("board_load.txt"), snapshot_id=1)
