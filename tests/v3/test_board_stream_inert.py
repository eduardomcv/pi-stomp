"""Outside a window, a lone connect marker only arms the next window: the board, the
screen and the outbound queue stay as they were. `remove :all` outside a window changes
the board; `plugin_pos` is covered by test_plugin_pos."""

from modalapi.board_sync import UNTITLED, SyncState
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

    ws_bridge.inject(CONNECTED_MARKER)
    handler.poll_ws_messages()

    assert handler._board_sync.state is SyncState.IDLE
    assert handler.current.pedalboard.plugins == [drive, delay]
    assert handler.current.pedalboard.connections == [_edge("drive", "delay")]
    assert not handler._is_pedalboard_loading
    assert ws_bridge.sent == []
    handler.lcd.draw_main_panel()
    snapshot("board")


def test_v3_remove_all_outside_a_window_empties_the_board_in_place(v3_system: SystemFixture, make_plugin, monkeypatch):
    handler = v3_system.handler
    board = handler.current.pedalboard
    board.plugins = [make_plugin("drive", category="Distortion")]
    board.connections = [_edge("drive", "delay")]
    reinits = []
    monkeypatch.setattr(handler.hardware, "reinit", reinits.append)

    v3_system.ws_bridge.inject("remove :all")
    handler.poll_ws_messages()

    assert handler.current.pedalboard is board
    assert (board.plugins, board.connections) == ([], [])
    assert (board.title, board.bundle) == (UNTITLED, None)
    assert reinits == []
    assert v3_system.ws_bridge.sent == []
