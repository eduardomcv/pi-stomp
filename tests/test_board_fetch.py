import json
import subprocess
import sys
import threading
import time
import urllib.parse
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import pytest

from modalapi import board_fetch
from modalapi.board_fetch import BoardFetcher, MetadataFetched, MetadataFetcher, fetch_metadata
from tests.fake_board_fetcher import FakeBoardFetcher

ROOT = "http://localhost:80/"
DRIVE = "http://example.com/drive"
DELAY = "http://example.com/delay"
DRIVE_INFO = {"name": "Drive", "category": ["Distortion"]}
DELAY_INFO = {"name": "Delay", "category": ["Delay"]}
NO_CACHE = {"Cache-Control": "no-cache", "Pragma": "no-cache"}


def _resp(status, body):
    return SimpleNamespace(status_code=status, text=json.dumps(body))


def _post_returning(outcome):
    def post(*_args, **_kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return post


def _get_serving(table):
    def get(url, headers=None, timeout=None):
        uri = urllib.parse.unquote(url.split("uri=", 1)[1])
        info = table.get(uri)
        return _resp(200, info) if info is not None else _resp(404, {})

    return get


def _wait_for(fetcher, count, timeout=2.0):
    deadline = time.monotonic() + timeout
    results: list[MetadataFetched] = []
    while len(results) < count and time.monotonic() < deadline:
        results.extend(fetcher.drain())
        time.sleep(0.005)
    return results


def _worker_alive() -> bool:
    return any(t.name == "board-fetch" and t.is_alive() for t in threading.enumerate())


@pytest.fixture
def fetcher():
    f = BoardFetcher(ROOT)
    yield f
    f.cleanup()


def test_bulk_resolves_every_uri_without_per_uri_gets():
    with (
        patch("pistomp.httpclient.post", return_value=_resp(200, {DRIVE: DRIVE_INFO, DELAY: DELAY_INFO})) as post,
        patch("pistomp.httpclient.get") as get,
    ):
        result = fetch_metadata(ROOT, [DRIVE, DELAY])
    assert result.requested == (DRIVE, DELAY)
    assert dict(result.info) == {DRIVE: DRIVE_INFO, DELAY: DELAY_INFO}
    get.assert_not_called()
    assert post.call_args.args[0] == ROOT + "effect/bulk/"
    assert post.call_args.kwargs["json"] == [DRIVE, DELAY]
    assert post.call_args.kwargs["timeout"] == board_fetch._BULK_TIMEOUT_S


def test_uri_missing_from_bulk_falls_back_to_effect_get_for_that_uri_only():
    with (
        patch("pistomp.httpclient.post", return_value=_resp(200, {DRIVE: DRIVE_INFO})),
        patch("pistomp.httpclient.get", side_effect=_get_serving({DELAY: DELAY_INFO})) as get,
    ):
        result = fetch_metadata(ROOT, [DRIVE, DELAY])
    assert dict(result.info) == {DRIVE: DRIVE_INFO, DELAY: DELAY_INFO}
    assert [c.args[0] for c in get.call_args_list] == [ROOT + "effect/get?uri=" + urllib.parse.quote(DELAY)]
    assert get.call_args.kwargs["headers"] == NO_CACHE
    assert get.call_args.kwargs["timeout"] == board_fetch._GET_TIMEOUT_S


@pytest.mark.parametrize(
    "bulk",
    [RuntimeError("down"), _resp(500, {}), _resp(200, ["not", "a", "dict"]), _resp(200, {})],
    ids=["raises", "http-500", "not-a-dict", "empty"],
)
def test_unusable_bulk_falls_back_to_effect_get_for_every_uri(bulk):
    with (
        patch("pistomp.httpclient.post", side_effect=_post_returning(bulk)),
        patch("pistomp.httpclient.get", side_effect=_get_serving({DRIVE: DRIVE_INFO, DELAY: DELAY_INFO})) as get,
    ):
        result = fetch_metadata(ROOT, [DRIVE, DELAY])
    assert dict(result.info) == {DRIVE: DRIVE_INFO, DELAY: DELAY_INFO}
    assert get.call_count == 2


def test_nothing_resolvable_yields_empty_info_without_raising():
    with (
        patch("pistomp.httpclient.post", side_effect=ConnectionRefusedError("down")),
        patch("pistomp.httpclient.get", side_effect=ConnectionRefusedError("down")),
    ):
        result = fetch_metadata(ROOT, [DRIVE])
    assert result.requested == (DRIVE,)
    assert dict(result.info) == {}


def test_effect_get_error_status_and_empty_body_count_as_missing():
    with (
        patch("pistomp.httpclient.post", return_value=_resp(200, {})),
        patch("pistomp.httpclient.get", side_effect=_get_serving({DELAY: {}})),
    ):
        result = fetch_metadata(ROOT, [DRIVE, DELAY])
    assert dict(result.info) == {}


def test_duplicate_uris_are_requested_once_in_order():
    with (
        patch("pistomp.httpclient.post", return_value=_resp(200, {DRIVE: DRIVE_INFO, DELAY: DELAY_INFO})) as post,
        patch("pistomp.httpclient.get"),
    ):
        result = fetch_metadata(ROOT, [DRIVE, DRIVE, DELAY])
    assert result.requested == (DRIVE, DELAY)
    assert post.call_args.kwargs["json"] == [DRIVE, DELAY]


def test_uris_nobody_asked_for_are_ignored():
    stray = "http://example.com/stray"
    with (
        patch("pistomp.httpclient.post", return_value=_resp(200, {DRIVE: DRIVE_INFO, stray: DELAY_INFO})),
        patch("pistomp.httpclient.get"),
    ):
        result = fetch_metadata(ROOT, [DRIVE])
    assert dict(result.info) == {DRIVE: DRIVE_INFO}


def test_worker_resolves_off_the_calling_thread(fetcher):
    seen: list[str] = []

    def post(*_args, **_kwargs):
        seen.append(threading.current_thread().name)
        return _resp(200, {DRIVE: DRIVE_INFO})

    with patch("pistomp.httpclient.post", side_effect=post), patch("pistomp.httpclient.get"):
        fetcher.request_metadata([DRIVE])
        [result] = _wait_for(fetcher, 1)
    assert dict(result.info) == {DRIVE: DRIVE_INFO}
    assert seen == ["board-fetch"]
    assert threading.current_thread().name != "board-fetch"


def test_result_is_delivered_by_drain_only(fetcher):
    release = threading.Event()

    def post(*_args, **_kwargs):
        release.wait(timeout=2.0)
        return _resp(200, {DRIVE: DRIVE_INFO})

    with patch("pistomp.httpclient.post", side_effect=post), patch("pistomp.httpclient.get"):
        fetcher.request_metadata([DRIVE])
        assert fetcher.drain() == []
        release.set()
        assert len(_wait_for(fetcher, 1)) == 1


def test_worker_never_exits_the_process(fetcher, monkeypatch):
    exits: list[tuple] = []
    monkeypatch.setattr(sys, "exit", lambda *args: exits.append(args))
    with (
        patch("pistomp.httpclient.post", side_effect=ConnectionRefusedError("down")),
        patch("pistomp.httpclient.get", side_effect=ConnectionRefusedError("down")),
    ):
        fetcher.request_metadata([DRIVE])
        [result] = _wait_for(fetcher, 1)
    assert dict(result.info) == {}
    assert exits == []


def test_worker_survives_an_unexpected_error(fetcher):
    resolved = MetadataFetched((DRIVE,), MappingProxyType({DRIVE: DRIVE_INFO}))
    with patch.object(board_fetch, "fetch_metadata", side_effect=[RuntimeError("boom"), resolved]):
        fetcher.request_metadata([DELAY])
        [first] = _wait_for(fetcher, 1)
        fetcher.request_metadata([DRIVE])
        [second] = _wait_for(fetcher, 1)
    assert first.requested == (DELAY,)
    assert dict(first.info) == {}
    assert second is resolved


def test_empty_request_starts_no_worker(fetcher):
    fetcher.request_metadata([])
    assert not _worker_alive()


def test_cleanup_stops_the_worker_and_is_idempotent():
    f = BoardFetcher(ROOT)
    with patch("pistomp.httpclient.post", return_value=_resp(200, {DRIVE: DRIVE_INFO})), patch("pistomp.httpclient.get"):
        f.request_metadata([DRIVE])
        _wait_for(f, 1)
        f.cleanup()
        assert not _worker_alive()
        f.cleanup()


def test_cleanup_without_a_request_is_safe():
    BoardFetcher(ROOT).cleanup()


def test_request_after_cleanup_is_ignored():
    f = BoardFetcher(ROOT)
    f.cleanup()
    with patch("pistomp.httpclient.post") as post:
        f.request_metadata([DRIVE])
        assert f.drain() == []
    post.assert_not_called()
    assert not _worker_alive()


def test_fake_fetcher_satisfies_the_protocol_and_resolves_inline():
    fake: MetadataFetcher = FakeBoardFetcher()
    with patch("pistomp.httpclient.post", return_value=_resp(200, {DRIVE: DRIVE_INFO})), patch("pistomp.httpclient.get"):
        fake.request_metadata([DRIVE])
        [result] = fake.drain()
    assert dict(result.info) == {DRIVE: DRIVE_INFO}
    assert fake.drain() == []


def test_fake_fetcher_holds_until_released():
    fake = FakeBoardFetcher()
    fake.hold = True
    with patch("pistomp.httpclient.post", return_value=_resp(200, {DRIVE: DRIVE_INFO})), patch("pistomp.httpclient.get"):
        fake.request_metadata([DRIVE])
        assert fake.drain() == []
        assert fake.requests == [(DRIVE,)]
        fake.release()
        [result] = fake.drain()
    assert dict(result.info) == {DRIVE: DRIVE_INFO}


def test_board_fetch_imports_neither_the_handler_nor_the_board():
    code = (
        "import sys, modalapi.board_fetch;"
        "bad = [m for m in ('modalapi.modhandler', 'modalapi.pedalboard', 'modalapi.ws_protocol') if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
