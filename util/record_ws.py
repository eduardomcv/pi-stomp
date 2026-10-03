#!/usr/bin/env python3

# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of pi-stomp.
#
# pi-stomp is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pi-stomp is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pi-stomp.  If not, see <https://www.gnu.org/licenses/>.

"""Record mod-ui's WebSocket feed as a replay fixture: one wire line per message,
filtered as pi-stomp's bridge filters them. The bridge's synthetic connect marker is
not included; prepend CONNECTED_MARKER to model a connect. Connecting makes mod-ui
replay its connect dump, so --windows 1 captures that alone; load a board in the web
UI during a --windows 2 capture to get `remove :all` and the load window too."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Iterator

from websockets.sync.client import connect
from websockets.sync.connection import Connection

DEFAULT_URL = "ws://pistomp.local/websocket"
# The bridge answers or drops these before they reach its received queue.
_DROPPED = ("data_ready ", "output_set ")


def keep(message: str) -> bool:
    return message != "ping" and not message.startswith(_DROPPED)


def take_windows(messages: Iterable[str], windows: int) -> Iterator[str]:
    """Kept messages up to and including the `windows`-th loading_end."""
    seen = 0
    for message in messages:
        if not keep(message):
            continue
        yield message
        if message.startswith("loading_end"):
            seen += 1
            if seen >= windows:
                return


def _feed(ws: Connection) -> Iterator[str]:
    for message in ws:
        if not isinstance(message, str):
            continue
        # mod-host holds all feedback until some client acks data_ready.
        if message == "ping":
            ws.send("pong")
        elif message.startswith("data_ready "):
            ws.send(message)
        yield message


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="fixture to write, e.g. tests/fixtures/replay/device_connect_dump.txt")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--windows", type=int, default=1, help="stop after this many loading_end lines")
    args = parser.parse_args(argv)
    with connect(args.url, max_size=None) as ws, open(args.output, "w", encoding="utf-8") as out:
        for message in take_windows(_feed(ws), args.windows):
            out.write(message + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
