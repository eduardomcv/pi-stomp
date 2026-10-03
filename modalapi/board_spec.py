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

"""The board mod-ui's WebSocket stream describes: frozen specs, and the pure
accumulator that folds the stream into one."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from common.parameter import Symbol
from modalapi.ws_protocol import (
    AddPluginMessage,
    ConnectMessage,
    DisconnectMessage,
    LoadingEndMessage,
    LoadingStartMessage,
    RemovePluginMessage,
    ResetMessage,
    WebSocketMessage,
)


@dataclass(frozen=True)
class MidiMapSpec:
    """A MIDI-CC addressing as `midi_map` reports it. mod-ui always sends a range:
    the custom sub-range, else the port's LV2 range."""

    channel: int
    controller: int
    minimum: float
    maximum: float

    @property
    def binding(self) -> str:
        return "%d:%d" % (self.channel, self.controller)

    @property
    def binding_range(self) -> tuple[float, float] | None:
        return (self.minimum, self.maximum) if self.maximum > self.minimum else None


@dataclass(frozen=True)
class PluginSpec:
    """One plugin instance as the stream describes it. `values` holds only what was
    streamed: a board load omits ports still at their default."""

    instance: str
    uri: str
    x: float
    y: float
    bypassed: bool
    instance_number: int | None
    values: Mapping[Symbol, float]
    midi: Mapping[Symbol, MidiMapSpec]
    patches: Mapping[str, str]


@dataclass(frozen=True)
class BoardSpec:
    """The board one replay window describes, with the window's flags. `connections`
    are wire port paths (`/graph/drive/out`, `/graph/capture_1`)."""

    plugins: Mapping[str, PluginSpec]
    connections: tuple[tuple[str, str], ...]
    transport_midi: Mapping[Symbol, MidiMapSpec]
    empty: bool
    modified: bool
    snapshot_id: int
    title: str


@dataclass
class _PluginDraft:
    uri: str
    x: float
    y: float
    bypassed: bool
    instance_number: int | None
    values: dict[Symbol, float] = field(default_factory=dict)
    midi: dict[Symbol, MidiMapSpec] = field(default_factory=dict)
    patches: dict[str, str] = field(default_factory=dict)

    def freeze(self, instance: str) -> PluginSpec:
        return PluginSpec(
            instance=instance,
            uri=self.uri,
            x=self.x,
            y=self.y,
            bypassed=self.bypassed,
            instance_number=self.instance_number,
            values=MappingProxyType(dict(self.values)),
            midi=MappingProxyType(dict(self.midi)),
            patches=MappingProxyType(dict(self.patches)),
        )


def _instance_of(port: str) -> str | None:
    """The plugin a port path names; None for a hardware port, which has no symbol."""
    instance, sep, _ = port.removeprefix("/graph/").partition("/")
    return instance if sep else None


class BoardBuilder:
    """Folds mod-ui's board-scoped messages into a BoardSpec. Pure: the caller
    decides which messages reach it; the builder only mirrors what they say."""

    def __init__(self) -> None:
        self._plugins: dict[str, _PluginDraft] = {}
        self._connections: dict[tuple[str, str], None] = {}
        self._transport_midi: dict[Symbol, MidiMapSpec] = {}
        self._empty: bool = True
        self._modified: bool = False
        self._snapshot_id: int = 0
        self._title: str = ""

    def start(self, msg: LoadingStartMessage) -> None:
        """A window replays the whole board, so it starts from nothing."""
        self._clear()
        self._empty = msg.empty
        self._modified = msg.modified

    def end(self, msg: LoadingEndMessage) -> None:
        self._snapshot_id = msg.snapshot_id
        self._title = msg.title

    def apply(self, msg: WebSocketMessage) -> None:
        match msg:
            case AddPluginMessage():
                self._plugins[msg.instance] = _PluginDraft(msg.uri, msg.x, msg.y, msg.bypassed, msg.instance_number)
            case RemovePluginMessage():
                self._remove(msg.instance)
            case ResetMessage():
                self._reset()
            case ConnectMessage():
                if self._holds(msg.port_from) and self._holds(msg.port_to):
                    self._connections[(msg.port_from, msg.port_to)] = None
            case DisconnectMessage():
                self._connections.pop((msg.port_from, msg.port_to), None)
            case _:
                pass

    def freeze(self) -> BoardSpec:
        """An immutable copy; later messages do not reach it."""
        return BoardSpec(
            plugins=MappingProxyType({instance: draft.freeze(instance) for instance, draft in self._plugins.items()}),
            connections=tuple(self._connections),
            transport_midi=MappingProxyType(dict(self._transport_midi)),
            empty=self._empty,
            modified=self._modified,
            snapshot_id=self._snapshot_id,
            title=self._title,
        )

    def _holds(self, port: str) -> bool:
        instance = _instance_of(port)
        return instance is None or instance in self._plugins

    def _remove(self, instance: str) -> None:
        self._plugins.pop(instance, None)
        self._connections = {
            pair: None for pair in self._connections if instance not in (_instance_of(pair[0]), _instance_of(pair[1]))
        }

    def _clear(self) -> None:
        self._plugins.clear()
        self._connections.clear()
        self._transport_midi.clear()

    def _reset(self) -> None:
        # Host.reset leaves the empty, unsaved, untitled board at snapshot 0.
        self._clear()
        self._empty, self._modified, self._snapshot_id, self._title = True, False, 0, ""
