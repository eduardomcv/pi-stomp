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

"""Plugin metadata fetching off the UI thread.

A live `add` for a plugin pi-stomp has not seen needs its LV2 metadata, which only
mod-ui can supply. The worker resolves it over REST so the 10 ms polling loop never
waits on HTTP; results come back through `drain()`, polled from the loop.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

import pistomp.httpclient as req

_BULK_TIMEOUT_S = 10.0
_GET_TIMEOUT_S = 5.0
_JOIN_TIMEOUT_S = 2.0
_NO_CACHE = {"Cache-Control": "no-cache", "Pragma": "no-cache"}


@dataclass(frozen=True)
class MetadataFetched:
    requested: tuple[str, ...]
    info: Mapping[str, dict]  # only the URIs that resolved


class MetadataFetcher(Protocol):
    def request_metadata(self, uris: Iterable[str]) -> None: ...

    def drain(self) -> list[MetadataFetched]: ...

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


class BoardFetcher:
    """One daemon worker, started on the first request. Requests and results cross
    threads only through the two queues."""

    def __init__(self, root_uri: str) -> None:
        self._root_uri = root_uri
        self._requests: queue.SimpleQueue[tuple[str, ...] | None] = queue.SimpleQueue()
        self._results: queue.SimpleQueue[MetadataFetched] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._lock = threading.Lock()

    def request_metadata(self, uris: Iterable[str]) -> None:
        batch = tuple(dict.fromkeys(uris))
        if not batch:
            return
        with self._lock:
            if self._closed:
                return
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, daemon=True, name="board-fetch")
                self._thread.start()
        self._requests.put(batch)

    def drain(self) -> list[MetadataFetched]:
        done: list[MetadataFetched] = []
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

    def _run(self) -> None:
        while True:
            batch = self._requests.get()
            if batch is None:
                return
            try:
                result = fetch_metadata(self._root_uri, batch)
            except Exception:
                logging.exception("plugin metadata fetch failed")
                result = MetadataFetched(batch, MappingProxyType({}))
            self._results.put(result)
