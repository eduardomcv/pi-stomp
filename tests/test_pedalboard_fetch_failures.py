import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from common.parameter import BYPASS_SYMBOL
from modalapi.pedalboard import Pedalboard


@pytest.fixture(autouse=True)
def _no_exit(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("sys.exit called")

    monkeypatch.setattr(sys, "exit", fail)


def test_get_plugin_data_is_empty_when_mod_ui_is_unreachable():
    pb = Pedalboard("T", "/bundle")
    with patch("pistomp.httpclient.get", side_effect=ConnectionRefusedError("down")):
        assert pb.get_plugin_data("http://example.com/drive") == {}


def test_get_pedalboard_info_is_empty_when_mod_ui_is_unreachable():
    pb = Pedalboard("T", "/bundle")
    with patch("pistomp.httpclient.get", side_effect=ConnectionRefusedError("down")):
        assert pb.get_pedalboard_info() == {}


def test_hydrate_stays_retryable_after_an_outage():
    pb = Pedalboard("T", "/bundle")
    with patch("pistomp.httpclient.get", side_effect=ConnectionRefusedError("down")):
        pb.hydrate({})
    assert not pb.hydrated
    assert pb.plugins == []

    body = SimpleNamespace(status_code=200, text=json.dumps({"plugins": [], "connections": []}))
    with patch("pistomp.httpclient.get", return_value=body):
        pb.hydrate({})
    assert pb.hydrated


def test_build_plugin_with_empty_info_is_bypass_only():
    pb = Pedalboard("T", "/bundle")
    plugin = pb._build_plugin("Mystery", "http://example.com/mystery", 10.0, 20.0, {})
    assert list(plugin.parameters) == [BYPASS_SYMBOL]
    assert plugin.uri == "http://example.com/mystery"
    assert plugin.category is None
    assert (plugin.canvas_x, plugin.canvas_y) == (10.0, 20.0)
