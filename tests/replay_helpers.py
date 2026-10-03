"""Replay fixtures and the routing BoardSync will do, shared by the board-spec tests."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from modalapi.board_spec import BoardBuilder, BoardSpec
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
    """One queued wire message per line. A trailing space is part of the wire."""
    return (REPLAY_DIR / name).read_text(encoding="utf-8").splitlines()


def load_plugin_info() -> dict[str, dict]:
    """effect/get-shaped metadata for the fixture URIs, bar the uninstalled one."""
    return json.loads((REPLAY_DIR / "plugin_info.json").read_text(encoding="utf-8"))
