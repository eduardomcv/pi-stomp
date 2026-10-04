import json
from unittest.mock import MagicMock

from tests.types import SystemFixture


def serve_board(
    system: SystemFixture,
    *,
    bundle: str | None,
    snapshots: dict[str, str] | None = None,
    boards: list[dict] | None = None,
) -> None:
    """Make the patched `pistomp.httpclient.get` answer what a board window's
    resolution asks mod-ui: /pedalboard/current, /snapshot/list and, if given, /pedalboard/list."""
    previous = system.mock_get.side_effect

    def get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "pedalboard/current" in url:
            resp.text = bundle or ""
        elif "snapshot/list" in url:
            resp.text = json.dumps(snapshots if snapshots is not None else {"0": "Default"})
        elif "pedalboard/list" in url and boards is not None:
            resp.text = json.dumps(boards)
        else:
            return previous(url, **kwargs)
        return resp

    system.mock_get.side_effect = get


def play_window(system: SystemFixture, lines: list[str]) -> None:
    """Inject a window's lines and drain until the board is applied (the fake fetcher
    resolves inline, so one extra poll lands the result)."""
    for line in lines:
        system.ws_bridge.inject(line)
    system.handler.poll_ws_messages()
    system.handler.poll_ws_messages()
