from collections.abc import Callable, Iterable

from modalapi.board_fetch import BoardJob, FetchResult, fetch_metadata, resolve_board
from modalapi.plugin_customization import Customizer


class FakeBoardFetcher:
    """Fetcher that resolves inline through the same fetch_metadata / resolve_board the
    worker runs, so a test's patched `pistomp.httpclient` still serves it. Results wait
    for drain() like the real thing; `hold` parks requests until `release()`."""

    def __init__(
        self,
        root_uri: str = "http://localhost:80/",
        *,
        bundle_fallback: Callable[[], str | None] | None = None,
        ttl_reader: Customizer | None = None,
    ) -> None:
        self._root_uri = root_uri
        self._bundle_fallback = bundle_fallback
        self._ttl_reader = ttl_reader
        self._ready: list[FetchResult] = []
        self._held: list[tuple[str, ...] | BoardJob] = []
        self.requests: list[tuple[str, ...]] = []
        self.board_jobs: list[BoardJob] = []
        self.hold = False
        self.closed = False

    def request_metadata(self, uris: Iterable[str]) -> None:
        batch = tuple(dict.fromkeys(uris))
        if not batch or self.closed:
            return
        self.requests.append(batch)
        self._submit(batch)

    def request_board(self, job: BoardJob) -> None:
        if self.closed:
            return
        self.board_jobs.append(job)
        self._submit(job)

    def release(self) -> None:
        held, self._held = self._held, []
        self._ready.extend(self._resolve(item) for item in held)

    def drain(self) -> list[FetchResult]:
        ready, self._ready = self._ready, []
        return ready

    def cleanup(self) -> None:
        self.closed = True

    def _submit(self, item: tuple[str, ...] | BoardJob) -> None:
        if self.hold:
            self._held.append(item)
        else:
            self._ready.append(self._resolve(item))

    def _resolve(self, item: tuple[str, ...] | BoardJob) -> FetchResult:
        if isinstance(item, BoardJob):
            return resolve_board(self._root_uri, item, self._bundle_fallback, self._ttl_reader)
        return fetch_metadata(self._root_uri, item)
