"""Replay fixtures and the routing BoardSync will do, shared by the board-spec tests."""

from __future__ import annotations

from collections.abc import Iterable

from modalapi.board_spec import BoardBuilder, BoardSpec
from modalapi.ws_protocol import LoadingEndMessage, LoadingStartMessage, parse_message


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
