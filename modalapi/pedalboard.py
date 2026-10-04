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

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Optional


from common.parameter import BYPASS_SYMBOL, TTL_INTEGER, MidiCC, Parameter, PortInfo, Ranges, Symbol, json_default
import modalapi.plugin as Plugin
from modalapi.board_spec import TRANSPORT_INSTANCE_ID, BoardSpec, MidiMapSpec
from modalapi.connections import Connection, build_connection
from modalapi.plugin_customization import Customizer, PatchParser, default_customizer
from modalapi.ws_protocol import TransportMessage


# mod-ui addresses pedalboard-level transport controls through a pseudo-instance
# "/pedalboard" (id 9995) with virtual port symbols :bpm, :bpb, :rolling. We
# mirror it as a synthetic Plugin so the binding/label machinery treats them
# like effect params.
BPM_SYMBOL = Symbol(":bpm")
BPB_SYMBOL = Symbol(":bpb")
ROLLING_SYMBOL = Symbol(":rolling")

# mod-ui's defaults when the TTL carries no ranges (utils_lilv.cpp).
_BPM_RANGE = (20.0, 280.0)
_BPB_RANGE = (1.0, 16.0)


def _bypass_info() -> PortInfo:
    # mod-ui reports bypass as a bool alongside the ports, not as a port row.
    return PortInfo(shortName="bypass", symbol=BYPASS_SYMBOL, ranges={"minimum": 0.0, "maximum": 1.0})


def _transport_port_info(symbol: Symbol) -> PortInfo:
    # The three virtual ports on mod-ui's /pedalboard pseudo-instance. :rolling
    # is a toggle whose value names the state — render via the enum path so the
    # footswitch label and parameter dialog read "Playing"/"Stopped".
    if symbol == BPM_SYMBOL:
        return PortInfo(
            shortName="Tempo",
            symbol=":bpm",
            ranges={"minimum": _BPM_RANGE[0], "maximum": _BPM_RANGE[1]},
            units={"symbol": "BPM", "label": "beats per minute"},
            properties=[TTL_INTEGER],
        )
    if symbol == BPB_SYMBOL:
        return PortInfo(
            shortName="BPB",
            symbol=":bpb",
            ranges={"minimum": _BPB_RANGE[0], "maximum": _BPB_RANGE[1]},
            properties=["integer"],
        )
    # :rolling — render via the enum path so its label names the state
    # ("Playing"/"Stopped") rather than the generic On/Off a TOGGLED param shows.
    return PortInfo(
        shortName="Transport",
        symbol=":rolling",
        ranges={"minimum": 0.0, "maximum": 1.0},
        properties=["enumeration"],
        scalePoints=[
            {"label": "Stopped", "value": 0.0},
            {"label": "Playing", "value": 1.0},
        ],
    )


def _control_inputs(plugin_info: dict | None) -> list[PortInfo] | None:
    """None means the LV2 metadata is absent, distinct from a plugin that
    genuinely exposes no control inputs."""
    try:
        return (plugin_info or {})["ports"]["control"]["input"]
    except (KeyError, TypeError):
        return None


def _port_default(pp: PortInfo) -> float:
    # A board load streams only values that differ from the plugin's default.
    ranges = pp.get("ranges") or Ranges()
    return float(ranges.get("default", ranges.get("minimum", 0.0)))


def _time_info(transport: TransportMessage | None, midi: Mapping[Symbol, MidiMapSpec]) -> dict:
    """The timeInfo block rebuilt from the stream: values from the transport
    broadcast, bindings from the /pedalboard MIDI maps."""
    time_info: dict = {}
    if transport is not None:
        time_info.update(bpb=transport.beats_per_bar, bpm=transport.bpm, rolling=transport.rolling)
    for key, symbol in (("bpbCC", BPB_SYMBOL), ("bpmCC", BPM_SYMBOL), ("rollingCC", ROLLING_SYMBOL)):
        mapping = midi.get(symbol)
        if mapping is not None:
            time_info[key] = MidiCC(channel=mapping.channel, control=mapping.controller)
    return time_info


class Pedalboard:
    def __init__(self, title, bundle, customizer: Customizer | None = None):
        self.title = title
        self.bundle = bundle
        # Resolver injected by the composition root (handler); defaults to a
        # no-op so headless/v1 construction degrades to standard behaviour
        # instead of silently depending on plugin-package import order.
        self._customizer: Customizer = customizer or default_customizer
        self.plugins = []
        self.connections: list[Connection] = []
        # Synthetic /pedalboard pseudo-instance carrying :bpm/:bpb/:rolling.
        # Excluded from self.plugins so the effect-graph render never paints it.
        self.transport_plugin: Plugin.Plugin = self._build_transport_plugin(None)

    @staticmethod
    def _binding(cc: MidiCC | None) -> Optional[str]:
        # channel -1 is mod-ui's "unmapped" sentinel (utils_lilv.cpp).
        if not cc or cc.get("channel", -1) < 0:
            return None
        return "%d:%d" % (cc["channel"], cc["control"])

    @classmethod
    def from_spec(
        cls,
        spec: BoardSpec,
        plugin_dict: Mapping[str, dict],
        bundle: str | None,
        title: str,
        transport: TransportMessage | None,
        customizer: Customizer,
        patch_parser: PatchParser,
    ) -> "Pedalboard":
        """A board from mod-ui's stream. Pure: metadata comes from `plugin_dict`,
        extra data from the streamed patch values, nothing from disk or REST."""
        pb = cls(title, bundle, customizer=customizer)
        all_plugins: list[Plugin.Plugin] = []
        instance_to_info: dict[str, Optional[dict]] = {}

        for ps in spec.plugins.values():
            plugin_info = plugin_dict.get(ps.uri)

            category = None
            cat = (plugin_info or {}).get("category")
            if cat is not None and len(cat) > 0:
                category = cat[0]

            bypass_map = ps.midi.get(BYPASS_SYMBOL)
            parameters: dict[Symbol, Parameter] = {
                BYPASS_SYMBOL: Parameter(
                    _bypass_info(),
                    1.0 if ps.bypassed else 0.0,
                    bypass_map.binding if bypass_map else None,
                    ps.instance,
                )
            }

            plugin_params = _control_inputs(plugin_info)
            if plugin_params is None:
                logging.warning("plugin port info not found, could be missing LV2 for: %s", ps.instance)
                plugin_params = []

            for pp in plugin_params:
                symbol = Symbol(pp["symbol"])
                mapping = ps.midi.get(symbol)
                parameters[symbol] = Parameter(
                    pp,
                    ps.values.get(symbol, _port_default(pp)),
                    mapping.binding if mapping else None,
                    ps.instance,
                    binding_range=mapping.binding_range if mapping else None,
                )

            customization = customizer(ps.uri)
            for param_uri, value in ps.patches.items():
                extra = patch_parser(ps.uri, param_uri, value)
                if extra is not None:
                    customization = replace(customization, extra_data=extra)

            inst = Plugin.Plugin(
                ps.instance,
                parameters,
                plugin_info,
                category,
                uri=ps.uri,
                customization=customization,
                instance_number=ps.instance_number,
            )
            inst.canvas_x = ps.x
            inst.canvas_y = ps.y
            inst.pedalboard_snapshot = {sym: float(p.value) for sym, p in parameters.items()}
            instance_to_info[ps.instance] = plugin_info
            all_plugins.append(inst)

        pb.plugins = sorted(all_plugins, key=lambda p: (p.canvas_x, p.canvas_y, p.instance_id))
        pb.connections = [
            build_connection(src.removeprefix("/graph/"), dst.removeprefix("/graph/"), "", instance_to_info)
            for src, dst in spec.connections
        ]
        pb.transport_plugin = pb._build_transport_plugin(_time_info(transport, spec.transport_midi))
        return pb

    @classmethod
    def empty(cls, customizer: Customizer | None = None) -> "Pedalboard":
        """The board mod-ui holds after a reset: untitled, unsaved, no plugins."""
        pb = cls("", None, customizer=customizer)
        return pb

    def _build_transport_plugin(self, time_info: dict | None) -> Plugin.Plugin:
        """The /pedalboard pseudo-instance carrying :bpm/:bpb/:rolling. Built
        from mod-ui's timeInfo block, or from mod-ui's own defaults when the
        board carries none."""
        # timeInfo's `available` mask says which ports mod-ui has *addressed*,
        # not which exist — transport is global and all three are always
        # settable. Built unconditionally so the type stays non-Optional; a
        # board that never addressed them just has unbound parameters.
        time_info = time_info or {}

        parameters: dict[Symbol, Parameter] = {
            BPB_SYMBOL: Parameter(
                _transport_port_info(BPB_SYMBOL),
                float(time_info.get("bpb", 4.0)),
                self._binding(time_info.get("bpbCC")),
                TRANSPORT_INSTANCE_ID,
            ),
            BPM_SYMBOL: Parameter(
                _transport_port_info(BPM_SYMBOL),
                float(time_info.get("bpm", 120.0)),
                self._binding(time_info.get("bpmCC")),
                TRANSPORT_INSTANCE_ID,
            ),
            ROLLING_SYMBOL: Parameter(
                _transport_port_info(ROLLING_SYMBOL),
                1.0 if time_info.get("rolling") else 0.0,
                self._binding(time_info.get("rollingCC")),
                TRANSPORT_INSTANCE_ID,
            ),
        }

        # category drives footswitch color; "Utility" is the benign choice for
        # transport — no LV2 category exists for it. uri=urn:mod:pedalboard so
        # the customizer resolves the transport labeling hook.
        return Plugin.Plugin(
            TRANSPORT_INSTANCE_ID,
            parameters,
            None,
            category="Utility",
            uri="urn:mod:pedalboard",
            customization=self._customizer("urn:mod:pedalboard"),
        )

    def find_plugin(self, instance_id: str) -> Plugin.Plugin | None:
        """Lookup by bare instance id, including the transport pseudo-plugin.
        Returns None for unknown instances."""
        for p in self.plugins:
            if p.instance_id == instance_id:
                return p
        if self.transport_plugin.instance_id == instance_id:
            return self.transport_plugin
        return None

    def _build_plugin(self, instance_id: str, uri: str, x: float, y: float, info: dict) -> Plugin.Plugin:
        """Build a Plugin from REST metadata (no LILV). Used for dynamic adds.

        Parameters start at REST defaults; bypass is set false. MIDI bindings
        arrive later via midi_map WS messages; values arrive via param_set.
        Empty info (metadata unfetchable) yields a bypass-only plugin, as from_spec does for a missing LV2.
        """
        category = None
        cat = info.get("category")
        if cat and len(cat) > 0:
            category = cat[0]

        parameters: dict[Symbol, Parameter] = {}
        parameters[BYPASS_SYMBOL] = Parameter(_bypass_info(), 0.0, None, instance_id)

        for pp in _control_inputs(info) or []:
            sym = pp.get("symbol")
            if not sym:
                continue
            default_val = (pp.get("ranges") or {}).get("default")
            parameters[Symbol(sym)] = Parameter(
                pp, float(default_val) if default_val is not None else 0.0, None, instance_id
            )

        # TODO: extra_data can't be populated here — mod-ui's `add` WS message
        # doesn't include the numeric `pedal:instanceNumber`, so we can't address
        # effect-N/effect.ttl. A protocol change (include the instance number
        # on the wire) would let us pass the bundle + number to the customizer.
        inst = Plugin.Plugin(instance_id, parameters, info or None, category, uri=uri, customization=self._customizer(uri))
        inst.canvas_x = x
        inst.canvas_y = y
        return inst

    def add_connection(self, port_from: str, port_to: str) -> None:
        """Add a connection from live WS port paths (e.g. /graph/A/out → /graph/B/in)."""
        instance_to_info = {p.instance_id: p.info for p in self.plugins}
        tail = port_from.removeprefix("/graph/")
        head = port_to.removeprefix("/graph/")
        try:
            conn = build_connection(tail, head, "", instance_to_info)
            if conn not in self.connections:
                self.connections.append(conn)
        except Exception as e:
            logging.warning("Failed to add connection %s -> %s: %s", port_from, port_to, e)

    def remove_connection(self, port_from: str, port_to: str) -> None:
        """Remove a connection matching live WS port paths."""
        tail = port_from.removeprefix("/graph/")
        head = port_to.removeprefix("/graph/")
        src_id, src_sym = tail.split("/", 1) if "/" in tail else (tail, "")
        dst_id, dst_sym = head.split("/", 1) if "/" in head else (head, "")
        self.connections = [
            c
            for c in self.connections
            if not (
                c.src.id == src_id
                and c.src.port_symbol == src_sym
                and c.dst.id == dst_id
                and c.dst.port_symbol == dst_sym
            )
        ]

    def to_json(self):
        return json.dumps(self, default=json_default, sort_keys=True, indent=4)
