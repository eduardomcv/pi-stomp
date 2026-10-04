"""End-to-end bootstrap of the emulator handler + hardware + window.

Catches wiring regressions in bootstrap_emulator (add_lcd, add_hardware,
set_window, load_banks, load_pedalboards, await_initial_board,
system_info_load) without requiring MOD Desktop or a real MIDI device."""

from pathlib import Path

import pytest

from emulator.bootstrap import bootstrap_emulator

PROJECT_ROOT = str(Path(__file__).parent.parent.parent)


@pytest.mark.parametrize("version", ["emulator_v2", "emulator_v3"])
def test_bootstrap_wires_handler_hardware_and_window(emulator_env, version):
    handler, midiout = bootstrap_emulator(version, PROJECT_ROOT, initial_board_timeout_s=0.0)

    assert midiout is None  # forced to fail in the fixture
    assert handler.hardware is not None
    assert handler.lcd is not None
    assert handler._window is not None  # pyright: ignore[reportAttributeAccessIssue]
    assert len(handler.hardware.footswitches) >= 1

    handler.hardware.cleanup()


def test_bootstrap_shows_the_board_mod_ui_streams(emulator_env):
    lines = iter([["loading_start 0 0", "loading_end 0 Emu Rig"]])
    emulator_env["bridge"].get_received_messages.side_effect = lambda: next(lines, [])

    handler, _ = bootstrap_emulator("emulator_v3", PROJECT_ROOT, initial_board_timeout_s=2.0)
    handler.board_fetcher.cleanup()

    assert handler.current is not None  # pyright: ignore[reportAttributeAccessIssue]
    assert handler.current.pedalboard.title == "Emu Rig"  # pyright: ignore[reportAttributeAccessIssue]

    assert handler.hardware is not None
    handler.hardware.cleanup()


def test_bootstrap_starts_empty_when_nothing_streams(emulator_env):
    handler, _ = bootstrap_emulator("emulator_v3", PROJECT_ROOT, initial_board_timeout_s=0.05)

    assert handler.current is not None  # pyright: ignore[reportAttributeAccessIssue]
    assert handler.current.pedalboard.plugins == []  # pyright: ignore[reportAttributeAccessIssue]
    assert handler.current.pedalboard.title == ""  # pyright: ignore[reportAttributeAccessIssue]
    assert not (emulator_env["tmp_path"] / ".pistomp_emulator" / "last.json").exists()

    assert handler.hardware is not None
    handler.hardware.cleanup()
