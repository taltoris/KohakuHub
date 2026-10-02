"""The import endpoints: what they refuse, and what they accept.

Validation is the point of these: every refusal here is a refusal that would
otherwise have become a task failing minutes later, after gigabytes had
moved.
"""

import pytest

from test.kohakuhub.support.live_server import start_live_server, stop_live_server
from test.kohakuhub.support.mock_hf_server import build_mock_hf_app

SOURCE_NAME = "Imported Source"


@pytest.fixture(scope="module")
def upstream_url():
    """One mock upstream for the module; it is stateless."""
    handle = start_live_server(build_mock_hf_app())
    try:
        yield handle.base_url
    finally:
        stop_live_server(handle)


@pytest.fixture
def imported_source(backend_test_state, upstream_url):
    """Point the fallback chain at the mock upstream for one test.

    The chain is what the importer reads through, so replacing it is replacing
    the source; the previous configuration is restored afterwards.
    """
    cfg = backend_test_state.modules.config_module.cfg
    previous = cfg.fallback.sources
    cfg.fallback.sources = [
        {
            "url": upstream_url,
            "token": None,
            "priority": 1,
            "name": SOURCE_NAME,
            "source_type": "huggingface",
        }
    ]
    from kohakuhub.api.fallback.cache import get_cache

    get_cache().clear()
    try:
        yield cfg
    finally:
        cfg.fallback.sources = previous
        get_cache().clear()


async def _import(owner_client, **payload):
    body = {"source_repo_id": "some-org/pattern", "repo_type": "model"}
    body.update(payload)
    return await owner_client.post("/api/repos/import", json=body)


def _import_rows(state=None):
    from kohakuhub.db import RepositoryImport

    query = RepositoryImport.select()
    if state is not None:
        query = query.where(RepositoryImport.state == state)
    return list(query)


@pytest.mark.asyncio
async def test_import_refuses_an_anonymous_caller(client):
    response = await client.post("/api/repos/import", json={"source_repo_id": "a/b"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_import_refuses_a_malformed_source_id(owner_client, imported_source):
    response = await _import(owner_client, source_repo_id="just-a-name")
    assert response.status_code == 400
    assert "namespace/name" in response.json()["detail"]["error"]


@pytest.mark.asyncio
async def test_import_refuses_a_malformed_target_id(owner_client, imported_source):
    response = await _import(
        owner_client, source_repo_id="some-org/pattern", target_repo_id="not-a-repo-id"
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_import_refuses_to_merge_into_an_existing_repository(owner_client, imported_source):
    """A snapshot into an occupied name would merge two histories."""
    existing = await owner_client.post(
        "/api/repos/create",
        json={"type": "model", "name": "already-here", "private": False},
    )
    assert existing.status_code in (200, 201, 409)

    response = await _import(
        owner_client,
        source_repo_id="some-org/pattern",
        target_repo_id="owner/already-here",
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "already exists" in detail["error"]
    assert detail["repo_id"] == "owner/already-here"


@pytest.mark.asyncio
async def test_import_refuses_a_source_that_does_not_have_it(owner_client, imported_source):
    response = await _import(owner_client, source_repo_id="some-org/definitely-not-there")
    assert response.status_code == 404
    assert "not on any configured fallback source" in response.json()["detail"]["error"]


@pytest.mark.asyncio
async def test_import_refuses_a_target_outside_the_callers_namespaces(
    owner_client, imported_source
):
    """Importing into someone else's namespace is not the caller's to do."""
    response = await _import(
        owner_client,
        source_repo_id="some-org/pattern",
        target_repo_id="someone-else/pattern",
    )
    assert response.status_code in (403, 404)


@pytest.mark.asyncio
async def test_import_refuses_an_import_over_the_callers_own_cap(owner_client, imported_source):
    response = await _import(
        owner_client,
        source_repo_id="some-org/pattern",
        max_bytes=1,
    )
    assert response.status_code == 400
    assert "over the" in response.json()["detail"]["error"]


@pytest.mark.asyncio
async def test_import_refuses_when_no_source_is_configured(owner_client, backend_test_state):
    cfg = backend_test_state.modules.config_module.cfg
    previous = cfg.fallback.sources
    cfg.fallback.sources = []
    from kohakuhub.api.fallback.cache import get_cache

    get_cache().clear()
    try:
        response = await _import(owner_client)
        assert response.status_code == 400
        assert "No fallback source is configured" in response.json()["detail"]["error"]
    finally:
        cfg.fallback.sources = previous
        get_cache().clear()


@pytest.mark.asyncio
async def test_import_accepts_and_reserves_the_target(owner_client, imported_source):
    response = await _import(owner_client, source_repo_id="some-org/pattern")

    assert response.status_code == 200
    body = response.json()
    # The default target is the caller's namespace under the source's name.
    assert body["repo_id"] == "owner/pattern"
    assert body["state"] == "queued"
    assert body["task_id"] is not None
    assert body["url"].endswith("/models/owner/pattern")
    assert body["source_repo_id"] == "some-org/pattern"
    assert body["files_total"] >= 1
    assert body["bytes_total"] > 0

    # The target exists from here on, empty until the task commits, so a
    # failure leaves something to delete rather than nothing to see.
    info = await owner_client.get("/api/models/owner/pattern")
    assert info.status_code == 200

    # The import row records where it reads from, for the page and the task.
    rows = [row for row in _import_rows() if row.source_repo_id == "some-org/pattern"]
    assert len(rows) == 1
    assert rows[0].source_url == imported_source.fallback.sources[0]["url"]
    assert rows[0].state == "queued"


@pytest.mark.asyncio
async def test_import_pins_the_source_it_found_it_at(owner_client, imported_source):
    """One source for the whole import: a snapshot half from each of two
    sources is a copy of neither."""
    response = await _import(owner_client, source_repo_id="some-org/pattern")
    assert response.status_code == 200

    row = next(r for r in _import_rows() if r.source_repo_id == "some-org/pattern")
    assert row.source_url == imported_source.fallback.sources[0]["url"]
    assert row.source_type == "huggingface"
    assert row.source_revision == "main"


@pytest.mark.asyncio
async def test_import_honours_an_explicit_target_and_visibility(owner_client, imported_source):
    response = await _import(
        owner_client,
        source_repo_id="some-org/pattern",
        target_repo_id="owner/renamed-on-import",
        private=True,
    )
    assert response.status_code == 200
    assert response.json()["repo_id"] == "owner/renamed-on-import"

    info = await owner_client.get("/api/models/owner/renamed-on-import")
    assert info.status_code == 200
    assert info.json()["private"] is True


@pytest.mark.asyncio
async def test_status_follows_the_task(owner_client, imported_source):
    started = (await _import(owner_client, source_repo_id="some-org/pattern")).json()

    response = await owner_client.get(f"/api/repos/import/{started['task_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["import_id"] == started["import_id"]
    assert body["task_id"] == started["task_id"]
    assert body["task_status"] == "queued"
    assert body["state"] in ("queued", "running")


@pytest.mark.asyncio
async def test_status_rejects_a_task_that_is_not_an_import(owner_client, imported_source):
    from kohakuhub import tasks

    other = tasks.enqueue("repo.import.on_failure", {"payload": {}, "failure": {}})
    try:
        response = await owner_client.get(f"/api/repos/import/{other}")
        assert response.status_code == 404
    finally:
        from kohakuhub.db import BackgroundTask

        BackgroundTask.delete().where(BackgroundTask.id == other).execute()


@pytest.mark.asyncio
async def test_latest_reports_nothing_for_a_repository_that_was_never_imported(
    owner_client, imported_source
):
    await owner_client.post(
        "/api/repos/create", json={"type": "model", "name": "ordinary", "private": False}
    )
    response = await owner_client.get(
        "/api/repos/import/latest", params={"repo_id": "owner/ordinary", "repo_type": "model"}
    )
    assert response.status_code == 200
    assert response.json() == {"import": None}


@pytest.mark.asyncio
async def test_the_repository_info_carries_its_import_state(owner_client, imported_source):
    """The page must not need a second request to know it is looking at a
    partial copy; that request could be the one that fails."""
    started = (await _import(owner_client, source_repo_id="some-org/pattern")).json()

    info = await owner_client.get("/api/models/owner/pattern")
    assert info.status_code == 200
    carried = info.json()["_import"]
    assert carried["import_id"] == started["import_id"]
    assert carried["state"] == "queued"
    assert carried["source_repo_id"] == "some-org/pattern"


@pytest.mark.asyncio
async def test_a_queued_import_can_be_cancelled(owner_client, imported_source):
    started = (await _import(owner_client, source_repo_id="some-org/pattern")).json()

    response = await owner_client.post(f"/api/repos/import/{started['task_id']}/cancel")
    assert response.status_code == 200
    assert response.json()["state"] == "cancelled"

    # The partial repository stays: it is the user's to inspect and delete.
    info = await owner_client.get("/api/models/owner/pattern")
    assert info.status_code == 200
    assert info.json()["_import"]["state"] == "cancelled"


@pytest.mark.asyncio
async def test_cancelling_a_finished_import_says_so(owner_client, imported_source):
    started = (await _import(owner_client, source_repo_id="some-org/pattern")).json()
    await owner_client.post(f"/api/repos/import/{started['task_id']}/cancel")

    again = await owner_client.post(f"/api/repos/import/{started['task_id']}/cancel")
    assert again.status_code == 409
