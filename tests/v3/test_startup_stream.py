from tests.board_window import serve_board
from tests.replay_helpers import load_plugin_info, load_replay


def test_await_initial_board_applies_the_first_stream(v3_system):
    system = v3_system
    handler = system.handler
    handler.plugin_dict.update(load_plugin_info())
    serve_board(system, bundle="/path/to/rig.pedalboard", snapshots={"0": "Clean"})
    for line in load_replay("connect_dump_saved.txt"):
        system.ws_bridge.inject(line)
    handler._current = None  # as at process start, before any board is installed
    handler.await_initial_board(timeout_s=1.0, sleep=lambda s: None)
    assert handler.current.pedalboard.title == "Fixture Rig"
    assert handler._board_sync.applied == 1


def test_await_initial_board_falls_back_to_an_empty_board_when_nothing_streams(v3_system):
    handler = v3_system.handler
    handler._current = None
    ticks = iter([0.0, 0.0, 6.0, 6.0, 6.0])
    handler.await_initial_board(timeout_s=5.0, sleep=lambda s: None, clock=lambda: next(ticks))
    assert handler.current.pedalboard.plugins == []
    assert handler.current.pedalboard.title == ""  # Pedalboard.empty(); a stream would say Untitled


def test_the_empty_fallback_goes_through_install_board(v3_system):
    handler = v3_system.handler
    handler._current = None
    handler.await_initial_board(timeout_s=0.0, sleep=lambda s: None)
    assert handler.lcd.current is handler.current


def test_a_stream_arriving_after_the_fallback_is_applied_normally(v3_system):
    system = v3_system
    handler = system.handler
    handler.plugin_dict.update(load_plugin_info())
    serve_board(system, bundle="/path/to/rig.pedalboard")
    handler._current = None
    handler.await_initial_board(timeout_s=0.0, sleep=lambda s: None)
    assert handler.current.pedalboard.plugins == []
    for line in load_replay("connect_dump_saved.txt"):
        system.ws_bridge.inject(line)
    handler.poll_ws_messages()
    handler.poll_ws_messages()
    assert [p.instance_id for p in handler.current.pedalboard.plugins] != []


def test_an_installed_board_is_kept_when_nothing_streams(v3_system):
    handler = v3_system.handler
    board = handler.current.pedalboard
    handler.await_initial_board(timeout_s=0.0, sleep=lambda s: None)
    assert handler.current.pedalboard is board



def test_a_failed_first_build_falls_through_to_the_empty_board(v3_system, monkeypatch):
    system = v3_system
    handler = system.handler
    handler.plugin_dict.update(load_plugin_info())
    serve_board(system, bundle="/path/to/rig.pedalboard")

    def explode(*args, **kwargs):
        raise RuntimeError("build failed")

    monkeypatch.setattr("modalapi.board_sync.Pedalboard.from_spec", explode)
    for line in load_replay("connect_dump_saved.txt"):
        system.ws_bridge.inject(line)
    handler._current = None
    ticks = iter([0.0, 0.0, 2.0, 2.0])
    handler.await_initial_board(timeout_s=1.0, sleep=lambda s: None, clock=lambda: next(ticks))
    assert handler._board_sync.applied == 0
    assert handler.current.pedalboard.title == ""
