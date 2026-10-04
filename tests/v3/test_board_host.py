import pytest

from common.parameter import BYPASS_SYMBOL, Parameter, PortInfo, Symbol
from modalapi.board_sync import UNTITLED
from modalapi.pedalboard import BPM_SYMBOL, Pedalboard
from modalapi.plugin import Plugin
from tests.types import SystemFixture
from uilib.misc import InputEvent

GAIN = Symbol("gain")


def _gain(instance_id: str, value: float, binding: str | None = None, binding_range=None) -> Parameter:
    info: PortInfo = {"shortName": "Gain", "symbol": GAIN, "ranges": {"minimum": 0.0, "maximum": 1.0}}
    return Parameter(info, value, binding, instance_id, binding_range)


def _give_every_plugin_a_gain(system: SystemFixture) -> None:
    for plugin in system.handler.current.pedalboard.plugins:
        plugin.parameters[GAIN] = _gain(plugin.instance_id, 0.25)


def _candidate_from(system: SystemFixture) -> Pedalboard:
    """The installed board rebuilt from scratch with the same plugins: what a window would build."""
    handler = system.handler
    board = handler.current.pedalboard
    candidate = Pedalboard.empty(handler.customizer)
    candidate.title, candidate.bundle = board.title, board.bundle
    for live in board.plugins:
        parameters = {
            symbol: Parameter(
                {
                    "shortName": p.name,
                    "symbol": symbol,
                    "ranges": {"minimum": p.declared_minimum, "maximum": p.declared_maximum},
                },
                p.value,
                p.binding,
                live.instance_id,
                (p.minimum, p.maximum),
            )
            for symbol, p in live.parameters.items()
        }
        plugin = Plugin(
            live.instance_id, parameters, live.info, live.category, uri=live.uri, customization=live.customization
        )
        plugin.pedalboard_snapshot = dict(live.pedalboard_snapshot)
        candidate.plugins.append(plugin)
    candidate.connections = list(board.connections)
    return candidate


def _open_a_plugin_panel(handler):
    lcd = handler.lcd
    lcd.main_panel.sel_widget(lcd.w_plugins[0])
    lcd.main_panel.input_event(InputEvent.LONG_CLICK)
    handler.poll_lcd_updates()
    return lcd.pstack.current


def test_install_board_takes_presets_and_index_explicitly(parallel_beths_system):
    handler = parallel_beths_system.handler
    board = Pedalboard.empty(handler.customizer)
    handler.install_board(board, {0: "A", 1: "B"}, 1)
    assert handler.current.presets == {0: "A", 1: "B"}
    assert handler.current.preset_index == 1
    assert handler.current.pedalboard is board


def test_install_board_clears_the_loading_flag(parallel_beths_system):
    handler = parallel_beths_system.handler
    handler._is_pedalboard_loading = True
    handler.install_board(Pedalboard.empty(handler.customizer), {0: "Default"}, 0)
    assert handler._is_pedalboard_loading is False


@pytest.fixture
def blend_syncs(monkeypatch):
    calls = []

    def spy(bundle_path, blend_configs, root_uri):
        calls.append(bundle_path)
        return {}

    monkeypatch.setattr("modalapi.modhandler.SnapshotManager.sync_blend_snapshots", spy)
    return calls


def test_install_board_without_a_bundle_never_syncs_blend(parallel_beths_system, blend_syncs, caplog):
    handler = parallel_beths_system.handler
    handler.install_board(Pedalboard.empty(handler.customizer), {0: "Default"}, 0, sync_blend=True)
    assert blend_syncs == []
    assert "Failed to prepare blend modes" not in caplog.text


def test_install_board_syncs_blend_only_when_asked_and_bundled(parallel_beths_system, blend_syncs):
    handler = parallel_beths_system.handler
    bundled = Pedalboard.empty(handler.customizer)
    bundled.bundle = "/path/to/rig.pedalboard"
    handler.install_board(bundled, {0: "Default"}, 0)
    assert blend_syncs == []
    handler.install_board(bundled, {0: "Default"}, 0, sync_blend=True)
    assert len(blend_syncs) == 1


def test_set_current_pedalboard_reads_presets_from_mod_ui(parallel_beths_system):
    handler = parallel_beths_system.handler
    handler.next_pedalboard_preset_index = 1
    handler.set_current_pedalboard(handler.pedalboards["/path/to/new.pedalboard"])
    assert handler.current.presets == {0: "Clean", 1: "Lead"}
    assert handler.current.preset_index == 0
    assert handler.next_pedalboard_preset_index is None


def test_reconcile_updates_values_bypass_and_title_but_keeps_the_plugin_objects(parallel_beths_system):
    handler = parallel_beths_system.handler
    _give_every_plugin_a_gain(parallel_beths_system)
    before = {p.instance_id: p for p in handler.current.pedalboard.plugins}
    candidate = _candidate_from(parallel_beths_system)
    target = candidate.plugins[0]
    target.parameters[GAIN].reconcile(0.75)
    target.set_bypass(not target.is_bypassed())
    candidate.title = "Renamed"

    handler.reconcile_board(candidate, {0: "Only"}, 0)

    live = {p.instance_id: p for p in handler.current.pedalboard.plugins}
    assert all(live[i] is before[i] for i in before)
    assert live[target.instance_id].parameters[GAIN].value == pytest.approx(0.75)
    assert live[target.instance_id].is_bypassed() == target.is_bypassed()
    assert handler.current.pedalboard.title == "Renamed"
    assert handler.current.presets == {0: "Only"}


def _bound_gain_on_every_plugin(system: SystemFixture, binding: str, binding_range=None) -> None:
    for plugin in system.handler.current.pedalboard.plugins:
        plugin.parameters[GAIN] = _gain(plugin.instance_id, 0.25, binding, binding_range)


@pytest.fixture
def rebinds(parallel_beths_system, monkeypatch):
    calls = []
    monkeypatch.setattr(parallel_beths_system.handler, "_rebind_pedalboard", lambda: calls.append(1))
    return calls


def test_reconcile_restores_the_declared_range_when_the_cc_is_kept(parallel_beths_system, rebinds):
    handler = parallel_beths_system.handler
    _bound_gain_on_every_plugin(parallel_beths_system, "13:70", (0.2, 0.8))
    candidate = _candidate_from(parallel_beths_system)
    candidate.plugins[0].parameters[GAIN].clear_binding_range()

    handler.reconcile_board(candidate, {0: "Default"}, 0)

    live = handler.current.pedalboard.plugins[0].parameters[GAIN]
    assert (live.minimum, live.maximum) == (live.declared_minimum, live.declared_maximum)
    assert live.binding == "13:70"
    assert rebinds == []


def test_reconcile_adopts_a_custom_range_when_the_cc_is_kept(parallel_beths_system, rebinds):
    handler = parallel_beths_system.handler
    _bound_gain_on_every_plugin(parallel_beths_system, "13:70")
    candidate = _candidate_from(parallel_beths_system)
    candidate.plugins[0].parameters[GAIN].set_binding_range((0.2, 0.8))

    handler.reconcile_board(candidate, {0: "Default"}, 0)

    live = handler.current.pedalboard.plugins[0].parameters[GAIN]
    assert (live.minimum, live.maximum) == (0.2, 0.8)
    assert live.binding == "13:70"
    assert rebinds == []


def test_reconcile_reconciles_a_changed_bypass_binding(parallel_beths_system, rebinds):
    handler = parallel_beths_system.handler
    candidate = _candidate_from(parallel_beths_system)
    candidate.plugins[0].parameters[BYPASS_SYMBOL].binding = "13:60"
    live = handler.current.pedalboard.plugins[0]
    assert live.parameters[BYPASS_SYMBOL].binding is None

    handler.reconcile_board(candidate, {0: "Default"}, 0)

    assert live.parameters[BYPASS_SYMBOL].binding == "13:60"
    assert rebinds == [1]


def test_reconcile_unbinds_what_the_candidate_no_longer_binds(parallel_beths_system, rebinds):
    handler = parallel_beths_system.handler
    _bound_gain_on_every_plugin(parallel_beths_system, "13:70", (0.2, 0.8))
    candidate = _candidate_from(parallel_beths_system)
    unbound = candidate.plugins[0].parameters[GAIN]
    unbound.binding = None
    unbound.clear_binding_range()

    handler.reconcile_board(candidate, {0: "Default"}, 0)

    live = handler.current.pedalboard.plugins[0].parameters[GAIN]
    assert live.binding is None
    assert (live.minimum, live.maximum) == (live.declared_minimum, live.declared_maximum)


def test_reconcile_value_only_change_leaves_bindings_alone(parallel_beths_system, rebinds):
    handler = parallel_beths_system.handler
    _bound_gain_on_every_plugin(parallel_beths_system, "13:70", (0.2, 0.8))
    candidate = _candidate_from(parallel_beths_system)
    candidate.plugins[0].parameters[GAIN].reconcile(0.6)

    handler.reconcile_board(candidate, {0: "Default"}, 0)

    live = handler.current.pedalboard.plugins[0].parameters[GAIN]
    assert live.value == pytest.approx(0.6)
    assert (live.binding, live.minimum, live.maximum) == ("13:70", 0.2, 0.8)
    assert rebinds == []


def test_reconcile_keeps_an_open_plugin_panel_and_install_pops_it(parallel_beths_system):
    handler = parallel_beths_system.handler
    pstack = handler.lcd.pstack
    panel = _open_a_plugin_panel(handler)
    assert panel is not handler.lcd.main_panel

    handler.reconcile_board(_candidate_from(parallel_beths_system), {0: "Default"}, 0)
    assert pstack.current is panel

    handler.install_board(Pedalboard.empty(handler.customizer), {0: "Default"}, 0)
    assert pstack.current is not panel


def test_clear_board_empties_in_place_without_a_hardware_reinit(parallel_beths_system):
    handler = parallel_beths_system.handler
    reinit_calls = []
    original = handler.hardware.reinit
    handler.hardware.reinit = lambda cfg: reinit_calls.append(cfg) or original(cfg)

    handler.clear_board()

    board = handler.current.pedalboard
    assert board.plugins == [] and board.connections == []
    assert (board.title, board.bundle) == (UNTITLED, None)
    assert handler.current.presets == {0: "Default"} and handler.current.preset_index == 0
    assert reinit_calls == []


def test_clear_board_resets_the_transport_and_releases_its_controllers(parallel_beths_system):
    handler = parallel_beths_system.handler
    old_bpm = handler.current.pedalboard.transport_plugin.parameters[BPM_SYMBOL]
    old_bpm.reconcile(90.0)
    old_bpm.binding = "13:70"
    handler.bind_current_pedalboard()
    controller = handler.hardware.controllers["13:70"]
    assert handler.current.control_for(old_bpm) is controller

    handler.clear_board()

    new_bpm = handler.current.pedalboard.transport_plugin.parameters[BPM_SYMBOL]
    assert new_bpm is not old_bpm
    assert (new_bpm.value, new_bpm.binding) == (120.0, None)
    assert handler.current.control_for(old_bpm) is None
    assert controller.parameter is not old_bpm


def test_rebase_adopts_a_new_bundle_title_and_reapplies_the_config(parallel_beths_system):
    handler = parallel_beths_system.handler
    handler.update_board_list((("Saved As", "/data/.pedalboards/SavedAs.pedalboard"),))
    reinit_calls = []
    original = handler.hardware.reinit
    handler.hardware.reinit = lambda cfg: reinit_calls.append(cfg) or original(cfg)
    plugins_before = list(handler.current.pedalboard.plugins)

    handler.rebase_board("/data/.pedalboards/SavedAs.pedalboard", {0: "Default", 1: "Two"})

    board = handler.current.pedalboard
    assert board.bundle == "/data/.pedalboards/SavedAs.pedalboard"
    assert board.title == "Saved As"
    assert board.plugins == plugins_before
    assert len(reinit_calls) == 1
    assert handler.current.presets == {0: "Default", 1: "Two"}


def test_rebase_to_the_same_bundle_does_not_reinit(parallel_beths_system):
    handler = parallel_beths_system.handler
    reinit_calls = []
    handler.hardware.reinit = lambda cfg: reinit_calls.append(cfg)
    handler.rebase_board(handler.current.pedalboard.bundle, None)
    assert reinit_calls == []


def test_update_board_list_replaces_the_shells(parallel_beths_system):
    handler = parallel_beths_system.handler
    handler.update_board_list((("One", "/b/one.pedalboard"), ("Two", "/b/two.pedalboard")))
    assert [p.title for p in handler.pedalboard_list] == ["One", "Two"]
    assert set(handler.pedalboards) == {"/b/one.pedalboard", "/b/two.pedalboard"}
    assert handler.known_bundles() == frozenset({"/b/one.pedalboard", "/b/two.pedalboard"})


def test_abort_window_clears_the_loading_flag(parallel_beths_system):
    handler = parallel_beths_system.handler
    handler._is_pedalboard_loading = True
    handler.abort_window()
    assert handler._is_pedalboard_loading is False
