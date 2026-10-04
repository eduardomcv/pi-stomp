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

"""Folds mod-ui's board stream into the board pi-stomp shows.

A window (`loading_start … loading_end`) replays the whole board; mod-ui sends one on
every board load and on every connect. While one is open, board-scoped messages go to a
`BoardBuilder` instead of the live board. At `loading_end` one fetch job gathers what the
stream leaves out, and the board is then reconciled in place (same bundle and structure)
or swapped in.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import replace
from enum import Enum, auto
from typing import Protocol

from modalapi.board_fetch import BoardJob, BoardResolved
from modalapi.board_spec import BoardBuilder, BoardSpec
from modalapi.pedalboard import Pedalboard
from modalapi.plugin_customization import Customizer, PatchParser
from modalapi.ws_protocol import (
    AddPluginMessage,
    ConnectedMessage,
    ConnectMessage,
    DisconnectMessage,
    LoadingEndMessage,
    LoadingStartMessage,
    MidiMapMessage,
    ParamSetMessage,
    PatchSetMessage,
    PedalSnapshotMessage,
    PluginBypassMessage,
    PluginPosMessage,
    RemovePluginMessage,
    ResetMessage,
    TransportMessage,
    WebSocketMessage,
)

UNTITLED = "Untitled"
_DEFAULT_PRESETS = {0: "Default"}
_BUILD_TIMEOUT_S = 30.0
_RESOLVE_TIMEOUT_S = 30.0


class SyncState(Enum):
    IDLE = auto()
    BUILDING = auto()
    RESOLVING = auto()


class WindowKind(Enum):
    LOAD = auto()
    REPLAY = auto()


class BoardHost(Protocol):
    plugin_dict: dict[str, dict]
    customizer: Customizer
    patch_parser: PatchParser

    def current_board(self) -> Pedalboard | None: ...
    def known_bundles(self) -> frozenset[str]: ...
    def request_board(self, job: BoardJob) -> None: ...
    def show_loading(self) -> None: ...
    def abort_window(self) -> None: ...
    def install_board(
        self, board: Pedalboard, presets: dict[int, str], preset_index: int, *, sync_blend: bool
    ) -> None: ...
    def reconcile_board(self, candidate: Pedalboard, presets: dict[int, str], preset_index: int) -> None: ...
    def clear_board(self) -> None: ...
    def rebase_board(self, bundle: str | None, presets: dict[int, str] | None) -> None: ...
    def update_board_list(self, boards: tuple[tuple[str, str], ...]) -> None: ...


def _structure(board: Pedalboard) -> tuple:
    plugins = tuple((p.instance_id, p.uri) for p in board.plugins)
    arcs = frozenset((c.src.id, c.src.port_symbol, c.dst.id, c.dst.port_symbol) for c in board.connections)
    return plugins, arcs


def same_structure(a: Pedalboard, b: Pedalboard) -> bool:
    """Same plugins in the same (x, y, id) order with the same URIs, and the same wiring."""
    return _structure(a) == _structure(b)


class BoardSync:
    def __init__(self, host: BoardHost, clock: Callable[[], float] = time.monotonic) -> None:
        self._host = host
        self._clock = clock
        self._builder = BoardBuilder()
        self._state = SyncState.IDLE
        self._kind = WindowKind.LOAD
        self._replay_next = False
        self._ticket = 0
        self._deadline = 0.0
        self._transport: TransportMessage | None = None
        self._snapshot: PedalSnapshotMessage | None = None
        self._rebase_ticket: int | None = None
        self._rebase_again = False
        self.applied = 0

    @property
    def state(self) -> SyncState:
        return self._state

    def feed(self, msg: WebSocketMessage) -> bool:
        match msg:
            case ConnectedMessage():
                self._replay_next = True
                return True
            case TransportMessage():
                self._transport = msg
                return False
            case LoadingStartMessage():
                self._begin(msg)
                return False
            case LoadingEndMessage():
                self._end(msg)
                return False
            case ResetMessage():
                self._builder.apply(msg)
                if self._state is SyncState.IDLE:
                    self._host.clear_board()
                return True
            case PedalSnapshotMessage():
                if self._state is SyncState.IDLE:
                    return False
                self._snapshot = msg
                return True
            case (
                AddPluginMessage()
                | RemovePluginMessage()
                | ConnectMessage()
                | DisconnectMessage()
                | ParamSetMessage()
                | PluginBypassMessage()
                | MidiMapMessage()
                | PatchSetMessage()
                | PluginPosMessage()
            ):
                if self._state is SyncState.IDLE:
                    return False
                self._builder.apply(msg)
                return True
            case _:
                return False

    def poll(self) -> None:
        if self._state is SyncState.IDLE or self._clock() < self._deadline:
            return
        if self._state is SyncState.BUILDING:
            logging.warning("board window never ended; abandoning it")
            self._state = SyncState.IDLE
            self._host.abort_window()
        else:
            logging.warning("board resolution timed out; building with what the stream gave")
            self._complete(BoardResolved.failed(self._ticket))

    def request_rebase(self) -> None:
        if self._state is not SyncState.IDLE:
            return
        if self._rebase_ticket is not None:
            self._rebase_again = True
            return
        self._ticket += 1
        self._rebase_ticket = self._ticket
        self._host.request_board(BoardJob(self._ticket, known_bundles=self._host.known_bundles()))

    def on_resolved(self, result: BoardResolved) -> None:
        if self._state is SyncState.IDLE and result.ticket == self._rebase_ticket:
            self._rebase_ticket = None
            self._apply_rebase(result)
            if self._rebase_again:
                self._rebase_again = False
                self.request_rebase()
            return
        if self._state is SyncState.RESOLVING and result.ticket == self._ticket:
            self._complete(result)

    def _begin(self, msg: LoadingStartMessage) -> None:
        self._builder.start(msg)
        self._kind = WindowKind.REPLAY if self._replay_next else WindowKind.LOAD
        self._replay_next = False
        self._snapshot = None
        self._rebase_ticket = None
        self._rebase_again = False
        self._ticket += 1
        self._state = SyncState.BUILDING
        self._deadline = self._clock() + _BUILD_TIMEOUT_S
        if self._kind is WindowKind.LOAD:
            self._host.show_loading()

    def _end(self, msg: LoadingEndMessage) -> None:
        if self._state is not SyncState.BUILDING:
            return
        self._builder.end(msg)
        self._state = SyncState.RESOLVING
        self._deadline = self._clock() + _RESOLVE_TIMEOUT_S
        spec = self._builder.freeze()
        uris = sorted({p.uri for p in spec.plugins.values()} - self._host.plugin_dict.keys())
        targets = tuple(
            (p.instance, p.instance_number, p.uri)
            for p in spec.plugins.values()
            if p.instance_number is not None and not p.patches
        )
        self._host.request_board(BoardJob(self._ticket, tuple(uris), self._host.known_bundles(), targets))

    def _complete(self, result: BoardResolved) -> None:
        spec = self._builder.freeze()
        host = self._host
        host.plugin_dict.update(result.metadata)
        bundle = result.bundle if result.bundle_known else None
        presets = dict(result.snapshots) if result.snapshots else dict(_DEFAULT_PRESETS)
        index = max(0, spec.snapshot_id)
        if self._snapshot is not None:
            index = self._snapshot.snapshot_id
            presets[index] = presets.get(index, self._snapshot.snapshot_name)
        if index not in presets:
            index = min(presets)
        candidate = Pedalboard.from_spec(
            spec, host.plugin_dict, bundle, spec.title or UNTITLED, self._transport, host.customizer, host.patch_parser
        )
        for instance, extra in result.extra_data.items():
            plugin = candidate.find_plugin(instance)
            if plugin is not None and plugin.customization.extra_data is None:
                plugin.customization = replace(plugin.customization, extra_data=extra)
        current = host.current_board()
        in_place = current is not None and current.bundle == candidate.bundle and same_structure(current, candidate)
        sync_blend = self._sync_blend(spec, bundle)
        self._state = SyncState.IDLE
        self.applied += 1
        if result.boards is not None:
            host.update_board_list(result.boards)
        if in_place:
            host.reconcile_board(candidate, presets, index)
        else:
            host.install_board(candidate, presets, index, sync_blend=sync_blend)

    def _sync_blend(self, spec: BoardSpec, bundle: str | None) -> bool:
        return bundle is not None and (self._kind is WindowKind.LOAD or not spec.modified)

    def _apply_rebase(self, result: BoardResolved) -> None:
        if result.boards is not None:
            self._host.update_board_list(result.boards)
        if not result.bundle_known:
            return
        presets = dict(result.snapshots) if result.snapshots else None
        self._host.rebase_board(result.bundle, presets)
