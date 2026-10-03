"""Pedalboard.from_spec: the stream's BoardSpec plus injected LV2 metadata, no I/O."""

import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from common.parameter import BYPASS_SYMBOL, Symbol
from modalapi.board_spec import BoardBuilder, BoardSpec
from modalapi.connections import EndpointKind
from modalapi.pedalboard import BPB_SYMBOL, BPM_SYMBOL, ROLLING_SYMBOL, Pedalboard
from modalapi.plugin_customization import (
    Customizer,
    PatchParser,
    PluginCustomization,
    PluginExtraData,
    default_customizer,
)
from modalapi.ws_protocol import TransportMessage
from plugins.customization import lookup, patch_extra_data
from plugins.nam import NamData
from tests.replay_helpers import board_to_replay_lines, feed, load_plugin_info, load_replay, spec_of

PROJECT_ROOT = Path(__file__).parent.parent
CHORUS = "http://example.com/fixture/chorus"
SPLIT = "http://example.com/fixture/split"
NOTES_TEXT = "http://example.com/fixture/notes#text"
NAM_PATH = "/home/pistomp/data/user-files/NAM Models/Clean (G1 L0 B1 T1).nam"

INFO: dict[str, dict] = {
    CHORUS: {
        "name": "Fixture Chorus",
        "category": ["Modulator"],
        "ports": {
            "audio": {"input": [{"symbol": "in"}], "output": [{"symbol": "out"}]},
            "control": {
                "input": [
                    {"symbol": "rate", "shortName": "Rate", "ranges": {"minimum": 0.0, "maximum": 5.0, "default": 1.0}},
                    {"symbol": "depth", "shortName": "Depth", "ranges": {"minimum": 0.0, "maximum": 1.0, "default": 0.5}},
                    {"symbol": "mix", "shortName": "Mix", "ranges": {"minimum": 0.0, "maximum": 1.0}},
                ]
            },
        },
    },
    SPLIT: {
        "name": "Fixture Split",
        "category": ["Utility"],
        "ports": {
            "audio": {"input": [{"symbol": "in"}], "output": [{"symbol": "out_l"}, {"symbol": "out_r"}]},
            "control": {"input": []},
        },
    },
}


@dataclass(frozen=True)
class _Text(PluginExtraData):
    text: str


def _patch_text(uri: str | None, param_uri: str, value: str) -> PluginExtraData | None:
    return _Text(value) if param_uri == NOTES_TEXT else None


def _no_patch(uri: str | None, param_uri: str, value: str) -> PluginExtraData | None:
    return None


class _RecordingCustomizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str, int | None]] = []

    def __call__(
        self, uri: str | None, bundlepath: str = "", instance_number: int | None = None
    ) -> PluginCustomization:
        self.calls.append((uri, bundlepath, instance_number))
        return PluginCustomization()


def _build(
    spec: BoardSpec,
    *,
    transport: TransportMessage | None = None,
    customizer: Customizer = default_customizer,
    patch_parser: PatchParser = _no_patch,
    plugin_dict: Mapping[str, dict] = INFO,
) -> Pedalboard:
    return Pedalboard.from_spec(spec, plugin_dict, "/b/rig.pedalboard", "Rig", transport, customizer, patch_parser)


def _add(
    instance: str, uri: str = CHORUS, x: float = 0.0, y: float = 0.0, bypassed: int = 0, number: int | None = None
) -> str:
    suffix = "" if number is None else f" {number}"
    return f"add /graph/{instance} {uri} {x:.1f} {y:.1f} {bypassed} 0_0_1_0 0{suffix}"


@pytest.fixture(autouse=True)
def _no_http(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("from_spec must not touch the network")

    monkeypatch.setattr("pistomp.httpclient.get", refuse)
    monkeypatch.setattr("pistomp.httpclient.post", refuse)


def test_unstreamed_ports_default_from_lv2_metadata():
    pb = _build(spec_of(_add("chorus"), "param_set /graph/chorus rate 3.000000"))
    params = pb.plugins[0].parameters
    assert params[Symbol("rate")].value == 3.0
    assert params[Symbol("depth")].value == 0.5
    assert params[Symbol("mix")].value == 0.0


def test_plugins_sort_by_x_then_y_then_id():
    pb = _build(
        spec_of(_add("z", x=100.0), _add("a", x=100.0), _add("m", x=100.0, y=-10.0), _add("b", x=50.0, y=900.0))
    )
    assert [p.instance_id for p in pb.plugins] == ["b", "m", "a", "z"]


def test_bypass_canvas_category_and_instance_number_carry_over():
    pb = _build(spec_of(_add("chorus", x=200.0, y=150.0, bypassed=1, number=4)))
    plugin = pb.plugins[0]
    assert plugin.is_bypassed()
    assert (plugin.canvas_x, plugin.canvas_y) == (200.0, 150.0)
    assert plugin.instance_number == 4
    assert plugin.category == "Modulator"
    assert plugin.name == "Fixture Chorus"


def test_midi_maps_become_bindings_with_their_range():
    pb = _build(
        spec_of(
            _add("chorus"),
            "midi_map /graph/chorus rate 0 61 1.000000 4.000000",
            "midi_map /graph/chorus :bypass 0 60 0.0 1.0",
        )
    )
    params = pb.plugins[0].parameters
    rate = params[Symbol("rate")]
    assert rate.binding == "0:61"
    assert (rate.minimum, rate.maximum) == (1.0, 4.0)
    assert (rate.declared_minimum, rate.declared_maximum) == (0.0, 5.0)
    assert params[Symbol("depth")].binding is None
    assert params[BYPASS_SYMBOL].binding == "0:60"


def test_uri_without_metadata_is_a_bypass_only_plugin():
    pb = _build(
        spec_of(
            _add("mystery", uri="http://example.com/not-installed", x=600.0, bypassed=1),
            "param_set /graph/mystery amount 0.250000",
            "midi_map /graph/mystery :bypass 0 60 0.0 1.0",
        )
    )
    plugin = pb.plugins[0]
    assert list(plugin.parameters) == [BYPASS_SYMBOL]
    assert plugin.is_bypassed()
    assert plugin.parameters[BYPASS_SYMBOL].binding == "0:60"
    assert plugin.info is None
    assert plugin.name == "mystery"
    assert plugin.category is None
    assert plugin.canvas_x == 600.0


def test_extra_data_comes_from_streamed_patches():
    pb = _build(
        spec_of(
            _add("notes"),
            f"patch_set /graph/notes 1 {NOTES_TEXT} s first",
            "patch_set /graph/notes 1 http://example.com/other s ignored",
            f"patch_set /graph/notes 1 {NOTES_TEXT} s second line",
        ),
        patch_parser=_patch_text,
    )
    assert pb.plugins[0].extra_data == _Text("second line")


def test_customizer_gets_no_bundle_so_nothing_reads_effect_ttl():
    customizer = _RecordingCustomizer()
    _build(spec_of(_add("chorus", number=4)), customizer=customizer)
    assert [c for c in customizer.calls if c[0] == CHORUS] == [(CHORUS, "", None)]
    assert all(c[1:] == ("", None) for c in customizer.calls)


def test_connections_resolve_endpoints_and_port_indices():
    pb = _build(
        spec_of(
            _add("split", uri=SPLIT),
            "connect /graph/capture_1 /graph/split/in",
            "connect /graph/split/out_r /graph/playback_2",
        )
    )
    first, second = pb.connections
    assert (first.src.kind, first.src.id, first.dst.id, first.dst.port_symbol) == (
        EndpointKind.SOURCE,
        "capture_1",
        "split",
        "in",
    )
    assert (second.src.port_symbol, second.src.port_idx) == ("out_r", 1)
    assert (second.dst.kind, second.dst.port_idx) == (EndpointKind.SINK, 1)


def test_transport_plugin_takes_values_from_transport_and_bindings_from_the_stream():
    pb = _build(
        spec_of("midi_map /pedalboard :bpm 0 70 20.000000 280.000000"),
        transport=TransportMessage(rolling=True, bpm=96.0, beats_per_bar=3.0),
    )
    tp = pb.transport_plugin.parameters
    assert (tp[BPM_SYMBOL].value, tp[BPB_SYMBOL].value, tp[ROLLING_SYMBOL].value) == (96.0, 3.0, 1.0)
    assert tp[BPM_SYMBOL].binding == "0:70"
    assert tp[BPB_SYMBOL].binding is None


def test_transport_plugin_defaults_without_a_transport_message():
    tp = _build(spec_of()).transport_plugin.parameters
    assert (tp[BPM_SYMBOL].value, tp[BPB_SYMBOL].value, tp[ROLLING_SYMBOL].value) == (120.0, 4.0, 0.0)


def test_from_spec_board_is_hydrated_and_carries_identity():
    pb = Pedalboard.from_spec(spec_of(_add("chorus")), INFO, None, "Scratch", None, default_customizer, _no_patch)
    assert pb.hydrated
    assert (pb.title, pb.bundle) == ("Scratch", None)
    pb.hydrate({})
    assert pb.plugins[0].pedalboard_snapshot == {
        BYPASS_SYMBOL: 0.0,
        Symbol("rate"): 1.0,
        Symbol("depth"): 0.5,
        Symbol("mix"): 0.0,
    }


def test_empty_board():
    pb = Pedalboard.empty()
    assert (pb.title, pb.bundle, pb.plugins, pb.connections, pb.hydrated) == ("", None, [], [], True)
    pb.hydrate({})
    assert pb.transport_plugin.parameters[BPM_SYMBOL].value == 120.0


def test_pedalboard_never_imports_plugins():
    code = (
        "import sys, modalapi.pedalboard\n"
        "bad = sorted(m for m in sys.modules if m == 'plugins' or m.startswith('plugins.'))\n"
        "assert not bad, bad\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=PROJECT_ROOT)


def _replayed(
    *names: str, customizer: Customizer = default_customizer, patch_parser: PatchParser = _no_patch
) -> Pedalboard:
    builder = BoardBuilder()
    for name in names:
        feed(builder, load_replay(name))
    spec = builder.freeze()
    bundle = None if spec.empty else "/home/pistomp/data/.pedalboards/Fixture_Rig.pedalboard"
    return Pedalboard.from_spec(spec, load_plugin_info(), bundle, spec.title, None, customizer, patch_parser)


def _summary(pb: Pedalboard) -> list[tuple]:
    return [
        (
            p.instance_id,
            p.uri,
            p.canvas_x,
            p.canvas_y,
            p.instance_number,
            p.is_bypassed(),
            {s: (q.value, q.binding, q.minimum, q.maximum) for s, q in p.parameters.items()},
        )
        for p in [*pb.plugins, pb.transport_plugin]
    ]


def test_board_load_and_saved_connect_dump_build_the_same_board():
    loaded = _replayed("board_load.txt")
    replayed = _replayed("connect_dump_saved.txt")
    assert _summary(loaded) == _summary(replayed)
    assert set(loaded.connections) == set(replayed.connections)


def test_unsaved_dump_builds_the_untitled_scratch_board():
    pb = _replayed("connect_dump_unsaved.txt")
    assert (pb.title, pb.bundle) == ("", None)
    assert [p.instance_id for p in pb.plugins] == ["drive", "drive_2", "mystery"]
    assert [p.instance_number for p in pb.plugins] == [0, 1, 2]
    assert list(pb.plugins[2].parameters) == [BYPASS_SYMBOL]
    assert pb.plugins[1].is_bypassed()


def test_stream_names_the_nam_model_without_effect_ttl():
    pb = _replayed("board_load.txt", customizer=lookup, patch_parser=patch_extra_data)
    nam = pb.find_plugin("neural_amp_modeler_lv2_1")
    assert nam is not None
    assert nam.extra_data == NamData(model_path=NAM_PATH)


def test_board_to_replay_lines_round_trips_through_the_builder():
    pb = _replayed("connect_dump_saved.txt")
    lines = board_to_replay_lines(pb, False, False, 0)
    assert lines[0] == "transport 0 4.000000 120.000000 none"
    assert lines[1] == "loading_start 0 0"
    assert lines[-1] == "loading_end 0 Fixture Rig"
    again = Pedalboard.from_spec(
        feed(BoardBuilder(), lines).freeze(), load_plugin_info(), pb.bundle, pb.title, None, default_customizer, _no_patch
    )
    assert _summary(again) == _summary(pb)
    assert set(again.connections) == set(pb.connections)
