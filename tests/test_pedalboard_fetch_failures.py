import sys

import pytest

from common.parameter import BYPASS_SYMBOL
from modalapi.pedalboard import Pedalboard


@pytest.fixture(autouse=True)
def _no_exit(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("sys.exit called")

    monkeypatch.setattr(sys, "exit", fail)


def test_build_plugin_with_empty_info_is_bypass_only():
    pb = Pedalboard("T", "/bundle")
    plugin = pb._build_plugin("Mystery", "http://example.com/mystery", 10.0, 20.0, {})
    assert list(plugin.parameters) == [BYPASS_SYMBOL]
    assert plugin.uri == "http://example.com/mystery"
    assert plugin.category is None
    assert (plugin.canvas_x, plugin.canvas_y) == (10.0, 20.0)
