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

"""Live `add` messages whose plugin metadata is still being fetched.

Until the metadata lands there is no Plugin to apply later messages to, so each one
for that instance is parked here and replayed, in arrival order, once the plugin
exists. A `remove` cancels the add instead.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from modalapi.ws_protocol import (
    AddPluginMessage,
    ConnectMessage,
    DisconnectMessage,
    MidiMapMessage,
    ParamSetMessage,
    PatchSetMessage,
    PluginBypassMessage,
    RemovePluginMessage,
    WebSocketMessage,
)


def _port_instance(port: str) -> str:
    return port.removeprefix("/graph/").split("/", 1)[0]


def instances_of(msg: WebSocketMessage) -> tuple[str, ...]:
    if isinstance(msg, (ParamSetMessage, PluginBypassMessage, MidiMapMessage, PatchSetMessage, RemovePluginMessage)):
        return (msg.instance,)
    if isinstance(msg, (ConnectMessage, DisconnectMessage)):
        return (_port_instance(msg.port_from), _port_instance(msg.port_to))
    return ()


@dataclass
class PendingAdd:
    add: AddPluginMessage
    buffered: list[WebSocketMessage] = field(default_factory=list)


class PendingAdds:
    def __init__(self) -> None:
        self._by_instance: dict[str, PendingAdd] = {}

    def __bool__(self) -> bool:
        return bool(self._by_instance)

    def start(self, add: AddPluginMessage) -> bool:
        """True when the caller must request `add.uri`; False when a fetch for it is
        already under way."""
        existing = self._by_instance.get(add.instance)
        if existing is not None and existing.add.uri == add.uri:
            existing.add = add
            return False
        awaited = any(p.add.uri == add.uri for p in self._by_instance.values())
        self._by_instance[add.instance] = PendingAdd(add)
        return not awaited

    def defer(self, msg: WebSocketMessage) -> bool:
        """True when `msg` concerned a pending add and was parked (or, for a remove,
        cancelled it). Anything else is the caller's to handle."""
        touched = [self._by_instance[i] for i in instances_of(msg) if i in self._by_instance]
        if not touched:
            return False
        if isinstance(msg, RemovePluginMessage):
            self._cancel(msg.instance)
            return True
        for pending in touched:
            pending.buffered.append(msg)
        return True

    def resolve(self, uris: Collection[str]) -> list[PendingAdd]:
        """Pop every pending add whose URI is in `uris`. A message parked on several
        pending adds is handed only to the last of them to resolve, so replay finds
        every plugin it names."""
        done = [p for p in self._by_instance.values() if p.add.uri in uris]
        for pending in done:
            del self._by_instance[pending.add.instance]
        held = {id(m) for p in self._by_instance.values() for m in p.buffered}
        replayed: set[int] = set()
        for pending in done:
            fresh = [m for m in pending.buffered if id(m) not in held and id(m) not in replayed]
            replayed.update(id(m) for m in fresh)
            pending.buffered = fresh
        return done

    def clear(self) -> None:
        self._by_instance.clear()

    def _cancel(self, instance: str) -> None:
        del self._by_instance[instance]
        for pending in self._by_instance.values():
            pending.buffered = [m for m in pending.buffered if instance not in instances_of(m)]
