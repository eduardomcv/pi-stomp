"""Integration tests for the pistomp-stamp stamping protocol — v3 (Modhandler).

v3 stamps when a board window swaps in a different bundle, or when a ``last.json``
refresh (Save-As) rebases the current board onto one.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

from modalapi.pedalboard import Pedalboard
from modalapi.ws_protocol import CONNECTED_MARKER
from tests.board_window import play_window, serve_board
from tests.replay_helpers import board_to_replay_lines
from tests.types import SystemFixture

NEW = "/path/to/new.pedalboard"


def _stamp_calls(mock_run):
    return [c for c in mock_run.call_args_list if c.args and c.args[0][:2] == ["pistomp-stamp", "stamp"]]


def _assert_stamp_called(mock_run, times: int = 1):
    calls = _stamp_calls(mock_run)
    assert len(calls) == times, (
        f"Expected {times} pistomp-stamp call(s), got {len(calls)}. "
        f"All subprocess.Popen calls: {mock_run.call_args_list}"
    )


def _assert_stamp_not_called(mock_run):
    calls = _stamp_calls(mock_run)
    assert not calls, f"Unexpected pistomp-stamp call(s): {calls}"


def _touch_last_json(handler, bundle: str) -> None:
    last_json = Path(handler.data_dir) / "last.json"
    last_json.write_text(json.dumps({"pedalboard": bundle}))
    os.utime(last_json, (9999, 9999))


def _load(system: SystemFixture, board: Pedalboard) -> None:
    """A load as mod-ui streams it: a reset (`remove :all`) first, then the board's window."""
    serve_board(system, bundle=board.bundle)
    play_window(system, ["remove :all", *board_to_replay_lines(board, False, False, 0)])


def _as_at_process_start(handler) -> None:
    handler._current = None
    handler._last_bundle = None


class TestStampOnPedalboardChange:
    """pistomp-stamp stamp must be called when mod-ui's stream or a last.json
    refresh moves pi-stomp onto a different bundle."""

    def test_stamp_called_on_modui_change(self, v3_system: SystemFixture):
        handler = v3_system.handler
        _touch_last_json(handler, NEW)

        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            _load(v3_system, Pedalboard("New Rig", NEW))
            handler.poll_modui_changes()
            handler.poll_modui_changes()

        assert handler.current.pedalboard.bundle == NEW
        _assert_stamp_called(mock_run, times=1)

    def test_stamp_called_on_save_as_rebase(self, v3_system: SystemFixture):
        handler = v3_system.handler
        serve_board(v3_system, bundle=NEW)
        _touch_last_json(handler, NEW)

        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            handler.poll_modui_changes()
            handler.poll_modui_changes()

        assert handler.current.pedalboard.bundle == NEW
        _assert_stamp_called(mock_run, times=1)

    def test_a_load_back_and_forth_stamps_each_change_once(self, v3_system: SystemFixture):
        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            _load(v3_system, Pedalboard("New Rig", NEW))
            _load(v3_system, Pedalboard("New Rig", NEW))
            _load(v3_system, Pedalboard("Integration Rig", "/path/to/rig.pedalboard"))
        assert [c.args[0][2] for c in _stamp_calls(mock_run)] == [NEW, "/path/to/rig.pedalboard"]

    def test_the_first_bundle_after_an_empty_start_is_a_baseline(self, v3_system: SystemFixture):
        handler = v3_system.handler
        _as_at_process_start(handler)
        handler.await_initial_board(timeout_s=0.0, sleep=lambda s: None)
        assert handler.current.pedalboard.bundle is None

        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            _load(v3_system, Pedalboard("New Rig", NEW))
            _assert_stamp_not_called(mock_run)
            _load(v3_system, Pedalboard("Integration Rig", "/path/to/rig.pedalboard"))
        _assert_stamp_called(mock_run, times=1)

    def test_no_stamp_on_reconnect_with_unsaved_edits(self, v3_system: SystemFixture):
        """A replayed board mod-ui holds modified is not a known-good bundle."""
        serve_board(v3_system, bundle=NEW)
        lines = [CONNECTED_MARKER, *board_to_replay_lines(Pedalboard("New Rig", NEW), False, True, 0)]

        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            play_window(v3_system, lines)

        assert v3_system.handler.current.pedalboard.bundle == NEW
        _assert_stamp_not_called(mock_run)

    def test_no_stamp_without_change(self, v3_system: SystemFixture):
        """poll_modui_changes() must NOT stamp when last.json hasn't changed."""
        handler = v3_system.handler

        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            handler.poll_modui_changes()

        _assert_stamp_not_called(mock_run)

    def test_no_stamp_on_same_pedalboard(self, v3_system: SystemFixture):
        """Neither a reload of the bundle already loaded (reset, then its window)
        nor a last.json refresh naming it may stamp."""
        handler = v3_system.handler
        rig = Pedalboard("Integration Rig", "/path/to/rig.pedalboard")
        _touch_last_json(handler, "/path/to/rig.pedalboard")

        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            _load(v3_system, rig)
            handler.poll_modui_changes()
            handler.poll_modui_changes()

        _assert_stamp_not_called(mock_run)


class TestStampOnSetCurrentPedalboard:
    """set_current_pedalboard stamps only when its bundle differs from the last
    bundle shown; the first bundle at startup is the baseline."""

    def test_no_stamp_on_the_same_bundle(self, v3_system: SystemFixture):
        handler = v3_system.handler
        pb = handler.pedalboards["/path/to/rig.pedalboard"]
        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            handler.set_current_pedalboard(pb)
        _assert_stamp_not_called(mock_run)

    def test_no_stamp_at_startup(self, v3_system: SystemFixture):
        handler = v3_system.handler
        _as_at_process_start(handler)
        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            handler.set_current_pedalboard(handler.pedalboards[NEW])
        _assert_stamp_not_called(mock_run)

    def test_stamp_on_a_different_bundle_after_startup(self, v3_system: SystemFixture):
        handler = v3_system.handler
        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            handler.set_current_pedalboard(handler.pedalboards[NEW])
        _assert_stamp_called(mock_run, times=1)


class TestStampNotCalledOnNonChangeOperations:
    """Operations that don't change the pedalboard must not trigger a stamp."""

    def test_no_stamp_on_load_pedalboards(self, v3_system: SystemFixture):
        handler = v3_system.handler
        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            handler.load_pedalboards()
        _assert_stamp_not_called(mock_run)

    def test_no_stamp_on_preset_change(self, v3_system: SystemFixture):
        handler = v3_system.handler
        with patch("modalapi.modhandler.subprocess.Popen") as mock_run:
            handler.preset_change(0)
        _assert_stamp_not_called(mock_run)

    def test_no_stamp_on_system_info_load(self, v3_system: SystemFixture):
        handler = v3_system.handler
        with (
            patch("modalapi.modhandler.subprocess.run"),
            patch("modalapi.modhandler.subprocess.Popen") as mock_popen,
        ):
            handler.system_info_load()
        _assert_stamp_not_called(mock_popen)
