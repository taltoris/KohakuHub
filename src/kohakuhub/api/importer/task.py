"""The ``repo.import`` background task: pull a source repository into this hub.

One import is one task. The task stages every file into the target's LakeFS
repository and makes **one** commit for the whole snapshot: replaying the
source's commit history into LakeFS is a much bigger job, and a snapshot is
what an import means here — re-importing later commits the difference, so the
repository still gains history over time.

Why a task rather than the request that asked for it:

- importing a model is minutes to hours of downloading; the request that
  asked for it returns a task id and the repository page follows it
- the worker holds a lease, so a restart resumes: ``TaskContext.checkpoint``
  records how far the file list got, and staging is idempotent (an unchanged
  file stages to the same object), so a resumed attempt re-stages a file at
  worst
- at-least-once delivery means the handler must be safe to run twice: the
  commit is the fence. An attempt that finds nothing left to stage reports
  success and records the branch head it finds, so a retry after the commit
  cannot make a second empty commit

What it reuses rather than reimplements:

- ``process_regular_file`` / ``process_lfs_file`` (the commit endpoint's own
  staging, so an imported file is byte-for-byte what an uploaded one is,
  including the LFS garbage-collection claim)
- ``commit_changes`` + ``record_commits`` (the only place that brings File
  rows, LFS history, head references and the ``main`` usage counters up to
  date from a commit — reverts and resets use it too)

Failure keeps the partial repository: a half-downloaded copy is not worth
deleting behind the user's back, and design asks for it to be visible and
deletable instead. The row says ``failed`` and the repository page shows it.
"""

import asyncio
import base64
import hashlib
import os
import tempfile
from datetime import datetime
from typing import Any

from kohakuhub import tasks, usage
from kohakuhub.api.commit.availability import entries as lakefs_entries
from kohakuhub.api.commit.records import record_commits
from kohakuhub.api.commit.routers.operations import (
    process_lfs_file,
    process_regular_file,
)
from kohakuhub.api.importer.source import SourceError, SourceReader
from kohakuhub.async_utils import run_in_s3_executor
from kohakuhub.config import cfg
from kohakuhub.crypto import decrypt_token
from kohakuhub.db import Repository, RepositoryImport, utcnow
from kohakuhub.db_operations import get_repository, should_use_lfs
from kohakuhub.lfs_gc import lfs_key
from kohakuhub.logger import get_logger
from kohakuhub.utils.lakefs import get_lakefs_client, resolve_lakefs_repo
from kohakuhub.utils.s3 import get_s3_client

logger = get_logger("IMPORT")

KIND = "repo.import"
MAIN = usage.MAIN  # the branch an import lands on, and the one usage counts
# Files staged between two checkpoints: a crash re-does at most this many.
CHECKPOINT_EVERY = 25
# Paths named in a log line about a failed file.
MAX_LISTED_IN_ERROR = 5


def _now() -> datetime:
    return utcnow()


def set_state(import_row: RepositoryImport, state: str, **fields: Any) -> None:
    """Update the row people read (the page, the status endpoint).

    Best effort: the row goes with its repository (``ON DELETE CASCADE``), so
    a repository deleted mid-import leaves nothing to update. Losing the row
    is a reason to stop the import, not to fail the task with a traceback.
    """
    import_row.state = state
    for key, value in fields.items():
        setattr(import_row, key, value)
    import_row.updated_at = _now()
    try:
        import_row.save()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Could not record the state of import {import_row.id}: {e}")


def source_gone(import_row: RepositoryImport) -> bool:
    """Whether the repository this import is filling still exists.

    Deleting a partial import is how a failed one is undone, and doing it
    while the import is still running is not a mistake the import should keep
    retrying against: the row goes with the repository, so there is nothing
    left to record either.
    """
    return Repository.get_or_none(Repository.id == import_row.repository_id) is None


def _decrypt(stored: str | None) -> str | None:
    if not stored:
        return None
    try:
        return decrypt_token(stored)
    except Exception as e:  # a key rotation must not strand the import
        logger.warning(f"Could not decrypt the stored source token: {e}")
        return None


def _source_dict(import_row: RepositoryImport) -> dict:
    """The pinned source, with the token this import stored (or the source's)."""
    from kohakuhub.api.fallback.config import get_source_by_url

    source = get_source_by_url(import_row.source_url) or {}
    source = {
        "url": import_row.source_url,
        "name": import_row.source_name,
        "source_type": import_row.source_type,
        "priority": source.get("priority", 100),
    }
    source["token"] = _decrypt(import_row.source_token) or source.get("token")
    return source


def _upload_lfs(local_path: str, oid: str) -> int:
    """Put a staged file's bytes where an LFS object lives. Returns its size.

    ``upload_fileobj`` rather than a presigned PUT: this is the backend
    holding the bytes already, it switches to multipart on its own, and a
    multi-gigabyte object must not be assembled in memory.
    """
    key = lfs_key(oid)
    size = os.path.getsize(local_path)
    s3 = get_s3_client()
    with open(local_path, "rb") as body:
        s3.upload_fileobj(body, cfg.s3.bucket, key)
    logger.info(f"Stored LFS object {oid[:8]} ({size:,} bytes) at {key}")
    return size


async def _stage_one(reader: SourceReader, path: str, repo: Repository, lakefs_repo: str) -> int:
    """Download one file and stage it in the target. Returns the bytes staged."""
    chunks, _ = await reader.open(path)
    digest = hashlib.sha256()
    size = 0
    handle = tempfile.NamedTemporaryFile(delete=False)
    local_path = handle.name
    try:
        for chunk in chunks:
            digest.update(chunk)
            size += len(chunk)
            handle.write(chunk)
        handle.flush()
        handle.close()
        oid = digest.hexdigest()

        if should_use_lfs(repo, path, size):
            await run_in_s3_executor(_upload_lfs, local_path, oid)
            await process_lfs_file(
                path=path,
                oid=oid,
                size=size,
                algo="sha256",
                repo=repo,
                lakefs_repo=lakefs_repo,
                revision=MAIN,
            )
        else:
            with open(local_path, "rb") as body:
                content = body.read()
            await process_regular_file(
                path=path,
                content_b64=base64.b64encode(content).decode("ascii"),
                encoding="base64",
                repo=repo,
                lakefs_repo=lakefs_repo,
                revision=MAIN,
            )
        return size
    finally:
        try:
            os.unlink(local_path)
        except OSError:  # pragma: no cover - already gone
            pass


async def _commit_snapshot(
    client,
    lakefs_repo: str,
    repo: Repository,
    import_row: RepositoryImport,
    paths: list[str],
) -> str:
    """Make the import's one commit and record what it holds.

    ``record_commits`` is the authority for the database: File rows, LFS
    history, head references, and the usage counters of ``main``. It reads
    the paths from LakeFS, so what is recorded is what was committed.
    """
    message = f"Import {import_row.source_repo_id} from {import_row.source_name}"
    description = (
        f"Snapshot of {import_row.source_repo_id}@{import_row.source_revision} "
        f"imported from {import_row.source_url} ({len(paths)} files)"
    )
    result = await client.commit(
        repository=lakefs_repo,
        branch=MAIN,
        message=message,
        metadata={"description": description},
    )
    commit_id = result["id"]
    logger.info(f"Committed import of {import_row.source_repo_id} as {commit_id[:8]}")

    # Verify it is readable before recording anything against it (LakeFS needs
    # a moment for a large commit), exactly as the commit endpoint does.
    for attempt in range(120):
        try:
            await client.get_commit(repository=lakefs_repo, commit_id=commit_id)
            break
        except Exception:
            if attempt == 119:
                logger.warning(f"Commit {commit_id[:8]} is not readable yet; recording anyway")
                break
            await asyncio.sleep(0.5)

    author = import_row.requested_by or repo.owner
    round_entries = await lakefs_entries(client, lakefs_repo, commit_id, paths)
    await record_commits(
        client,
        lakefs_repo,
        repo,
        MAIN,
        [(commit_id, round_entries)],
        author,
        message,
        description,
    )
    return commit_id


async def on_import_failure(payload: dict[str, Any], failure: tasks.TaskFailure) -> None:
    """Record that an import ended failed or cancelled.

    The task system runs this as its own task (``repo.import.on_failure``)
    when an import ends without succeeding, so it must be trivial and
    re-runnable. A handler that already recorded a specific reason keeps it:
    "the import outgrew the storage quota" says more than the exception that
    carried it.
    """
    import_row = RepositoryImport.get_or_none(RepositoryImport.id == payload.get("import_id"))
    if import_row is None or import_row.state == "succeeded":
        return
    if import_row.state in ("failed", "cancelled") and import_row.error:
        return
    error = failure.error or "The import did not finish. See the task log for details."
    if failure.outcome == "cancelled":
        set_state(import_row, "cancelled", stage=None, error=error)
    else:
        set_state(import_row, "failed", stage=None, error=error)
    logger.warning(
        f"Import {import_row.id} of {import_row.source_repo_id} {failure.outcome}: {error}"
    )


@tasks.task(KIND, queue="imports", max_attempts=3, timeout=24 * 3600, on_failure=on_import_failure)
async def import_repository(payload: dict[str, Any], ctx: tasks.TaskContext) -> None:
    """Stage every file of the source, then commit the snapshot.

    Stages into the target's LakeFS staging area and commits once at the end.
    The order of the source's tree is the cursor: a resumed attempt re-stages
    from the last checkpoint, and because staging an unchanged file reaches
    the same object, re-doing a file is safe (``process_regular_file`` even
    skips it).
    """
    import_id = payload.get("import_id")
    import_row = (
        RepositoryImport.get_or_none(RepositoryImport.id == import_id)
        if import_id is not None
        else None
    )
    if import_row is None:
        raise tasks.PermanentTaskError(f"Import {import_id} does not exist")

    if import_row.state == "succeeded":
        ctx.log("INFO", f"Import {import_id} already succeeded; nothing to do")
        return

    repo = Repository.get_or_none(Repository.id == import_row.repository_id)
    if repo is None:  # the target was deleted while the import waited
        logger.info(f"Import {import_id} stopped: its target repository was deleted")
        raise tasks.PermanentTaskError("The target repository was deleted")

    namespace, _, name = import_row.source_repo_id.partition("/")
    if not namespace or not name:
        message = f"Not a repository id: {import_row.source_repo_id}"
        set_state(import_row, "failed", stage=None, error=message)
        raise tasks.PermanentTaskError(message)

    lakefs_repo = resolve_lakefs_repo(repo)
    client = get_lakefs_client()
    reader = SourceReader(
        _source_dict(import_row),
        repo.repo_type,
        namespace,
        name,
        import_row.source_revision,
    )

    set_state(import_row, "running", stage="listing", error=None)
    ctx.stage("listing")
    try:
        files = await reader.list_files()
    except SourceError as e:
        set_state(import_row, "failed", stage=None, error=e.message)
        raise tasks.PermanentTaskError(e.message)

    total_bytes = sum(f.size or 0 for f in files)
    import_row.files_total = len(files)
    import_row.bytes_total = total_bytes
    import_row.save(only=[RepositoryImport.files_total, RepositoryImport.bytes_total])
    ctx.log("INFO", f"{len(files)} file(s), {total_bytes:,} bytes at {reader.source_name}")

    if not files:
        # An empty source is a valid (if useless) repository: commit nothing.
        set_state(import_row, "succeeded", stage=None, files_done=0, bytes_done=0)
        ctx.log("WARNING", "The source has no files; nothing to import")
        return

    paths = [f.path for f in files]
    cursor = 0
    if isinstance(ctx.checkpoint_state, dict):
        cursor = int(ctx.checkpoint_state.get("cursor") or 0)
        if cursor:
            ctx.log("INFO", f"Resuming: {cursor} file(s) were already staged")

    ctx.stage("staging")
    set_state(import_row, "running", stage="staging")
    bytes_done = import_row.bytes_done

    while cursor < len(files):
        if ctx.cancel_requested:
            set_state(import_row, "cancelled", stage=None, error="Cancelled by an administrator")
            raise tasks.TaskCancelled()

        if source_gone(import_row):
            # Deleted mid-import, which is how a partial import is undone.
            # Stop rather than staging into a repository that is gone.
            logger.info(f"Import {import_row.id} stopped: its target was deleted")
            raise tasks.PermanentTaskError("The target repository was deleted")

        entry = files[cursor]
        try:
            staged = await _stage_one(reader, entry.path, repo, lakefs_repo)
        except SourceError as e:
            set_state(import_row, "failed", stage=None, error=e.message)
            raise tasks.PermanentTaskError(e.message)
        except Exception as e:
            # A staging failure (LakeFS, S3) is retryable: leave the row
            # running and let the retry resume from the last checkpoint.
            set_state(import_row, "running", error=f"Staging {entry.path} failed: {e}")
            raise

        bytes_done += staged
        cursor += 1
        ctx.progress(cursor, len(files))

        if import_row.allowed_bytes is not None and bytes_done > import_row.allowed_bytes:
            message = (
                f"The import outgrew the storage quota: {bytes_done:,} bytes staged, "
                f"{import_row.allowed_bytes:,} allowed. Delete the partial repository, "
                "free space, and import again."
            )
            set_state(import_row, "failed", stage=None, error=message)
            raise tasks.PermanentTaskError(message)

        if cursor % CHECKPOINT_EVERY == 0 or cursor == len(files):
            ctx.checkpoint({"cursor": cursor})
            import_row.files_done = cursor
            import_row.bytes_done = bytes_done
            import_row.save(only=[RepositoryImport.files_done, RepositoryImport.bytes_done])

    ctx.stage("committing")
    set_state(import_row, "running", stage="committing", files_done=cursor, bytes_done=bytes_done)
    try:
        commit_id = await _commit_snapshot(client, lakefs_repo, repo, import_row, paths)
    except Exception as e:
        if "no changes" in str(e).lower() or "nothing to commit" in str(e).lower():
            # A previous attempt committed already: the snapshot is in place.
            head = await client.get_branch(repository=lakefs_repo, branch=MAIN)
            commit_id = head["commit_id"]
            ctx.log("INFO", f"Nothing left to stage; the snapshot is {commit_id[:8]}")
        else:
            set_state(
                import_row,
                "failed",
                stage=None,
                error=f"The snapshot could not be committed: {e}",
            )
            raise

    set_state(
        import_row,
        "succeeded",
        stage=None,
        commit_id=commit_id,
        files_done=len(files),
        bytes_done=bytes_done,
        error=None,
    )
    ctx.log(
        "INFO",
        f"Imported {len(files)} file(s) ({bytes_done:,} bytes) as {commit_id[:8]}",
    )


def import_summary(import_row: RepositoryImport, task=None) -> dict:
    """One import as an API payload: state, progress, and what went wrong.

    Shared by the repository page (which gets it on the repository info it
    already fetches) and by the import endpoints (which also poll it), so the
    two can never disagree about what a state looks like.
    """
    body = {
        "import_id": import_row.id,
        "repo_id": import_row.repository.full_id if import_row.repository else None,
        "repo_type": import_row.repo_type,
        "state": import_row.state,
        "stage": import_row.stage,
        "source": import_row.source_name,
        "source_url": import_row.source_url,
        "source_repo_id": import_row.source_repo_id,
        "source_revision": import_row.source_revision,
        "files_total": import_row.files_total,
        "files_done": import_row.files_done,
        "bytes_total": import_row.bytes_total,
        "bytes_done": import_row.bytes_done,
        "error": import_row.error,
        "commit_id": import_row.commit_id,
        "created_at": import_row.created_at.isoformat() if import_row.created_at else None,
        "updated_at": import_row.updated_at.isoformat() if import_row.updated_at else None,
        "task_id": None,
        "task_status": None,
        "eta_seconds": None,
    }
    if task is not None:
        # The task row is the live one: it carries the attempts, the stage of
        # the current attempt, and the log the admin task view shows.
        body["task_id"] = task.id
        body["task_status"] = task.status
        body["task_attempts"] = task.attempts
        body["task_max_attempts"] = task.max_attempts
        if task.progress_stage:
            body["stage"] = task.progress_stage
        body["eta_seconds"] = tasks.estimate_eta_seconds(task, utcnow())
    return body


def latest_import(repo_type: str, namespace: str, name: str) -> RepositoryImport | None:
    """The newest import of a repository, whatever it ended as."""
    repo = get_repository(repo_type, namespace, name)
    if repo is None:
        return None
    return (
        RepositoryImport.select()
        .where(RepositoryImport.repository == repo)
        .order_by(RepositoryImport.created_at.desc(), RepositoryImport.id.desc())
        .first()
    )
