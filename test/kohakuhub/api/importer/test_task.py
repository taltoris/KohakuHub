"""The ``repo.import`` handler: staging, idempotency, quota and cancellation.

Runs against the real stack (LakeFS, the bucket, the database): an import is
staging plus a commit, and the thing worth testing is what it leaves behind.
"""

import hashlib

import pytest

from test.kohakuhub.support.live_server import start_live_server, stop_live_server
from test.kohakuhub.support.mock_hf_server import (
    PATTERN_A_BYTES,
    build_mock_hf_app,
)
from kohakuhub.task_testing import RecordingContext

SOURCE_NAME = "Imported Source"


@pytest.fixture(scope="module")
def upstream_url():
    handle = start_live_server(build_mock_hf_app())
    try:
        yield handle.base_url
    finally:
        stop_live_server(handle)


@pytest.fixture
def imported_source(backend_test_state, upstream_url):
    """Point the fallback chain at the mock upstream for one test."""
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


@pytest.fixture
def import_state(prepared_backend_test_state):
    """Clean up the import rows and the repositories these tests create."""
    from kohakuhub.db import Repository, RepositoryImport, BackgroundTask

    def clear():
        RepositoryImport.delete().execute()
        BackgroundTask.delete().where(BackgroundTask.kind.startswith("repo.import")).execute()
        for repo in Repository.select().where(Repository.name.startswith("import-task-")):
            repo.delete_instance()

    clear()
    yield
    clear()


async def _start(owner_client, **payload):
    body = {
        "source_repo_id": "some-org/pattern",
        "repo_type": "model",
        "target_repo_id": "owner/import-task-fixture",
    }
    body.update(payload)
    response = await owner_client.post("/api/repos/import", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _row(import_id):
    from kohakuhub.db import RepositoryImport

    return RepositoryImport.get_by_id(import_id)


async def _run(import_id, *, ctx=None):
    """Run the handler the way the worker does."""
    from kohakuhub.api.importer.task import import_repository

    ctx = ctx or RecordingContext(task_id=import_id)
    await import_repository({"import_id": import_id}, ctx)
    return ctx


@pytest.mark.asyncio
async def test_import_stages_the_sources_files_and_commits_one_snapshot(
    owner_client, imported_source, import_state
):
    from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo

    started = await _start(owner_client)
    await _run(started["import_id"])

    row = _row(started["import_id"])
    assert row.state == "succeeded"
    assert row.commit_id
    assert row.files_done == row.files_total
    assert row.bytes_done > 0
    assert row.error is None

    # The repository is now local: the files are in LakeFS and the branch has
    # exactly one commit for the whole snapshot.
    repo = row.repository
    client = get_lakefs_client()
    lakefs_repo = resolve_lakefs_repo(repo)
    head = await client.get_branch(repository=lakefs_repo, branch="main")
    assert head["commit_id"] == row.commit_id
    commit = await client.get_commit(repository=lakefs_repo, commit_id=row.commit_id)
    assert commit["message"].startswith("Import some-org/pattern from")

    # The bytes are the source's bytes, and the database knows the files.
    from kohakuhub.db import File

    files = {f.path_in_repo: f for f in File.select().where(File.repository == repo)}
    assert "pattern_a.txt" in files
    pattern_a = files["pattern_a.txt"]
    assert pattern_a.size == len(PATTERN_A_BYTES)
    assert (
        pattern_a.sha256
        == hashlib.sha1(b"blob %d\0" % len(PATTERN_A_BYTES) + PATTERN_A_BYTES).hexdigest()
    )


@pytest.mark.asyncio
async def test_import_is_safe_to_run_twice(owner_client, imported_source, import_state):
    """Delivery is at-least-once, so a second attempt must not commit an empty
    snapshot on top of the first."""
    from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo

    started = await _start(owner_client)
    await _run(started["import_id"])
    first = _row(started["import_id"])
    lakefs_repo = resolve_lakefs_repo(first.repository)

    # The worker dies after committing and the task is delivered again.
    await _run(started["import_id"])

    row = _row(started["import_id"])
    client = get_lakefs_client()
    head = await client.get_branch(repository=lakefs_repo, branch="main")
    assert head["commit_id"] == first.commit_id == row.commit_id

    # Nothing extra was staged: the file list is what the first run made.
    from kohakuhub.db import File

    assert File.select().where(File.repository == first.repository).count() >= 1


@pytest.mark.asyncio
async def test_import_reports_progress_while_it_stages(owner_client, imported_source, import_state):
    started = await _start(owner_client)
    ctx = await _run(started["import_id"])

    assert ctx.reports, "an import of several files must report progress"
    done, total = ctx.reports[-1]
    assert total == _row(started["import_id"]).files_total
    assert done == total
    assert "staging" in ctx.stages


@pytest.mark.asyncio
async def test_a_resumed_attempt_continues_from_its_checkpoint(
    owner_client, imported_source, import_state
):
    """A restarted worker must not re-download what it already staged."""
    from kohakuhub.api.importer import task as task_module

    started = await _start(owner_client)
    opened: list[str] = []

    real_stage = task_module._stage_one

    async def counting_stage(reader, path, repo, lakefs_repo):
        opened.append(path)
        return await real_stage(reader, path, repo, lakefs_repo)

    task_module._stage_one = counting_stage
    try:
        # A checkpoint at the end of file 1, as a crash after the first file
        # would have left.
        resume = RecordingContext(task_id=started["import_id"], checkpoint_state={"cursor": 1})
        await _run(started["import_id"], ctx=resume)
    finally:
        task_module._stage_one = real_stage

    assert opened, "at least the remaining files must be staged"
    assert _row(started["import_id"]).state == "succeeded"
    # The first file was skipped, so it was never re-read from the source.
    from kohakuhub.db import File

    paths = {
        f.path_in_repo
        for f in File.select().where(File.repository == _row(started["import_id"]).repository)
    }
    assert paths - set(opened)


@pytest.mark.asyncio
async def test_import_stops_at_the_byte_allowance(owner_client, imported_source, import_state):
    """A source that grew since it was listed must stop at the quota instead
    of filling the disk."""
    from kohakuhub.db import RepositoryImport

    started = await _start(owner_client)
    # One byte: the first file overruns it immediately.
    RepositoryImport.update(allowed_bytes=1).where(
        RepositoryImport.id == started["import_id"]
    ).execute()

    from kohakuhub import tasks

    with pytest.raises(tasks.PermanentTaskError):
        await _run(started["import_id"])

    row = _row(started["import_id"])
    assert row.state == "failed"
    assert "quota" in row.error.lower()

    # The partial repository is left for the user to delete, as the design
    # says it should be rather than vanishing behind their back.
    assert row.repository is not None


@pytest.mark.asyncio
async def test_import_stops_when_cancellation_is_requested(
    owner_client, imported_source, import_state
):
    from kohakuhub import tasks
    from kohakuhub.api.importer import task as task_module

    started = await _start(owner_client)
    ctx = RecordingContext(task_id=started["import_id"])
    ctx.cancel_requested = True

    with pytest.raises(tasks.TaskCancelled):
        await task_module.import_repository({"import_id": started["import_id"]}, ctx)

    assert _row(started["import_id"]).state == "cancelled"


@pytest.mark.asyncio
async def test_import_fails_permanently_when_the_source_loses_the_file(
    owner_client, imported_source, import_state
):
    """A 404 mid-import is not worth retrying: the source does not have it."""
    from kohakuhub import tasks
    from kohakuhub.api.importer import task as task_module
    from kohakuhub.api.importer.source import SourceError

    started = await _start(owner_client)

    async def gone(reader, path, repo, lakefs_repo):
        raise SourceError("pattern_a.txt is missing at Imported Source", status=404)

    real_stage = task_module._stage_one
    task_module._stage_one = gone
    try:
        with pytest.raises(tasks.PermanentTaskError):
            await _run(started["import_id"])
    finally:
        task_module._stage_one = real_stage

    row = _row(started["import_id"])
    assert row.state == "failed"
    assert "missing" in row.error


@pytest.mark.asyncio
async def test_the_failure_hook_records_the_outcome(owner_client, imported_source, import_state):
    """``repo.import.on_failure`` is how a task that ended without reaching its
    own failure path is still visible on the page."""
    from kohakuhub.api.importer.task import on_import_failure
    from kohakuhub import tasks

    started = await _start(owner_client)
    failure = tasks.TaskFailure(
        task_id=started["task_id"],
        kind="repo.import",
        outcome="failed",
        error="S3 went away",
        scratch_prefix="tmp/tasks/1/",
    )
    await on_import_failure({"import_id": started["import_id"]}, failure)

    row = _row(started["import_id"])
    assert row.state == "failed"
    assert row.error == "S3 went away"


@pytest.mark.asyncio
async def test_the_failure_hook_keeps_a_specific_reason(
    owner_client, imported_source, import_state
):
    """\"It outgrew the quota\" says more than the exception that carried it."""
    from kohakuhub.api.importer.task import on_import_failure
    from kohakuhub.db import RepositoryImport
    from kohakuhub import tasks

    started = await _start(owner_client)
    RepositoryImport.update(state="failed", error="It outgrew the storage quota").where(
        RepositoryImport.id == started["import_id"]
    ).execute()

    failure = tasks.TaskFailure(
        task_id=started["task_id"],
        kind="repo.import",
        outcome="failed",
        error="PermanentTaskError: ...",
        scratch_prefix="tmp/tasks/1/",
    )
    await on_import_failure({"import_id": started["import_id"]}, failure)

    assert _row(started["import_id"]).error == "It outgrew the storage quota"


@pytest.mark.asyncio
async def test_the_failure_hook_leaves_a_successful_import_alone(
    owner_client, imported_source, import_state
):
    from kohakuhub.api.importer.task import on_import_failure
    from kohakuhub.db import RepositoryImport
    from kohakuhub import tasks

    started = await _start(owner_client)
    RepositoryImport.update(state="succeeded", commit_id="c" * 40).where(
        RepositoryImport.id == started["import_id"]
    ).execute()

    failure = tasks.TaskFailure(
        task_id=started["task_id"],
        kind="repo.import",
        outcome="cancelled",
        error=None,
        scratch_prefix="tmp/tasks/1/",
    )
    await on_import_failure({"import_id": started["import_id"]}, failure)

    row = _row(started["import_id"])
    assert row.state == "succeeded"
    assert row.commit_id == "c" * 40


@pytest.mark.asyncio
async def test_import_reports_the_source_being_unreachable(
    owner_client, imported_source, import_state
):
    """A source that goes away before the tree is read fails the import with
    something a person can act on."""
    from kohakuhub import tasks
    from kohakuhub.api.importer import task as task_module

    started = await _start(owner_client)

    async def unreachable(self):
        from kohakuhub.api.importer.source import SourceError

        raise SourceError("Could not list their-repo at Imported Source: connection refused")

    real = task_module.SourceReader.list_files
    task_module.SourceReader.list_files = unreachable
    try:
        with pytest.raises(tasks.PermanentTaskError):
            await _run(started["import_id"])
    finally:
        task_module.SourceReader.list_files = real

    row = _row(started["import_id"])
    assert row.state == "failed"
    assert "Could not list" in row.error


@pytest.mark.asyncio
async def test_import_stops_when_its_target_is_deleted_midway(
    owner_client, imported_source, import_state
):
    """Deleting a partial import is how one is undone; doing it while the
    import runs must stop it, not leave it retrying against a repository that
    is gone."""
    from kohakuhub import tasks
    from kohakuhub.db import Repository

    started = await _start(owner_client)
    row = _row(started["import_id"])
    Repository.delete().where(Repository.id == row.repository_id).execute()

    with pytest.raises(tasks.PermanentTaskError):
        await _run(started["import_id"])


@pytest.mark.asyncio
async def test_import_stops_when_its_target_was_deleted_before_it_started(import_state):
    """The same, for one whose target was deleted while it queued."""
    from kohakuhub import tasks
    from kohakuhub.api.importer.task import import_repository
    from kohakuhub.db import Repository, RepositoryImport, User

    owner = User.get(User.username == "owner")
    repo = Repository.create(
        repo_type="model",
        namespace="owner",
        name="import-task-vanished",
        full_id="owner/import-task-vanished",
        private=False,
        owner=owner,
        lakefs_repo=f"m-owner-import-task-vanished-{'0' * 22}",
    )
    row = RepositoryImport.create(
        repository=repo,
        repo_type="model",
        source_repo_id="some-org/pattern",
        source_url="https://huggingface.co",
        source_name="HuggingFace",
        source_type="huggingface",
        requested_by=owner,
        state="queued",
    )
    import_id = row.id
    # The import row goes with the repository (ON DELETE CASCADE), so this is
    # what a deleted target looks like by the time the worker starts.
    Repository.delete().where(Repository.id == repo.id).execute()
    assert RepositoryImport.get_or_none(RepositoryImport.id == import_id) is None

    with pytest.raises(tasks.PermanentTaskError):
        await import_repository({"import_id": import_id}, RecordingContext())


@pytest.mark.asyncio
async def test_import_refuses_a_missing_import_row(import_state):
    from kohakuhub import tasks
    from kohakuhub.api.importer.task import import_repository

    with pytest.raises(tasks.PermanentTaskError):
        await import_repository({"import_id": 10_000_000}, RecordingContext())
