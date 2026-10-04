import dataclasses
import subprocess
import sys
from typing import Literal, get_args, get_origin

import pytest

from common.parameter import BYPASS_SYMBOL, Symbol
from modalapi.pending_add import PendingAdds, instances_of
from modalapi.ws_protocol import (
    AddPluginMessage,
    ConnectMessage,
    DisconnectMessage,
    MidiMapMessage,
    ParamSetMessage,
    PatchSetMessage,
    PluginBypassMessage,
    PluginPosMessage,
    RemovePluginMessage,
    WebSocketMessage,
)

DRIVE = "http://example.com/drive"
VERB = "http://example.com/verb"


def add(instance: str, uri: str = DRIVE, bypassed: bool = False) -> AddPluginMessage:
    return AddPluginMessage(instance=instance, uri=uri, x=10.0, y=20.0, bypassed=bypassed)


def gain(instance: str, value: float = 0.5) -> ParamSetMessage:
    return ParamSetMessage(instance=instance, symbol=Symbol("gain"), value=value)


def connect(src: str, dst: str) -> ConnectMessage:
    return ConnectMessage(port_from=f"/graph/{src}/out_L", port_to=f"/graph/{dst}/in_L")


@pytest.mark.parametrize(
    ("msg", "expected"),
    [
        (gain("A"), ("A",)),
        (PluginBypassMessage(instance="A", bypassed=True), ("A",)),
        (
            MidiMapMessage(instance="A", symbol=BYPASS_SYMBOL, channel=0, controller=60, minimum=0.0, maximum=1.0),
            ("A",),
        ),
        (PatchSetMessage(instance="A", param_uri="http://p", value_type="s", value="x y"), ("A",)),
        (RemovePluginMessage(instance="A"), ("A",)),
        (PluginPosMessage(instance="A", x=1.0, y=2.0), ("A",)),
        (connect("A", "B"), ("A", "B")),
        (DisconnectMessage(port_from="/graph/capture_1", port_to="/graph/A/in_L"), ("capture_1", "A")),
        (add("A"), ()),
    ],
)
def test_instances_of(msg, expected):
    assert instances_of(msg) == expected


def test_start_asks_for_a_uri_only_once():
    pending = PendingAdds()
    assert pending.start(add("A", DRIVE)) is True
    assert pending.start(add("B", DRIVE)) is False
    assert pending.start(add("C", VERB)) is True


def test_a_repeated_add_for_the_same_instance_refreshes_it_without_a_new_request():
    pending = PendingAdds()
    assert pending.start(add("A", bypassed=False)) is True
    assert pending.start(add("A", bypassed=True)) is False
    [resolved] = pending.resolve([DRIVE])
    assert resolved.add.bypassed is True


def test_bool_tracks_whether_anything_is_pending():
    pending = PendingAdds()
    assert not pending
    pending.start(add("A"))
    assert pending
    pending.clear()
    assert not pending


def test_defer_parks_messages_for_a_pending_instance_in_order():
    pending = PendingAdds()
    pending.start(add("A"))
    first, second = gain("A", 0.1), gain("A", 0.9)
    assert pending.defer(first) is True
    assert pending.defer(second) is True
    [resolved] = pending.resolve([DRIVE])
    assert resolved.buffered == [first, second]


def test_defer_lets_messages_for_other_instances_through():
    pending = PendingAdds()
    pending.start(add("A"))
    assert pending.defer(gain("Known")) is False
    assert pending.defer(connect("Known", "Other")) is False
    assert PendingAdds().defer(gain("A")) is False


def test_a_connect_touching_a_pending_instance_is_parked():
    pending = PendingAdds()
    pending.start(add("A"))
    msg = connect("A", "Known")
    assert pending.defer(msg) is True
    [resolved] = pending.resolve([DRIVE])
    assert resolved.buffered == [msg]


def test_remove_cancels_the_pending_add_and_its_buffer():
    pending = PendingAdds()
    pending.start(add("A"))
    pending.defer(gain("A"))
    assert pending.defer(RemovePluginMessage(instance="A")) is True
    assert not pending
    assert pending.resolve([DRIVE]) == []


def test_remove_purges_messages_other_pending_adds_parked_about_it():
    pending = PendingAdds()
    pending.start(add("A", DRIVE))
    pending.start(add("B", VERB))
    pending.defer(connect("A", "B"))
    pending.defer(gain("B"))
    pending.defer(RemovePluginMessage(instance="A"))
    [b] = pending.resolve([VERB])
    assert b.buffered == [gain("B")]


def test_resolve_pops_only_the_requested_uris_in_arrival_order():
    pending = PendingAdds()
    pending.start(add("A", DRIVE))
    pending.start(add("B", VERB))
    pending.start(add("C", DRIVE))
    assert [p.add.instance for p in pending.resolve([DRIVE])] == ["A", "C"]
    assert [p.add.instance for p in pending.resolve([VERB])] == ["B"]
    assert not pending


def test_a_connect_between_two_pending_adds_replays_with_the_last_to_resolve():
    pending = PendingAdds()
    pending.start(add("A", DRIVE))
    pending.start(add("B", VERB))
    msg = connect("A", "B")
    pending.defer(msg)
    [a] = pending.resolve([DRIVE])
    assert a.buffered == []
    [b] = pending.resolve([VERB])
    assert b.buffered == [msg]


def test_a_connect_between_two_pending_adds_resolved_together_replays_once():
    pending = PendingAdds()
    pending.start(add("A", DRIVE))
    pending.start(add("B", VERB))
    msg = connect("A", "B")
    pending.defer(msg)
    resolved = pending.resolve([DRIVE, VERB])
    assert [m for p in resolved for m in p.buffered] == [msg]


def test_pending_add_imports_only_the_protocol():
    code = (
        "import sys, modalapi.pending_add;"
        "bad = [m for m in ('modalapi.modhandler', 'modalapi.pedalboard', 'modalapi.board_fetch') if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0


def _sample_value(name: str, annotation: object) -> object:
    if name in {"port_from", "port_to"}:
        return "/graph/a/out"
    if name == "instance":
        return "a"
    if annotation is Symbol:
        return Symbol("x")
    if get_origin(annotation) is Literal:
        return get_args(annotation)[0]
    if annotation is str:
        return ""
    if annotation is bool:
        return False
    if annotation is float:
        return 0.0
    return 0


def _sample(cls: type) -> WebSocketMessage:
    kwargs = {f.name: _sample_value(f.name, f.type) for f in dataclasses.fields(cls) if f.init}
    return cls(**kwargs)


def test_a_plugin_pos_for_a_pending_instance_is_parked():
    pending = PendingAdds()
    pending.start(add("A"))
    msg = PluginPosMessage(instance="A", x=5.0, y=6.0)
    assert pending.defer(msg) is True
    [resolved] = pending.resolve([DRIVE])
    assert resolved.buffered == [msg]


def test_instances_of_covers_every_message_that_names_a_plugin():
    named = {"instance", "port_from", "port_to"}
    checked = 0
    for cls in get_args(WebSocketMessage):
        if cls is AddPluginMessage:
            continue
        if {f.name for f in dataclasses.fields(cls)} & named:
            assert instances_of(_sample(cls)) != (), f"{cls.__name__} names a plugin but instances_of ignores it"
            checked += 1
    assert checked >= 8
