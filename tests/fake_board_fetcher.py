from collections.abc import Iterable

from modalapi.board_fetch import MetadataFetched, fetch_metadata


class FakeBoardFetcher:
    """MetadataFetcher that resolves inline through the same fetch_metadata the worker
    runs, so a test's patched `pistomp.httpclient` still serves it. Results wait for
    drain() like the real thing; `hold` parks requests until `release()`."""

    def __init__(self, root_uri: str = "http://localhost:80/") -> None:
        self._root_uri = root_uri
        self._ready: list[MetadataFetched] = []
        self._held: list[tuple[str, ...]] = []
        self.requests: list[tuple[str, ...]] = []
        self.hold = False
        self.closed = False

    def request_metadata(self, uris: Iterable[str]) -> None:
        batch = tuple(dict.fromkeys(uris))
        if not batch or self.closed:
            return
        self.requests.append(batch)
        if self.hold:
            self._held.append(batch)
        else:
            self._ready.append(fetch_metadata(self._root_uri, batch))

    def release(self) -> None:
        held, self._held = self._held, []
        self._ready.extend(fetch_metadata(self._root_uri, batch) for batch in held)

    def drain(self) -> list[MetadataFetched]:
        ready, self._ready = self._ready, []
        return ready

    def cleanup(self) -> None:
        self.closed = True
