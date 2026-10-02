"""Reading a repository's tree and file bytes from one fallback source.

An import pins one source for its whole run (the source the repository was
found at, see ``kohakuhub.api.importer.router``) rather than walking the
fallback chain per file: a repository that answers at two sources is two
different repositories (#75), and a snapshot half from each is not a copy of
either.

URL mapping and the source token come from ``FallbackClient``, so the paths
here are the KohakuHub-shaped ones every other fallback call uses. The
transport is this module's own, because an import streams:

- ``try_fallback_resolve`` buffers a 200 into memory and deliberately does
  not follow redirects (Plan A hands the ``Location`` to the client so bytes
  never traverse the backend). An import *is* the backend fetching the
  bytes, and a 1.5 GB safetensors must not be held in a Python ``bytes``.
- The redirect target is fetched **without** the source's ``Authorization``
  header. HuggingFace redirects to a presigned CDN URL, and S3 rejects a
  presigned request that carries an Authorization header.
"""

from dataclasses import dataclass
from typing import AsyncIterator, Optional

import httpx

from kohakuhub.api.fallback.client import FallbackClient
from kohakuhub.config import cfg
from kohakuhub.logger import get_logger

logger = get_logger("IMPORT_SOURCE")

# HF's listing maximum; asking for less only costs more round trips.
TREE_PAGE = 1000
# Chunks read from the upstream at a time (1 MiB).
CHUNK = 1024 * 1024


class SourceError(Exception):
    """The source could not be read. ``message`` is safe to show a user."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class SourceFile:
    """One file of the source's tree, as the source describes it."""

    path: str
    size: int | None  # None when the source did not say (some kohakuhub trees)
    oid: str | None  # the source's LFS object id, when it stored one


def _entry_files(payload: object) -> list[SourceFile]:
    """The files of one tree page, ignoring directories (recursive listings
    carry both, and a file's path already says where it lives)."""
    if not isinstance(payload, list):
        raise SourceError("The source returned an unexpected tree listing")
    files = []
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            continue
        path = entry.get("path")
        if not path:
            continue
        size = entry.get("size")
        lfs = entry.get("lfs") if isinstance(entry.get("lfs"), dict) else None
        oid = (lfs or {}).get("oid") or entry.get("oid")
        files.append(
            SourceFile(path=path, size=int(size) if isinstance(size, int) else None, oid=oid)
        )
    return files


def _next_cursor(response: httpx.Response) -> str | None:
    """The ``cursor`` of the upstream's ``Link: rel="next"``, if it paginated.

    The listing is cursor-based (an opaque offset), so the next page is
    whatever the upstream's own next link says; building one ourselves would
    mean guessing at an offset format that is explicitly opaque.
    """
    link = response.headers.get("link")
    if not link:
        return None
    for part in link.split(","):
        url, _, params = part.partition(";")
        if 'rel="next"' not in params:
            continue
        query = url.strip().strip("<>").partition("?")[2]
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            if key == "cursor" and value:
                return value
    return None


class SourceReader:
    """Reads one repository at one fallback source."""

    def __init__(self, source: dict, repo_type: str, namespace: str, name: str, revision: str):
        self.source = source
        self.repo_type = repo_type
        self.namespace = namespace
        self.name = name
        self.revision = revision
        self.client = FallbackClient(
            source_url=source["url"],
            source_type=source["source_type"],
            token=source.get("token"),
        )
        self.timeout = cfg.fallback.timeout_seconds

    @property
    def source_name(self) -> str:
        return self.source.get("name") or self.source["url"]

    def _tree_path(self) -> str:
        return f"/api/{self.repo_type}s/{self.namespace}/{self.name}/tree/{self.revision}"

    def _resolve_path(self, path: str) -> str:
        return f"/{self.repo_type}s/{self.namespace}/{self.name}" f"/resolve/{self.revision}/{path}"

    def _headers(self) -> dict[str, str]:
        if self.client.token:
            return {"Authorization": f"Bearer {self.client.token}"}
        return {}

    async def list_files(self) -> list[SourceFile]:
        """Every file of the source's tree at the pinned revision.

        Paginates to the end. A tree too large to list is not caught here:
        the caller's own caps (``max_bytes``, the target's quota) refuse the
        import, and an unbounded listing is bounded by the repository.
        """
        url = self.client.map_url(self._tree_path(), self.repo_type)
        params: dict = {"recursive": "true", "expand": "true", "limit": TREE_PAGE}
        files: list[SourceFile] = []
        seen: set[str] = set()
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            while True:
                try:
                    response = await http.get(url, params=params, headers=self._headers())
                except httpx.HTTPError as e:
                    raise SourceError(f"Could not list {self.name} at {self.source_name}: {e}")
                if response.status_code == 401 or response.status_code == 403:
                    raise SourceError(
                        f"{self.source_name} refused access to {self.namespace}/{self.name}. "
                        "Add an external token for this source and try again.",
                        status=response.status_code,
                    )
                if response.status_code == 404:
                    raise SourceError(
                        f"{self.namespace}/{self.name} is not on {self.source_name}",
                        status=404,
                    )
                if response.status_code >= 400:
                    raise SourceError(
                        f"{self.source_name} answered {response.status_code} for "
                        f"{self.namespace}/{self.name}: {response.text[:200]}",
                        status=response.status_code,
                    )
                page = _entry_files(response.json())
                for entry in page:
                    if entry.path in seen:  # a source that paginates over a moving tree
                        continue
                    seen.add(entry.path)
                    files.append(entry)
                cursor = _next_cursor(response)
                if not cursor:
                    return files
                params = {**params, "cursor": cursor}

    async def open(self, path: str) -> tuple[AsyncIterator[bytes], Optional[int]]:
        """Start streaming ``path``, following the source's redirect if it sends one.

        Returns the chunk iterator and the length the source declared
        (``None`` when it did not, which a chunked or CDN response often
        does not). The caller must ``aclose`` the iterator's context.
        """
        resolve_url = self.client.map_url(self._resolve_path(path), self.repo_type)
        http = httpx.AsyncClient(timeout=self.timeout)
        try:
            first = await http.send(
                http.build_request("GET", resolve_url, headers=self._headers()),
                stream=True,
                follow_redirects=False,
            )
        except httpx.HTTPError as e:
            await http.aclose()
            raise SourceError(f"Could not read {path} from {self.source_name}: {e}")

        if 300 <= first.status_code < 400 and first.headers.get("location"):
            location = str(httpx.URL(resolve_url).join(first.headers["location"]))
            await first.aclose()
            try:
                # No Authorization: the target is a presigned URL, and S3
                # rejects a presigned request that also carries that header.
                stream = await http.send(
                    http.build_request("GET", location), stream=True, follow_redirects=True
                )
            except httpx.HTTPError as e:
                await http.aclose()
                raise SourceError(f"Could not read {path} from {self.source_name}: {e}")
        else:
            if first.status_code == 404:
                await first.aclose()
                await http.aclose()
                raise SourceError(f"{path} is missing at {self.source_name}", status=404)
            if first.status_code >= 400:
                body = (await first.aread())[:200]
                await first.aclose()
                await http.aclose()
                raise SourceError(
                    f"{self.source_name} answered {first.status_code} for {path}: "
                    f"{body.decode('utf-8', 'replace')}",
                    status=first.status_code,
                )
            stream = first

        size_header = stream.headers.get("content-length")
        declared = int(size_header) if size_header and size_header.isdigit() else None

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in stream.aiter_bytes(CHUNK):
                    yield chunk
            finally:
                await stream.aclose()
                await http.aclose()

        return chunks(), declared
