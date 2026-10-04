import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from common.parameter import Symbol
from modalapi.board_fetch import BoardJob, BoardResolved
from modalapi.board_sync import UNTITLED, BoardSync, SyncState, same_structure
from modalapi.pedalboard import Pedalboard
from modalapi.plugin import Plugin
from modalapi.plugin_customization import Customizer, PatchParser, PluginExtraData, default_customizer
from modalapi.ws_protocol import parse_message
from tests.replay_helpers import load_plugin_info, load_replay

BUNDLE = "/data/.pedalboards/Rig.pedalboard"


def _patch_none(uri: str | None, param_uri: str, value: str) -> PluginExtraData | None:
    return None


class FakeHost:
    def __init__(self, board: Pedalboard | None = None) -> None:
        self.plugin_dict: dict[str, dict] = {}
        self.customizer: Customizer = default_customizer
        self.patch_parser: PatchParser = _patch_none
        self.board = board
        self.bundles: frozenset[str] = frozenset()
        self.calls: list[tuple] = []

    @property
    def installed(self) -> Pedalboard:
        assert self.board is not None
        return self.board

    def plugin(self, instance: str) -> Plugin:
        found = self.installed.find_plugin(instance)
        assert found is not None
        return found

    def current_board(self) -> Pedalboard | None:
        return self.board

    def known_bundles(self) -> frozenset[str]:
        return self.bundles

    def request_board(self, job: BoardJob) -> None:
        self.calls.append(("request", job))

    def show_loading(self) -> None:
        self.calls.append(("loading",))

    def abort_window(self) -> None:
        self.calls.append(("abort",))

    def install_board(self, board: Pedalboard, presets: dict[int, str], preset_index: int, *, sync_blend: bool) -> None:
        self.board = board
        self.calls.append(("install", board.bundle, dict(presets), preset_index, sync_blend))

    def reconcile_board(self, candidate: Pedalboard, presets: dict[int, str], preset_index: int) -> None:
        self.calls.append(("reconcile", candidate.bundle, dict(presets), preset_index))

    def clear_board(self) -> None:
        self.calls.append(("clear",))

    def rebase_board(self, bundle: str | None, presets: dict[int, str] | None) -> None:
        self.calls.append(("rebase", bundle, presets))

    def update_board_list(self, boards: tuple[tuple[str, str], ...]) -> None:
        self.calls.append(("boards", boards))

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _feed(sync: BoardSync, lines: list[str]) -> list[bool]:
    return [sync.feed(parse_message(line)) for line in lines]


def _resolved(
    ticket: int,
    *,
    bundle: str | None = BUNDLE,
    bundle_known: bool = True,
    snapshots: Mapping[int, str] | None = None,
    boards: tuple[tuple[str, str], ...] | None = None,
    extra_data: Mapping[str, PluginExtraData] | None = None,
) -> BoardResolved:
    return BoardResolved(
        ticket=ticket,
        metadata=load_plugin_info(),
        bundle=bundle,
        bundle_known=bundle_known,
        snapshots={0: "Clean", 1: "Lead"} if snapshots is None else snapshots,
        boards=boards,
        extra_data={} if extra_data is None else extra_data,
    )


def _window(sync: BoardSync, host: FakeHost, name: str = "connect_dump_saved.txt", *, replay: bool = False) -> BoardJob:
    if replay:
        sync.feed(parse_message(":connected"))
    _feed(sync, load_replay(name))
    return [c[1] for c in host.calls if c[0] == "request"][-1]


def test_a_window_walks_idle_building_resolving_idle():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    assert sync.state is SyncState.IDLE
    sync.feed(parse_message("loading_start 0 0"))
    assert sync.state is SyncState.BUILDING
    sync.feed(parse_message("loading_end 0 Rig"))
    assert sync.state is SyncState.RESOLVING
    job = host.calls[-1][1]
    sync.on_resolved(_resolved(job.ticket))
    assert sync.state is SyncState.IDLE
    assert sync.applied == 1


def test_board_scoped_messages_are_consumed_only_while_a_window_is_open():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    assert _feed(sync, ["param_set /graph/drive gain 0.700000"]) == [False]
    _feed(sync, ["loading_start 0 0"])
    assert _feed(sync, ["param_set /graph/drive gain 0.700000", "connect /graph/a/out /graph/b/in"]) == [True, True]
    _feed(sync, ["loading_end 0 Rig"])
    assert _feed(sync, ["param_set /graph/drive gain 0.800000"]) == [True]


def test_window_boundary_messages_are_left_to_the_handler():
    sync = BoardSync(FakeHost(), Clock())
    assert _feed(sync, ["loading_start 0 0", "loading_end 0 Rig"]) == [False, False]


def test_connected_marks_the_next_window_as_a_replay_and_is_consumed():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    assert sync.feed(parse_message(":connected")) is True
    job = _window(sync, host)
    sync.on_resolved(_resolved(job.ticket))
    # a replay of an unmodified board syncs blend; so does a load: see the gate tests
    assert host.calls[-1][0] == "install"


def test_the_job_asks_only_for_metadata_it_lacks_and_lists_ttl_targets():
    host = FakeHost()
    host.plugin_dict = {"http://example.com/fixture/drive": {"name": "cached"}}
    host.bundles = frozenset({BUNDLE})
    sync = BoardSync(host, Clock())
    job = _window(sync, host, "connect_dump_unsaved.txt")
    assert "http://example.com/fixture/drive" not in job.uris
    assert job.known_bundles == frozenset({BUNDLE})
    assert all(len(t) == 3 for t in job.ttl_targets)


def test_a_stale_ticket_is_discarded():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    first = _window(sync, host)
    _feed(sync, ["loading_start 0 0"])
    sync.on_resolved(_resolved(first.ticket))
    assert sync.state is SyncState.BUILDING
    assert "install" not in host.kinds()


def test_params_during_resolving_reach_the_new_board():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    job = _window(sync, host)
    _feed(sync, ["param_set /graph/drive gain 0.123000"])
    sync.on_resolved(_resolved(job.ticket))
    assert host.plugin("drive").parameters[Symbol("gain")].value == pytest.approx(0.123)


def test_install_gets_the_streamed_title_the_snapshot_list_and_index():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    job = _window(sync, host)
    sync.on_resolved(_resolved(job.ticket))
    kind, bundle, presets, index, _ = host.calls[-1]
    assert (kind, bundle, presets) == ("install", BUNDLE, {0: "Clean", 1: "Lead"})
    assert index == 0
    assert host.installed.title == "Fixture Rig"


def test_failed_resolution_degrades_to_defaults_and_still_installs():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    job = _window(sync, host)
    sync.on_resolved(BoardResolved.failed(job.ticket))
    kind, bundle, presets, index, sync_blend = host.calls[-1]
    assert (kind, bundle, presets, index, sync_blend) == ("install", None, {0: "Default"}, 0, False)
    assert [p.instance_id for p in host.installed.plugins]  # tiles exist, bypass-only


def test_an_empty_streamed_title_is_untitled():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    job = _window(sync, host, "connect_dump_unsaved.txt")
    sync.on_resolved(_resolved(job.ticket, bundle=None))
    assert host.installed.title == UNTITLED


@pytest.mark.parametrize(
    ("replay", "dump", "bundle", "expected"),
    [
        (False, "connect_dump_saved.txt", BUNDLE, True),
        (True, "connect_dump_saved.txt", BUNDLE, True),
        (True, "connect_dump_unsaved.txt", BUNDLE, False),
        (False, "connect_dump_saved.txt", None, False),
    ],
    ids=["load", "replay-unmodified", "replay-modified", "no-bundle"],
)
def test_blend_gate(replay, dump, bundle, expected):
    host = FakeHost()
    sync = BoardSync(host, Clock())
    job = _window(sync, host, dump, replay=replay)
    sync.on_resolved(_resolved(job.ticket, bundle=bundle, bundle_known=True))
    assert host.calls[-1][4] is expected


def test_same_bundle_and_structure_reconciles_in_place():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    sync.on_resolved(_resolved(_window(sync, host).ticket))
    host.calls.clear()
    sync.on_resolved(_resolved(_window(sync, host).ticket))
    assert host.kinds()[-1] == "reconcile"


def test_a_different_bundle_swaps():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    sync.on_resolved(_resolved(_window(sync, host).ticket))
    sync.on_resolved(_resolved(_window(sync, host).ticket, bundle="/other.pedalboard"))
    assert host.kinds()[-1] == "install"


def test_a_changed_structure_swaps():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    sync.on_resolved(_resolved(_window(sync, host).ticket))
    _feed(sync, ["loading_start 0 0"])
    _feed(sync, ["loading_end 0 Fixture Rig"])  # an empty board: structure differs
    job = host.calls[-1][1]
    sync.on_resolved(_resolved(job.ticket))
    assert host.kinds()[-1] == "install"


def test_same_structure_compares_order_uris_and_connections():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    sync.on_resolved(_resolved(_window(sync, host).ticket))
    a = host.installed
    assert same_structure(a, a)
    assert not same_structure(a, Pedalboard.empty())


def test_reset_outside_a_window_clears_in_place_and_is_consumed():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    assert _feed(sync, ["remove :all"]) == [True]
    assert host.kinds() == ["clear"]


def test_the_load_window_shows_loading_but_a_replay_does_not():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    _feed(sync, ["loading_start 0 0"])
    assert host.kinds() == ["loading"]
    host.calls.clear()
    sync.feed(parse_message(":connected"))
    _feed(sync, ["loading_start 0 0"])
    assert host.kinds() == []


def test_a_snapshot_message_during_the_window_picks_the_index_and_name():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    _feed(sync, ["loading_start 0 0", "pedal_snapshot 2 Third"])
    _feed(sync, ["loading_end 0 Rig"])
    job = host.calls[-1][1]
    sync.on_resolved(_resolved(job.ticket, snapshots={0: "A", 1: "B"}))
    assert host.calls[-1][2:4] == ({0: "A", 1: "B", 2: "Third"}, 2)


def test_a_snapshot_message_while_idle_is_the_handlers():
    sync = BoardSync(FakeHost(), Clock())
    assert _feed(sync, ["pedal_snapshot 1 Lead"]) == [False]


def test_the_latest_transport_feeds_the_new_board():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    job = _window(sync, host)
    _feed(sync, ["transport 1 4.000000 96.000000 none"])
    sync.on_resolved(_resolved(job.ticket))
    assert host.installed.transport_plugin.parameters[Symbol(":bpm")].value == pytest.approx(96.0)


@dataclass(frozen=True)
class _Extra(PluginExtraData):
    tag: str


def _streamed_nam(uri: str | None, param_uri: str, value: str) -> PluginExtraData | None:
    return _Extra("streamed") if uri is not None and "neural-amp-modeler" in uri else None


def test_extra_data_fills_a_plugin_the_stream_gave_none():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    job = _window(sync, host)
    sync.on_resolved(_resolved(job.ticket, extra_data={"drive": _Extra("fetched")}))
    assert host.plugin("drive").customization.extra_data == _Extra("fetched")


def test_extra_data_never_overrides_what_the_stream_gave():
    host = FakeHost()
    host.patch_parser = _streamed_nam
    sync = BoardSync(host, Clock())
    job = _window(sync, host)
    sync.on_resolved(_resolved(job.ticket, extra_data={"neural_amp_modeler_lv2_1": _Extra("fetched")}))
    assert host.plugin("neural_amp_modeler_lv2_1").customization.extra_data == _Extra("streamed")


def test_building_watchdog_abandons_a_window_that_never_ends():
    clock, host = Clock(), FakeHost()
    sync = BoardSync(host, clock)
    _feed(sync, ["loading_start 0 0"])
    clock.now = 31.0
    sync.poll()
    assert sync.state is SyncState.IDLE
    assert host.kinds()[-1] == "abort"


def test_resolving_watchdog_completes_with_a_failed_resolution():
    clock, host = Clock(), FakeHost()
    sync = BoardSync(host, clock)
    _window(sync, host)
    clock.now = 31.0
    sync.poll()
    assert sync.state is SyncState.IDLE
    assert host.calls[-1][:2] == ("install", None)


def test_polling_inside_the_deadline_changes_nothing():
    clock, host = Clock(), FakeHost()
    sync = BoardSync(host, clock)
    _window(sync, host)
    clock.now = 10.0
    sync.poll()
    assert sync.state is SyncState.RESOLVING


def test_a_rebase_asks_for_bundle_and_snapshots_and_applies_the_answer():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    sync.request_rebase()
    [job] = [c[1] for c in host.calls if c[0] == "request"]
    assert job.uris == () and job.ttl_targets == ()
    sync.on_resolved(_resolved(job.ticket, boards=(("Rig", BUNDLE),)))
    assert host.kinds() == ["request", "boards", "rebase"]
    assert host.calls[-1] == ("rebase", BUNDLE, {0: "Clean", 1: "Lead"})
    assert sync.state is SyncState.IDLE and sync.applied == 0


def test_a_rebase_with_an_unknown_bundle_is_not_applied():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    sync.request_rebase()
    job = host.calls[-1][1]
    sync.on_resolved(BoardResolved.failed(job.ticket))
    assert host.kinds() == ["request"]


def test_rebase_is_ignored_during_a_window_and_deduplicated_while_in_flight():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    _feed(sync, ["loading_start 0 0"])
    sync.request_rebase()
    assert host.kinds() == ["loading"]
    host2 = FakeHost()
    sync2 = BoardSync(host2, Clock())
    sync2.request_rebase()
    sync2.request_rebase()
    assert host2.kinds().count("request") == 1


def test_a_window_cancels_a_rebase_in_flight():
    host = FakeHost()
    sync = BoardSync(host, Clock())
    sync.request_rebase()
    rebase = host.calls[-1][1]
    _feed(sync, ["loading_start 0 0"])
    sync.on_resolved(_resolved(rebase.ticket))
    assert "rebase" not in host.kinds()


def test_board_sync_imports_neither_the_handler_nor_the_plugins_package():
    code = (
        "import sys, modalapi.board_sync;"
        "bad = [m for m in ('modalapi.modhandler', 'plugins') if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
