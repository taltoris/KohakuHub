"""Reading a source repository: URL mapping, pagination, streaming, redirects.

Runs without the service stack: the source is a fake inside the test's own
``httpx.MockTransport``, because what is being checked is this module's own
HTTP behavior — which upstream path it asks for, how it paginates, and what
headers survive a redirect — not LakeFS or the bucket.
"""

import httpx
import pytest

from kohakuhub.api.importer import source as source_module
from kohakuhub.api.importer.source import SourceError, SourceReader

CHUNK = source_module.CHUNK


@pytest.fixture
def reader_factory(monkeypatch):
    """Build a ``SourceReader`` whose requests go to ``handler`` instead of the network.

    ``SourceReader`` constructs its own ``httpx.AsyncClient``, so the seam is a
    patched client class that injects the transport. That keeps the production
    code free of a test-only parameter.
    """
    requests: list[httpx.Request] = []

    def build(handler, *, source_type="huggingface", token="sekret", repo_type="model"):
        seen = []

        def recording(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            requests.append(request)
            return handler(request)

        transport = httpx.MockTransport(recording)
        real_client = httpx.AsyncClient

        class PatchedClient(real_client):
            def __init__(self, *args, **kwargs):
                kwargs["transport"] = transport
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(source_module.httpx, "AsyncClient", PatchedClient)
        reader = SourceReader(
            {
                "url": "https://source.example",
                "name": "HuggingFace",
                "source_type": source_type,
                "token": token,
            },
            repo_type,
            "a-company",
            "their-repo",
            "main",
        )
        return reader, seen

    yield build


def _tree_body(entries):
    return httpx.Response(200, json=entries)


def _file(path, size=None, oid=None):
    entry = {"type": "file", "path": path}
    if size is not None:
        entry["size"] = size
    if oid is not None:
        entry["lfs"] = {"oid": oid, "size": size}
    return entry


@pytest.mark.asyncio
async def test_list_files_asks_the_upstream_tree_and_keeps_only_files(reader_factory):
    def handler(request):
        assert request.method == "GET"
        # HF has no /api prefix for a model tree; the fallback client maps it.
        assert request.url.path == "/api/models/a-company/their-repo/tree/main"
        assert request.url.params["recursive"] == "true"
        assert request.headers["authorization"] == "Bearer sekret"
        return _tree_body(
            [
                {"type": "directory", "path": "weights"},
                _file("README.md", size=42),
                _file("weights/model.safetensors", size=1000, oid="a" * 64),
                # A tree entry with no path is not addressable; skip it.
                {"type": "file"},
            ]
        )

    reader, _ = reader_factory(handler)
    files = await reader.list_files()

    assert [f.path for f in files] == ["README.md", "weights/model.safetensors"]
    assert files[0].size == 42
    assert files[1].oid == "a" * 64


@pytest.mark.asyncio
async def test_list_files_follows_the_upstreams_own_next_link(reader_factory):
    """The listing is cursor-based and the offset is opaque, so the next page
    is whatever the upstream's ``Link`` names — not a number this code makes up."""

    def handler(request):
        if request.url.params.get("cursor") == "page-2":
            return _tree_body([_file("b.txt", size=2)])
        return httpx.Response(
            200,
            json=[_file("a.txt", size=1)],
            headers={
                "link": (
                    "</api/models/a-company/their-repo/tree/main"
                    '?cursor=page-2&limit=1000>; rel="next"'
                )
            },
        )

    reader, seen = reader_factory(handler)
    files = await reader.list_files()

    assert [f.path for f in files] == ["a.txt", "b.txt"]
    assert len(seen) == 2
    assert seen[1].url.params["cursor"] == "page-2"


@pytest.mark.asyncio
async def test_list_files_stops_at_a_link_that_is_not_next(reader_factory):
    def handler(request):
        return httpx.Response(
            200,
            json=[_file("a.txt", size=1)],
            headers={"link": '</x?cursor=page-2>; rel="prev"'},
        )

    reader, seen = reader_factory(handler)
    files = await reader.list_files()

    assert [f.path for f in files] == ["a.txt"]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_list_files_drops_a_repeated_path(reader_factory):
    """A source paginating over a tree that moved can repeat an entry; a
    duplicate would stage the same file twice for no reason."""

    def handler(request):
        if request.url.params.get("cursor"):
            return _tree_body([_file("a.txt", size=1)])
        return httpx.Response(
            200,
            json=[_file("a.txt", size=1)],
            headers={"link": '</t?cursor=next>; rel="next"'},
        )

    reader, _ = reader_factory(handler)
    files = await reader.list_files()

    assert [f.path for f in files] == ["a.txt"]


@pytest.mark.asyncio
async def test_list_files_reports_a_refusal_as_actionable(reader_factory):
    """A gated repository fails in one second here, not after 20 GB."""

    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    reader, _ = reader_factory(handler)
    with pytest.raises(SourceError) as caught:
        await reader.list_files()

    assert caught.value.status == 401
    assert "token" in caught.value.message.lower()
    assert "HuggingFace" in caught.value.message


@pytest.mark.asyncio
async def test_list_files_reports_a_missing_repository(reader_factory):
    def handler(request):
        return httpx.Response(404, json={})

    reader, _ = reader_factory(handler)
    with pytest.raises(SourceError) as caught:
        await reader.list_files()

    assert caught.value.status == 404
    assert "not on HuggingFace" in caught.value.message


@pytest.mark.asyncio
async def test_list_files_rejects_a_tree_that_is_not_a_list(reader_factory):
    def handler(request):
        return httpx.Response(200, json={"unexpected": "shape"})

    reader, _ = reader_factory(handler)
    with pytest.raises(SourceError):
        await reader.list_files()


@pytest.mark.asyncio
async def test_list_files_reports_a_transport_failure(reader_factory):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    reader, _ = reader_factory(handler)
    with pytest.raises(SourceError) as caught:
        await reader.list_files()

    assert "Could not list" in caught.value.message


@pytest.mark.asyncio
async def test_open_streams_a_direct_response_and_reports_its_length(reader_factory):
    payload = b"x" * (CHUNK + 10)

    def handler(request):
        # HF is asymmetric: a model's resolve URL drops the ``/models``
        # prefix the hub's own path carries.
        assert request.url.path == "/a-company/their-repo/resolve/main/big.bin"
        return httpx.Response(200, content=payload, headers={"content-length": str(len(payload))})

    reader, _ = reader_factory(handler)
    chunks, declared = await reader.open("big.bin")
    body = b"".join([chunk async for chunk in chunks])

    assert body == payload
    assert declared == len(payload)


@pytest.mark.asyncio
async def test_open_breaks_the_body_into_bounded_chunks(reader_factory):
    """A 1.5 GB safetensors must never be held in one ``bytes``."""

    payload = b"y" * (CHUNK * 2 + 5)

    def handler(request):
        return httpx.Response(200, content=payload)

    reader, _ = reader_factory(handler)
    chunks, _ = await reader.open("big.bin")
    sizes = [len(chunk) async for chunk in chunks]

    assert all(size <= CHUNK for size in sizes)
    assert sum(sizes) == len(payload)
    assert len(sizes) > 1


@pytest.mark.asyncio
async def test_open_follows_a_redirect_without_the_source_token(reader_factory):
    """HuggingFace points at a presigned CDN URL, and S3 rejects a presigned
    request that also carries an Authorization header."""

    payload = b"cdn bytes"

    def handler(request):
        if request.url.host == "cdn.example":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=payload)
        return httpx.Response(302, headers={"location": "https://cdn.example/presigned/object"})

    reader, seen = reader_factory(handler)
    chunks, declared = await reader.open("weights.bin")
    body = b"".join([chunk async for chunk in chunks])

    assert body == payload
    # httpx fills Content-Length in for a bytes body; what matters is that the
    # reader reports what the source said rather than guessing.
    assert declared == len(payload)
    assert seen[0].headers["authorization"] == "Bearer sekret"
    assert seen[1].url.host == "cdn.example"


@pytest.mark.asyncio
async def test_open_resolves_a_relative_redirect_against_the_source(reader_factory):
    """A relative Location must be followed at the source, not against
    whatever base this process happens to have."""

    def handler(request):
        if request.url.path == "/api/resolve-cache/models/a-company/their-repo/main/a.txt":
            return httpx.Response(200, content=b"resolved")
        return httpx.Response(
            307, headers={"location": "/api/resolve-cache/models/a-company/their-repo/main/a.txt"}
        )

    reader, seen = reader_factory(handler)
    chunks, _ = await reader.open("a.txt")
    body = b"".join([chunk async for chunk in chunks])

    assert body == b"resolved"
    assert seen[1].url.host == "source.example"


@pytest.mark.asyncio
async def test_open_treats_an_unusable_length_as_unknown(reader_factory):
    """A chunked or proxied response may send a Content-Length that is not a
    number; the import then simply cannot pre-check the size."""

    def handler(request):
        return httpx.Response(200, content=b"body", headers={"content-length": "chunked"})

    reader, _ = reader_factory(handler)
    chunks, declared = await reader.open("a.bin")
    assert b"".join([chunk async for chunk in chunks]) == b"body"
    assert declared is None


@pytest.mark.asyncio
async def test_open_reports_a_missing_file(reader_factory):
    def handler(request):
        return httpx.Response(404, json={})

    reader, _ = reader_factory(handler)
    with pytest.raises(SourceError) as caught:
        await reader.open("gone.bin")

    assert caught.value.status == 404
    assert "gone.bin" in caught.value.message


@pytest.mark.asyncio
async def test_open_reports_a_refusal(reader_factory):
    def handler(request):
        return httpx.Response(403, content=b"forbidden")

    reader, _ = reader_factory(handler)
    with pytest.raises(SourceError) as caught:
        await reader.open("gated.bin")

    assert caught.value.status == 403
    assert "403" in caught.value.message


@pytest.mark.asyncio
async def test_open_reports_a_transport_failure(reader_factory):
    def handler(request):
        raise httpx.ConnectError("no route to host")

    reader, _ = reader_factory(handler)
    with pytest.raises(SourceError) as caught:
        await reader.open("a.bin")

    assert "Could not read" in caught.value.message


@pytest.mark.asyncio
async def test_a_reader_without_a_token_sends_no_authorization_header(reader_factory):
    """The source may be public, and the source's own admin token may be
    absent; sending an empty Authorization would be worse than sending none."""

    def handler(request):
        assert "authorization" not in request.headers
        return _tree_body([_file("a.txt", size=1)])

    reader, _ = reader_factory(handler, token=None)
    await reader.list_files()


@pytest.mark.asyncio
async def test_a_kohakuhub_source_keeps_the_kohakuhub_path_shape(reader_factory):
    """Another KohakuHub instance serves the same paths we do, so no mapping."""

    def handler(request):
        assert request.url.path == "/api/models/a-company/their-repo/tree/main"
        return _tree_body([])

    reader, _ = reader_factory(handler, source_type="kohakuhub")
    assert await reader.list_files() == []


@pytest.mark.asyncio
async def test_the_datasets_path_keeps_its_prefix_for_huggingface(reader_factory):
    """HF is asymmetric: a model's resolve URL drops ``/models``, a dataset's
    keeps ``/datasets``. The mapping is the fallback client's, and this checks
    the reader goes through it rather than building URLs itself."""

    def handler(request):
        assert request.url.path == "/datasets/a-company/their-repo/resolve/main/a.csv"
        return httpx.Response(200, content=b"a,b\n")

    reader, _ = reader_factory(handler, repo_type="dataset")
    chunks, _ = await reader.open("a.csv")
    assert b"".join([chunk async for chunk in chunks]) == b"a,b\n"
