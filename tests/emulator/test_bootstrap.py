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


def test_the_emulator_never_takes_a_bundle_from_a_stale_last_json_or_a_bogus_body(emulator_env):
    """Nothing writes the emulator's last.json any more, and its mod-ui answered `{}`:
    the board stays unsaved rather than adopting either."""
    emu_dir = emulator_env["tmp_path"] / ".pistomp_emulator"
    emu_dir.mkdir()
    (emu_dir / "last.json").write_text('{"pedalboard": "/stale/Old.pedalboard"}')
    lines = iter([["loading_start 0 0", "loading_end 0 Emu Rig"]])
    emulator_env["bridge"].get_received_messages.side_effect = lambda: next(lines, [])

    handler, _ = bootstrap_emulator("emulator_v3", PROJECT_ROOT, initial_board_timeout_s=2.0)
    handler.board_fetcher.cleanup()

    assert handler.current.pedalboard.title == "Emu Rig"  # pyright: ignore[reportAttributeAccessIssue]
    assert handler.current.pedalboard.bundle is None  # pyright: ignore[reportAttributeAccessIssue]

    assert handler.hardware is not None
    handler.hardware.cleanup()


def test_bootstrap_starts_empty_when_nothing_streams(emulator_env):
    handler, _ = bootstrap_emulator("emulator_v3", PROJECT_ROOT, initial_board_timeout_s=0.05)

    assert handler.current is not None  # pyright: ignore[reportAttributeAccessIssue]
    assert handler.current.pedalboard.plugins == []  # pyright: ignore[reportAttributeAccessIssue]
    assert handler.current.pedalboard.title == "Untitled"  # pyright: ignore[reportAttributeAccessIssue]
    assert not (emulator_env["tmp_path"] / ".pistomp_emulator" / "last.json").exists()

    assert handler.hardware is not None
    handler.hardware.cleanup()


def test_choosing_a_board_leaves_the_install_to_the_stream(emulator_env):
    handler, _ = bootstrap_emulator("emulator_v3", PROJECT_ROOT, initial_board_timeout_s=0.05)
    shown = handler.current.pedalboard  # pyright: ignore[reportAttributeAccessIssue]
    assert handler.pedalboard_list

    handler.pedalboard_change(handler.pedalboard_list[0])

    assert handler.current.pedalboard is shown  # pyright: ignore[reportAttributeAccessIssue]
    assert shown.bundle is None

    assert handler.hardware is not None
    handler.hardware.cleanup()
