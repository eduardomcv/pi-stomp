from modalapi.board_sync import UNTITLED
from tests.board_window import serve_board
from tests.fake_board_fetcher import FakeBoardFetcher
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
    assert handler.current.pedalboard.title == UNTITLED


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
    assert handler.current.pedalboard.title == UNTITLED


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _count_installs(handler, monkeypatch) -> list[object]:
    installed: list[object] = []
    original = handler.install_board

    def spy(board, *args, **kwargs):
        installed.append(board)
        original(board, *args, **kwargs)

    monkeypatch.setattr(handler, "install_board", spy)
    return installed


def test_a_stream_in_flight_at_the_deadline_is_waited_for(v3_system, monkeypatch):
    system = v3_system
    handler = system.handler
    handler.plugin_dict.update(load_plugin_info())
    serve_board(system, bundle="/path/to/rig.pedalboard", snapshots={"0": "Clean"})
    fetcher = handler.board_fetcher
    assert isinstance(fetcher, FakeBoardFetcher)
    fetcher.hold = True
    for line in load_replay("connect_dump_saved.txt"):
        system.ws_bridge.inject(line)
    handler._current = None
    installed = _count_installs(handler, monkeypatch)
    clock = _Clock()
    sleeps = 0

    def sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 1:
            clock.now = 2.0
        else:
            fetcher.release()

    handler.await_initial_board(timeout_s=1.0, sleep=sleep, clock=clock)
    assert len(installed) == 1
    assert handler.current.pedalboard.title == "Fixture Rig"


def test_a_window_that_never_resolves_hits_the_hard_cap(v3_system, monkeypatch):
    system = v3_system
    handler = system.handler
    handler.plugin_dict.update(load_plugin_info())
    fetcher = handler.board_fetcher
    assert isinstance(fetcher, FakeBoardFetcher)
    fetcher.hold = True
    for line in load_replay("connect_dump_saved.txt"):
        system.ws_bridge.inject(line)
    handler._current = None
    installed = _count_installs(handler, monkeypatch)
    clock = _Clock()

    def sleep(_: float) -> None:
        clock.now += 30.0

    handler.await_initial_board(timeout_s=1.0, sleep=sleep, clock=clock)
    assert clock.now > 61.0
    assert len(installed) == 1
    assert handler.current.pedalboard.plugins == []


def test_the_audio_midi_tile_reflects_a_transport_that_precedes_the_first_board(v3_system, monkeypatch):
    system = v3_system
    handler = system.handler
    monkeypatch.setattr(handler.jack_mute, "is_muted", lambda: False)
    lcd = handler.lcd
    lcd.w_eq = None
    lcd.w_wifi = None  # as at process start, before draw_tools has run
    handler._current = None
    system.ws_bridge.inject("transport 1 4.0 120.0 none")
    for line in load_replay("connect_dump_saved.txt")[3:]:
        system.ws_bridge.inject(line)
    handler.await_initial_board(timeout_s=1.0, sleep=lambda s: None)
    assert handler.transport_rolling is True
    assert lcd.w_eq is not None
    assert lcd.w_eq.image.get_at((15, 5))[3] > 0  # rolling glyph's play-triangle apex
