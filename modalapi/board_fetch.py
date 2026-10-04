# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of pi-stomp.
#
# pi-stomp is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pi-stomp is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with pi-stomp.  If not, see <https://www.gnu.org/licenses/>.

"""REST fetching off the UI thread.

A live `add` for a plugin pi-stomp has not seen needs its LV2 metadata, and a board
built from a replayed window needs its bundle, snapshots and extra data; only mod-ui
can supply them. The worker resolves them over REST so the 10 ms polling loop never
waits on HTTP; results come back through `drain()`, polled from the loop.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

import common.token as Token
import pistomp.httpclient as req

if TYPE_CHECKING:
    from modalapi.plugin_customization import Customizer, PluginExtraData

_BULK_TIMEOUT_S = 10.0
_GET_TIMEOUT_S = 5.0
_JOIN_TIMEOUT_S = 2.0
_LIST_TIMEOUT_S = 5.0
_NO_CACHE = {"Cache-Control": "no-cache", "Pragma": "no-cache"}


@dataclass(frozen=True)
class MetadataFetched:
    requested: tuple[str, ...]
    info: Mapping[str, dict]  # only the URIs that resolved


@dataclass(frozen=True)
class BoardJob:
    ticket: int
    uris: tuple[str, ...] = ()
    known_bundles: frozenset[str] = frozenset()
    ttl_targets: tuple[tuple[str, int, str], ...] = ()


@dataclass(frozen=True)
class BoardResolved:
    ticket: int
    metadata: Mapping[str, dict]
    bundle: str | None
    bundle_known: bool
    snapshots: Mapping[int, str] | None
    boards: tuple[tuple[str, str], ...] | None
    extra_data: Mapping[str, PluginExtraData]

    @classmethod
    def failed(cls, ticket: int) -> "BoardResolved":
        return cls(ticket, MappingProxyType({}), None, False, None, None, MappingProxyType({}))


FetchResult = MetadataFetched | BoardResolved


class Fetcher(Protocol):
    def request_metadata(self, uris: Iterable[str]) -> None: ...

    def request_board(self, job: BoardJob) -> None: ...

    def drain(self) -> list[FetchResult]: ...

    def cleanup(self) -> None: ...


def _get_one(root_uri: str, uri: str) -> dict | None:
    url = root_uri + "effect/get?uri=" + urllib.parse.quote(uri)
    try:
        resp = req.get(url, headers=_NO_CACHE, timeout=_GET_TIMEOUT_S)
        if resp.status_code != 200:
            logging.error("mod-ui not able to get plugin data: %s Status: %s", url, resp.status_code)
            return None
        info = json.loads(resp.text)
    except Exception as e:
        logging.error("Cannot fetch plugin data %s: %s", url, e)
        return None
    return info if isinstance(info, dict) and info else None


def fetch_metadata(root_uri: str, uris: Iterable[str]) -> MetadataFetched:
    """Never raises. mod-ui's bulk endpoint silently omits URIs it cannot describe,
    so every URI it left out gets its own effect/get."""
    requested = tuple(dict.fromkeys(uris))
    found: dict[str, dict] = {}
    try:
        resp = req.post(root_uri + "effect/bulk/", json=list(requested), timeout=_BULK_TIMEOUT_S)
        if resp.status_code == 200:
            body = json.loads(resp.text)
            if isinstance(body, dict):
                found.update({u: i for u, i in body.items() if u in requested and isinstance(i, dict) and i})
        else:
            logging.warning("effect/bulk Status: %s", resp.status_code)
    except Exception as e:
        logging.warning("effect/bulk failed: %s", e)
    for uri in requested:
        if uri not in found:
            info = _get_one(root_uri, uri)
            if info is not None:
                found[uri] = info
    return MetadataFetched(requested, MappingProxyType(found))


def _get_text(root_uri: str, path: str, timeout: float) -> str | None:
    try:
        resp = req.get(root_uri + path, headers=_NO_CACHE, timeout=timeout)
        if resp.status_code != 200:
            logging.warning("mod-ui %s Status: %s", path, resp.status_code)
            return None
        return resp.text
    except Exception as e:
        logging.warning("mod-ui %s failed: %s", path, e)
        return None


def _bundle_of(text: str) -> str | None:
    text = text.strip()
    if text.startswith('"'):
        try:
            text = str(json.loads(text))
        except ValueError:
            return None
    return text.rstrip("/") or None


def _fetch_bundle(root_uri: str, fallback: Callable[[], str | None] | None) -> tuple[str | None, bool]:
    text = _get_text(root_uri, "pedalboard/current", _GET_TIMEOUT_S)
    if text is not None:
        return _bundle_of(text), True
    if fallback is not None:
        try:
            bundle = fallback()
        except Exception as e:
            logging.warning("bundle fallback failed: %s", e)
            return None, False
        return (bundle.rstrip("/") or None, True) if bundle else (None, False)
    return None, False


def _fetch_snapshots(root_uri: str) -> Mapping[int, str] | None:
    text = _get_text(root_uri, "snapshot/list", _LIST_TIMEOUT_S)
    if text is None:
        return None
    try:
        return MappingProxyType({int(k): str(v) for k, v in json.loads(text).items()})
    except (ValueError, AttributeError):
        return None


def _fetch_boards(root_uri: str) -> tuple[tuple[str, str], ...] | None:
    text = _get_text(root_uri, "pedalboard/list", _LIST_TIMEOUT_S)
    if text is None:
        return None
    try:
        return tuple((str(pb[Token.TITLE]), str(pb[Token.BUNDLE])) for pb in json.loads(text))
    except (ValueError, KeyError, TypeError):
        return None


def _read_extra_data(
    bundle: str, targets: Iterable[tuple[str, int, str]], reader: Customizer
) -> dict[str, PluginExtraData]:
    found: dict[str, PluginExtraData] = {}
    for instance, number, uri in targets:
        try:
            extra = reader(uri, bundle, number).extra_data
        except Exception as e:
            logging.warning("effect.ttl for %s unreadable: %s", instance, e)
            continue
        if extra is not None:
            found[instance] = extra
    return found


def resolve_board(
    root_uri: str,
    job: BoardJob,
    bundle_fallback: Callable[[], str | None] | None,
    ttl_reader: Customizer | None,
) -> BoardResolved:
    """Everything a board window leaves out. Never raises: a part that fails comes
    back as its unknown value, and the board is built with what did arrive."""
    metadata = fetch_metadata(root_uri, job.uris).info if job.uris else MappingProxyType({})
    bundle, known = _fetch_bundle(root_uri, bundle_fallback)
    snapshots = _fetch_snapshots(root_uri)
    boards = _fetch_boards(root_uri) if bundle is not None and bundle not in job.known_bundles else None
    extra: dict[str, PluginExtraData] = {}
    if bundle is not None and ttl_reader is not None and job.ttl_targets:
        extra = _read_extra_data(bundle, job.ttl_targets, ttl_reader)
    return BoardResolved(job.ticket, metadata, bundle, known, snapshots, boards, MappingProxyType(extra))


class BoardFetcher:
    """One daemon worker, started on the first request. Requests and results cross
    threads only through the two queues."""

    def __init__(
        self,
        root_uri: str,
        *,
        bundle_fallback: Callable[[], str | None] | None = None,
        ttl_reader: Customizer | None = None,
    ) -> None:
        self._root_uri = root_uri
        self._bundle_fallback = bundle_fallback
        self._ttl_reader = ttl_reader
        self._requests: queue.SimpleQueue[tuple[str, ...] | BoardJob | None] = queue.SimpleQueue()
        self._results: queue.SimpleQueue[FetchResult] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._lock = threading.Lock()

    def request_metadata(self, uris: Iterable[str]) -> None:
        batch = tuple(dict.fromkeys(uris))
        if batch and self._ensure_started():
            self._requests.put(batch)

    def request_board(self, job: BoardJob) -> None:
        if self._ensure_started():
            self._requests.put(job)

    def drain(self) -> list[FetchResult]:
        done: list[FetchResult] = []
        while True:
            try:
                done.append(self._results.get_nowait())
            except queue.Empty:
                return done

    def cleanup(self) -> None:
        with self._lock:
            self._closed = True
            thread, self._thread = self._thread, None
        if thread is None:
            return
        self._requests.put(None)
        thread.join(timeout=_JOIN_TIMEOUT_S)

    def _ensure_started(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="board-fetch")
                self._thread.start()
            return True

    def _run(self) -> None:
        while True:
            item = self._requests.get()
            if item is None:
                return
            try:
                if isinstance(item, BoardJob):
                    result: FetchResult = resolve_board(self._root_uri, item, self._bundle_fallback, self._ttl_reader)
                else:
                    result = fetch_metadata(self._root_uri, item)
            except Exception:
                logging.exception("board fetch failed")
                result = (
                    BoardResolved.failed(item.ticket)
                    if isinstance(item, BoardJob)
                    else MetadataFetched(item, MappingProxyType({}))
                )
            self._results.put(result)
