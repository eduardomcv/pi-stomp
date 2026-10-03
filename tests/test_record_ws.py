"""record_ws: which lines land in a replay fixture, and where capture stops."""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "record_ws", Path(__file__).resolve().parent.parent / "util" / "record_ws.py"
)
assert _SPEC is not None and _SPEC.loader is not None
record_ws = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(record_ws)


def test_keep_drops_what_the_bridge_never_queues():
    assert not record_ws.keep("ping")
    assert not record_ws.keep("data_ready 12")
    assert not record_ws.keep("output_set /graph/a meter 0.5")
    assert record_ws.keep("stats 12.5 0")
    assert record_ws.keep("loading_end 0 ")


def test_take_windows_stops_after_the_nth_loading_end():
    feed = [
        "ping",
        "transport 0 4.000000 120.000000 none",
        "loading_start 0 0",
        "loading_end 0 A",
        "remove :all",
        "loading_start 0 0",
        "loading_end 1 B",
        "param_set /graph/a gain 0.500000",
    ]
    assert list(record_ws.take_windows(feed, 1)) == feed[1:4]
    assert list(record_ws.take_windows(feed, 2)) == feed[1:7]
