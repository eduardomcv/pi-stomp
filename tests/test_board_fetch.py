import json
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

import pytest

from modalapi import board_fetch
from modalapi.board_fetch import (
    BoardFetcher,
    BoardJob,
    BoardResolved,
    Fetcher,
    MetadataFetched,
    fetch_metadata,
    resolve_board,
)
from modalapi.plugin_customization import PluginCustomization, PluginExtraData
from tests.fake_board_fetcher import FakeBoardFetcher

ROOT = "http://localhost:80/"
BUNDLE = "/home/pistomp/data/.pedalboards/Rig.pedalboard"
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
    results: list[Any] = []
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
    fake: Fetcher = FakeBoardFetcher()
    with patch("pistomp.httpclient.post", return_value=_resp(200, {DRIVE: DRIVE_INFO})), patch("pistomp.httpclient.get"):
        fake.request_metadata([DRIVE])
        [result] = fake.drain()
    assert isinstance(result, MetadataFetched)
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
    assert isinstance(result, MetadataFetched)
    assert dict(result.info) == {DRIVE: DRIVE_INFO}


def _router(routes):
    """GET handler serving `routes[path-fragment] -> (status, body)`; a value that is
    an Exception is raised. Unlisted URLs 404."""

    def get(url, headers=None, timeout=None):
        for fragment, outcome in routes.items():
            if fragment in url:
                if isinstance(outcome, Exception):
                    raise outcome
                status, body = outcome
                text = body if isinstance(body, str) else json.dumps(body)
                return SimpleNamespace(status_code=status, text=text)
        return SimpleNamespace(status_code=404, text="{}")

    return get


def test_resolve_board_gathers_bundle_snapshots_and_metadata():
    routes = {"pedalboard/current": (200, BUNDLE), "snapshot/list": (200, {"0": "Clean", "1": "Lead"})}
    with (
        patch("pistomp.httpclient.post", return_value=_resp(200, {DRIVE: DRIVE_INFO})),
        patch("pistomp.httpclient.get", side_effect=_router(routes)),
    ):
        out = resolve_board(ROOT, BoardJob(ticket=7, uris=(DRIVE,), known_bundles=frozenset({BUNDLE})), None, None)
    assert out.ticket == 7
    assert dict(out.metadata) == {DRIVE: DRIVE_INFO}
    assert (out.bundle, out.bundle_known) == (BUNDLE, True)
    assert dict(out.snapshots or {}) == {0: "Clean", 1: "Lead"}
    assert out.boards is None


def test_resolve_board_strips_a_trailing_slash_and_a_json_string():
    for body in (BUNDLE + "/", json.dumps(BUNDLE)):
        with patch("pistomp.httpclient.get", side_effect=_router({"pedalboard/current": (200, body)})):
            assert resolve_board(ROOT, BoardJob(ticket=1), None, None).bundle == BUNDLE


def test_resolve_board_treats_a_body_that_is_not_an_absolute_path_as_a_failed_fetch():
    for body in ("{}", json.dumps("relative.pedalboard"), "Rig.pedalboard"):
        with patch("pistomp.httpclient.get", side_effect=_router({"pedalboard/current": (200, body)})):
            assert resolve_board(ROOT, BoardJob(ticket=1), None, None).bundle_known is False
            out = resolve_board(ROOT, BoardJob(ticket=1), lambda: BUNDLE, None)
        assert (out.bundle, out.bundle_known) == (BUNDLE, True)


def test_resolve_board_unsaved_board_has_no_bundle_but_is_known():
    with patch("pistomp.httpclient.get", side_effect=_router({"pedalboard/current": (200, "")})):
        out = resolve_board(ROOT, BoardJob(ticket=1), None, None)
    assert (out.bundle, out.bundle_known) == (None, True)


def test_resolve_board_falls_back_to_last_json_when_current_fails():
    with patch("pistomp.httpclient.get", side_effect=_router({"pedalboard/current": ConnectionRefusedError("x")})):
        out = resolve_board(ROOT, BoardJob(ticket=1), lambda: BUNDLE, None)
    assert (out.bundle, out.bundle_known) == (BUNDLE, True)


def test_resolve_board_with_nothing_answering_is_all_failed_not_an_error():
    with (
        patch("pistomp.httpclient.post", side_effect=ConnectionRefusedError("x")),
        patch("pistomp.httpclient.get", side_effect=ConnectionRefusedError("x")),
    ):
        out = resolve_board(ROOT, BoardJob(ticket=3, uris=(DRIVE,)), lambda: None, None)
    assert out == BoardResolved.failed(3)


def test_resolve_board_refetches_the_list_only_for_an_unknown_bundle():
    boards = [{"title": "Rig", "bundle": BUNDLE}, {"title": "Other", "bundle": "/b/other.pedalboard"}]
    routes = {
        "pedalboard/current": (200, BUNDLE),
        "snapshot/list": (200, {"0": "Default"}),
        "pedalboard/list": (200, boards),
    }
    with patch("pistomp.httpclient.get", side_effect=_router(routes)) as get:
        unknown = resolve_board(ROOT, BoardJob(ticket=1), None, None)
        known = resolve_board(ROOT, BoardJob(ticket=2, known_bundles=frozenset({BUNDLE})), None, None)
    assert unknown.boards == (("Rig", BUNDLE), ("Other", "/b/other.pedalboard"))
    assert known.boards is None
    assert sum("pedalboard/list" in c.args[0] for c in get.call_args_list) == 1


def test_resolve_board_bad_snapshot_list_is_none():
    routes = {"pedalboard/current": (200, BUNDLE), "snapshot/list": (500, {})}
    with patch("pistomp.httpclient.get", side_effect=_router(routes)):
        assert resolve_board(ROOT, BoardJob(ticket=1), None, None).snapshots is None


@dataclass(frozen=True)
class _Notes(PluginExtraData):
    pass


def test_resolve_board_reads_extra_data_through_the_injected_customizer():
    seen = []

    def ttl_reader(uri, bundlepath="", instance_number=None):
        seen.append((uri, bundlepath, instance_number))
        if uri == DRIVE:
            return PluginCustomization(extra_data=_Notes())
        raise OSError("no ttl")

    job = BoardJob(ticket=1, ttl_targets=(("drive", 4, DRIVE), ("delay", 5, DELAY)))
    with patch("pistomp.httpclient.get", side_effect=_router({"pedalboard/current": (200, BUNDLE)})):
        out = resolve_board(ROOT, job, None, ttl_reader)
    assert list(out.extra_data) == ["drive"]
    assert seen == [(DRIVE, BUNDLE, 4), (DELAY, BUNDLE, 5)]


def test_resolve_board_skips_extra_data_without_a_bundle():
    called = []

    def ttl_reader(uri, bundlepath="", instance_number=None):
        called.append((uri, bundlepath, instance_number))
        return PluginCustomization()

    job = BoardJob(ticket=1, ttl_targets=(("drive", 4, DRIVE),))
    with patch("pistomp.httpclient.get", side_effect=_router({"pedalboard/current": (200, "")})):
        out = resolve_board(ROOT, job, None, ttl_reader)
    assert called == [] and dict(out.extra_data) == {}


def test_worker_serves_board_jobs_and_metadata_batches_in_order(fetcher):
    routes = {"pedalboard/current": (200, BUNDLE), "snapshot/list": (200, {"0": "Default"})}
    with (
        patch("pistomp.httpclient.post", return_value=_resp(200, {DRIVE: DRIVE_INFO})),
        patch("pistomp.httpclient.get", side_effect=_router(routes)),
    ):
        fetcher.request_metadata([DRIVE])
        fetcher.request_board(BoardJob(ticket=9))
        first, second = _wait_for(fetcher, 2)
    assert isinstance(first, MetadataFetched) and isinstance(second, BoardResolved)
    assert second.ticket == 9


def test_worker_survives_a_failing_board_job(fetcher):
    with patch.object(board_fetch, "resolve_board", side_effect=[RuntimeError("boom")]):
        fetcher.request_board(BoardJob(ticket=4))
        [result] = _wait_for(fetcher, 1)
    assert result == BoardResolved.failed(4)


def test_fake_fetcher_resolves_board_jobs_and_holds_them():
    fake = FakeBoardFetcher()
    with patch("pistomp.httpclient.get", side_effect=_router({"pedalboard/current": (200, BUNDLE)})):
        fake.hold = True
        fake.request_board(BoardJob(ticket=1))
        assert fake.drain() == [] and fake.board_jobs == [BoardJob(ticket=1)]
        fake.release()
        [out] = fake.drain()
    assert isinstance(out, BoardResolved) and out.bundle == BUNDLE


def test_board_fetch_imports_neither_the_handler_nor_the_board():
    code = (
        "import sys, modalapi.board_fetch;"
        "bad = [m for m in ('modalapi.modhandler', 'modalapi.pedalboard', 'modalapi.ws_protocol', 'modalapi.board_sync', 'modalapi.plugins') if m in sys.modules];"
        "sys.exit(1 if bad else 0)"
    )
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
