"""The board-stream vocabulary PR-A parses is not acted on yet: the connect marker,
`plugin_pos` and `remove :all` leave the board, the screen and the outbound queue as
they were. `remove :all` was already inert: it parsed as an instance named ":all"
that no board holds."""

from modalapi.connections import Connection, Endpoint, EndpointKind
from modalapi.ws_protocol import CONNECTED_MARKER
from tests.types import SystemFixture


def _edge(src: str, dst: str) -> Connection:
    return Connection(
        src=Endpoint(kind=EndpointKind.PLUGIN, id=src, port_symbol="", port_idx=0),
        dst=Endpoint(kind=EndpointKind.PLUGIN, id=dst, port_symbol="", port_idx=0),
    )


def test_v3_board_stream_messages_are_inert(v3_system: SystemFixture, make_plugin, snapshot):
    handler = v3_system.handler
    ws_bridge = v3_system.ws_bridge
    assert handler.current is not None
    drive = make_plugin("drive", category="Distortion")
    delay = make_plugin("delay", category="Delay", bypassed=True)
    handler.current.pedalboard.plugins = [drive, delay]
    handler.current.pedalboard.connections = [_edge("drive", "delay")]
    handler.lcd.link_data(handler.pedalboard_list, handler.current, v3_system.hw.footswitches)
    handler.lcd.draw_main_panel()
    snapshot("board")

    for raw in (CONNECTED_MARKER, "plugin_pos /graph/drive 900 40", "remove :all"):
        ws_bridge.inject(raw)
    handler.poll_ws_messages()

    assert handler.current.pedalboard.plugins == [drive, delay]
    assert handler.current.pedalboard.connections == [_edge("drive", "delay")]
    assert (drive.canvas_x, drive.canvas_y) == (0.0, 0.0)
    assert not handler._is_pedalboard_loading
    assert ws_bridge.sent == []
    handler.lcd.draw_main_panel()
    snapshot("board")
