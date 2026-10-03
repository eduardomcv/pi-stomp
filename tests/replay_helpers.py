"""Replay fixtures and the routing BoardSync will do, shared by the board-spec tests."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from common.parameter import BYPASS_SYMBOL, Parameter
from modalapi.board_spec import BoardBuilder, BoardSpec
from modalapi.connections import Endpoint
from modalapi.pedalboard import BPB_SYMBOL, BPM_SYMBOL, ROLLING_SYMBOL, Pedalboard
from modalapi.ws_protocol import LoadingEndMessage, LoadingStartMessage, parse_message

REPLAY_DIR = Path(__file__).parent / "fixtures" / "replay"


def feed(builder: BoardBuilder, lines: Iterable[str]) -> BoardBuilder:
    """Route wire lines as BoardSync will: window markers to start/end, the rest to apply."""
    for line in lines:
        msg = parse_message(line)
        if isinstance(msg, LoadingStartMessage):
            builder.start(msg)
        elif isinstance(msg, LoadingEndMessage):
            builder.end(msg)
        else:
            builder.apply(msg)
    return builder


def spec_of(*lines: str) -> BoardSpec:
    return feed(BoardBuilder(), lines).freeze()


def load_replay(name: str) -> list[str]:
    """mod-ui wire lines, filtered as the bridge filters them; no synthetic connect marker
    (prepend CONNECTED_MARKER to model a connect). A trailing space is part of the wire."""
    return (REPLAY_DIR / name).read_text(encoding="utf-8").splitlines()


def load_plugin_info() -> dict[str, dict]:
    """effect/get-shaped metadata for the fixture URIs, bar the uninstalled one."""
    return json.loads((REPLAY_DIR / "plugin_info.json").read_text(encoding="utf-8"))


def _port_path(endpoint: Endpoint) -> str:
    return f"/graph/{endpoint.id}/{endpoint.port_symbol}" if endpoint.port_symbol else f"/graph/{endpoint.id}"


def _midi_map(instance_path: str, param: Parameter) -> list[str]:
    if param.binding is None:
        return []
    channel, controller = param.binding.split(":")
    return [f"midi_map {instance_path} {param.symbol} {channel} {controller} {param.minimum:f} {param.maximum:f}"]


def board_to_replay_lines(pedalboard: Pedalboard, empty: bool, modified: bool, sid: int) -> list[str]:
    """`pedalboard` as mod-ui's connect dump replays it (Host.report_current_state),
    from the transport line through loading_end. Extra data has no generic inverse:
    append any patch_set lines yourself."""
    tp = pedalboard.transport_plugin.parameters
    lines = [
        f"transport {int(tp[ROLLING_SYMBOL].value)} {tp[BPB_SYMBOL].value:f} {tp[BPM_SYMBOL].value:f} none",
        f"loading_start {int(empty)} {int(modified)}",
    ]
    for p in pedalboard.plugins:
        number = "" if p.instance_number is None else f" {p.instance_number}"
        uri = p.uri or f"urn:pistomp:test:{p.instance_id}"
        bypassed = int(p.is_bypassed())
        lines.append(f"add /graph/{p.instance_id} {uri} {p.canvas_x:.1f} {p.canvas_y:.1f} {bypassed} 0_0_0_0 0{number}")
    for p in pedalboard.plugins:
        path = f"/graph/{p.instance_id}"
        lines += [f"param_set {path} {s} {q.value:f}" for s, q in p.parameters.items() if s != BYPASS_SYMBOL]
    lines += [f"connect {_port_path(c.src)} {_port_path(c.dst)}" for c in pedalboard.connections]
    for p in pedalboard.plugins:
        for q in p.parameters.values():
            lines += _midi_map(f"/graph/{p.instance_id}", q)
    for q in tp.values():
        lines += _midi_map("/pedalboard", q)
    lines.append(f"loading_end {sid} {pedalboard.title}")
    return lines
