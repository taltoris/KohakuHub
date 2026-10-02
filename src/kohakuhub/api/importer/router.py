"""The import API: pull a repository from a fallback source into this hub.

Three endpoints, all under ``cfg.app.api_base``:

- ``POST /repos/import`` — validate, reserve the target, enqueue the work
- ``GET  /repos/import/{task_id}`` — follow it
- ``POST /repos/import/{task_id}/cancel`` — ask it to stop

plus ``GET /repos/import/latest?repo_id=…``, which is what the repository page
needs: a repository whose import did not finish is a partial copy and has to
say so rather than look like an empty repository.

Everything that can be decided before a byte moves is decided in the request,
so a user gets an immediate answer instead of a task that fails ten minutes
later:

1. **Where it lands.** ``target_repo_id``, or ``<your username>/<source
   name>``. The name must be free: importing over a repository would merge
   two repositories' histories, which is not what an import means.
2. **Whether the source has it.** A probe against the configured chain, so
   "you do not have access to this gated repository" answers in one second
   rather than after twenty gigabytes.
3. **Whether it fits.** The source's own tree gives every size; the sum is
   checked against the target namespace's quota *before* the task starts.
   The task re-checks as bytes arrive (``RepositoryImport.allowed_bytes``),
   because a repository can grow between the two.

The source is pinned here and stored on the import row. Walking the fallback
chain per file would be a bug: a repository that answers at two sources is
two different repositories (#75), and a snapshot half from each is a copy of
neither.

The CLI is not part of this. ``kohub-cli`` lives in its own repository
(``KohakuBlueleaf/Kohub-CLI``); this is the API the web UI drives.
"""

import json
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from kohakuhub import tasks
from kohakuhub.api.fallback.config import get_enabled_sources
from kohakuhub.api.fallback.operations import try_fallback_info
from kohakuhub.api.importer.source import SourceError, SourceReader
from kohakuhub.api.importer.task import KIND, import_summary, latest_import, set_state
from kohakuhub.api.quota.util import check_quota
from kohakuhub.api.repo.routers.crud import create_repository_for_import
from kohakuhub.api.repo.utils.hf import HFErrorCode
from kohakuhub.auth.dependencies import get_current_user, get_external_tokens, get_optional_user
from kohakuhub.auth.permissions import (
    check_namespace_permission,
    check_repo_read_permission,
    check_repo_write_permission,
)
from kohakuhub.config import cfg
from kohakuhub.crypto import encrypt_token
from kohakuhub.db import BackgroundTask, RepositoryImport, User
from kohakuhub.db_operations import get_merged_external_tokens, get_repository
from kohakuhub.logger import get_logger
from kohakuhub.usage import namespace_used

logger = get_logger("IMPORT_API")
router = APIRouter()

RepoType = Literal["model", "dataset", "space"]

# A repository this many files long is refused rather than imported: the
# cursor checkpoint, the staging loop and the commit are all per-file, and a
# job nobody can reason about is worse than a clear "not supported".
MAX_FILES = 200_000


class ImportPayload(BaseModel):
    """What the UI (or any client) asks for."""

    source_repo_id: str = Field(..., description="namespace/name at the source")
    target_repo_id: Optional[str] = Field(
        None, description="namespace/name here; defaults to <you>/<source name>"
    )
    repo_type: RepoType = "model"
    source: Optional[str] = Field(
        None, description="Base URL of the fallback source to read; default: whichever has it"
    )
    revision: str = Field("main", description="Branch or commit to snapshot")
    private: bool = False
    max_bytes: Optional[int] = Field(None, ge=0, description="Refuse an import bigger than this")


def _split_repo_id(repo_id: str) -> tuple[str, str]:
    namespace, _, name = repo_id.partition("/")
    if not namespace or not name or "/" in name:
        raise HTTPException(
            400,
            detail={"error": f"Not a repository id: {repo_id} (expected namespace/name)"},
        )
    return namespace, name


def _target(repo_id: str | None, source_repo_id: str, user: User) -> str:
    if repo_id:
        return repo_id.strip().strip("/")
    _, _, source_name = source_repo_id.partition("/")
    return f"{user.username}/{source_name}"


def _state_response(import_row: RepositoryImport, task=None) -> dict:
    """The import as an API payload (``task.import_summary`` plus the task id)."""
    return import_summary(import_row, task)


def _task_payload(import_row: RepositoryImport) -> dict:
    return {"import_id": import_row.id}


def _find_task(import_row: RepositoryImport):
    """The newest ``repo.import`` task of this import.

    Matched on the payload's exact text, which ``enqueue`` writes with
    ``json.dumps`` and this module is the only writer of. A ``LIKE`` on the
    number would also match a longer id (``12`` in ``123``).
    """
    return (
        BackgroundTask.select()
        .where(BackgroundTask.kind == KIND)
        .where(BackgroundTask.payload == json.dumps(_task_payload(import_row)))
        .order_by(BackgroundTask.id.desc())
        .first()
    )


def _import_for_task(task: BackgroundTask) -> RepositoryImport | None:
    try:
        import_id = json.loads(task.payload or "{}").get("import_id")
    except ValueError:
        return None
    if import_id is None:
        return None
    return RepositoryImport.get_or_none(RepositoryImport.id == import_id)


def _allowed_bytes(namespace: str, private: bool) -> int | None:
    """The storage this import may use; ``None`` when the quota is unlimited.

    From the namespace's quota and usage at acceptance time. The task checks
    its running total against it, so a source that grew since it was listed
    stops at the quota instead of filling the disk.
    """
    owner = User.get_or_none(User.username == namespace)
    if owner is None:
        return None
    quota = owner.private_quota_bytes if private else owner.public_quota_bytes
    if quota is None:
        return None
    return max(0, quota - namespace_used(namespace, private))


@router.post("/repos/import")
async def start_import(
    payload: ImportPayload,
    request: Request,
    user: User = Depends(get_current_user),
):
    """Accept an import: validate it, reserve the target, enqueue the task."""
    source_repo_id = payload.source_repo_id.strip().strip("/")
    source_namespace, source_name = _split_repo_id(source_repo_id)
    target_repo_id = _target(payload.target_repo_id, source_repo_id, user)
    target_namespace, target_name = _split_repo_id(target_repo_id)

    check_namespace_permission(target_namespace, user)

    # Refuse rather than merge: an import is a snapshot, and silently merging
    # two histories is the kind of thing nobody forgives.
    if get_repository(payload.repo_type, target_namespace, target_name) is not None:
        raise HTTPException(
            409,
            detail={
                "error": (
                    f"{target_repo_id} already exists. Importing into an existing "
                    "repository would merge two histories; pick another name or "
                    "delete the repository first."
                ),
                "repo_id": target_repo_id,
            },
            headers={"X-Error-Code": HFErrorCode.REPO_EXISTS},
        )

    # The user's own tokens for external sources, plus whatever this request
    # carried in its Authorization header. Merged here because the worker has
    # no request of its own.
    user_tokens = get_merged_external_tokens(user, get_external_tokens(request))
    sources = get_enabled_sources(target_namespace, user_tokens=user_tokens)
    if not sources:
        raise HTTPException(
            400,
            detail={
                "error": (
                    "No fallback source is configured, so there is nothing to "
                    "import from. An administrator configures them."
                )
            },
        )

    if payload.source:
        pinned = next(
            (s for s in sources if s["url"].rstrip("/") == payload.source.rstrip("/")), None
        )
        if pinned is None:
            raise HTTPException(
                400,
                detail={"error": f"{payload.source} is not an enabled fallback source"},
            )
        sources = [pinned]

    # Probe: which source has it, and may this user read it? The one second a
    # gated repository fails in, instead of twenty gigabytes.
    probe = await try_fallback_info(
        payload.repo_type,
        source_namespace,
        source_name,
        user_tokens=user_tokens,
        user=user,
    )
    if probe is None:
        raise HTTPException(
            404,
            detail={
                "error": (
                    f"{source_repo_id} is not on any configured fallback source. "
                    "Check the name, or add a token for the source that has it."
                ),
                "repo_id": source_repo_id,
            },
        )
    if not isinstance(probe, dict):
        # BIND_AND_PROPAGATE: a source has the repository but refused the
        # request (disabled here, or a revision it does not have).
        raise HTTPException(
            probe.status_code,
            detail={"error": f"The source refused to describe {source_repo_id}"},
        )

    source_url = probe.get("_source_url")
    pinned = next((s for s in sources if s["url"] == source_url), None)
    if pinned is None:
        raise HTTPException(
            502, detail={"error": f"The source {source_url} answered but is not configured"}
        )

    # What the source holds, and whether it fits.
    reader = SourceReader(
        pinned, payload.repo_type, source_namespace, source_name, payload.revision
    )
    try:
        files = await reader.list_files()
    except SourceError as e:
        raise HTTPException(e.status or 502, detail={"error": e.message})

    total_bytes = sum(f.size or 0 for f in files)
    if len(files) > MAX_FILES:
        raise HTTPException(
            400,
            detail={
                "error": (
                    f"{source_repo_id} has {len(files):,} files, more than the "
                    f"{MAX_FILES:,} one import stages. Import it in parts, or raise "
                    "the limit if the target can take it."
                )
            },
        )
    if payload.max_bytes is not None and total_bytes > payload.max_bytes:
        raise HTTPException(
            400,
            detail={
                "error": (
                    f"{source_repo_id} is {total_bytes:,} bytes, over the "
                    f"{payload.max_bytes:,} you allowed"
                )
            },
        )

    owner = User.get_or_none(User.username == target_namespace)
    allowed, reason = check_quota(
        target_namespace,
        total_bytes,
        is_private=payload.private,
        is_org=bool(owner is not None and owner.is_org),
    )
    if not allowed:
        raise HTTPException(
            413,
            detail={
                "error": (
                    f"{source_repo_id} does not fit: {reason}. Nothing was "
                    "downloaded; free space or raise the quota and try again."
                )
            },
        )

    # Reserve the target. The repository exists from here on, empty until the
    # task commits; a failure leaves it to be deleted rather than hidden.
    repo_row = await create_repository_for_import(
        repo_type=payload.repo_type,
        namespace=target_namespace,
        name=target_name,
        private=payload.private,
    )

    import_row = RepositoryImport.create(
        repository=repo_row,
        repo_type=payload.repo_type,
        source_repo_id=source_repo_id,
        source_revision=payload.revision,
        source_url=source_url,
        source_name=probe.get("_source") or source_url,
        source_type=pinned.get("source_type", "huggingface"),
        # Only this import's own token; the source's configured one stays in
        # the fallback config and the task falls back to it.
        source_token=encrypt_token(pinned["token"]) if pinned.get("token") else None,
        requested_by=user,
        state="queued",
        files_total=len(files),
        bytes_total=total_bytes,
        allowed_bytes=_allowed_bytes(target_namespace, payload.private),
    )

    task_id = tasks.enqueue(KIND, _task_payload(import_row), dedupe_key=f"import:{import_row.id}")
    logger.info(
        f"Import {import_row.id}: {source_repo_id} -> {target_repo_id} "
        f"({len(files)} files, {total_bytes:,} bytes) as task {task_id}"
    )

    body = _state_response(import_row)
    body["task_id"] = task_id
    body["url"] = f"{cfg.app.base_url}/{payload.repo_type}s/{target_repo_id}"
    return body


@router.get("/repos/import/latest")
async def latest_import_state(
    repo_id: str,
    repo_type: RepoType = "model",
    user: User | None = Depends(get_optional_user),
):
    """What the newest import of this repository is doing, or what it did.

    The repository page asks this to decide between "importing", "import
    failed — delete it" and nothing at all. ``null`` when the repository was
    not imported.
    """
    namespace, name = _split_repo_id(repo_id)
    import_row = latest_import(repo_type, namespace, name)
    if import_row is None:
        return {"import": None}
    if import_row.repository is not None:
        # Reading an import is reading the repository it made: the
        # repository's own read permission covers it.
        check_repo_read_permission(import_row.repository, user)
    return {"import": _state_response(import_row, _find_task(import_row))}


@router.get("/repos/import/{task_id}")
async def import_state(task_id: int, user: User = Depends(get_current_user)):
    """Follow an import by the task id the start endpoint returned."""
    task = BackgroundTask.get_or_none(BackgroundTask.id == task_id)
    if task is None or task.kind != KIND:
        raise HTTPException(404, detail={"error": f"No import task {task_id}"})
    import_row = _import_for_task(task)
    if import_row is None:
        raise HTTPException(404, detail={"error": f"No import for task {task_id}"})
    return _state_response(import_row, task)


@router.post("/repos/import/{task_id}/cancel")
async def cancel_import(task_id: int, user: User = Depends(get_current_user)):
    """Ask an import to stop.

    A queued task is cancelled at once. A running one is flagged and stops
    within the worker's flush interval; what it staged stays staged, and the
    partial repository stays for the user to inspect or delete.
    """
    task = BackgroundTask.get_or_none(BackgroundTask.id == task_id)
    if task is None or task.kind != KIND:
        raise HTTPException(404, detail={"error": f"No import task {task_id}"})
    import_row = _import_for_task(task)
    if import_row is None:
        raise HTTPException(404, detail={"error": f"No import for task {task_id}"})
    if import_row.repository is not None:
        check_repo_write_permission(import_row.repository, user)

    outcome = tasks.request_cancel(task_id)
    if outcome == tasks.CANCELLED:
        # Nothing was running; record the outcome here so the page sees it
        # now instead of when an on_failure hook eventually runs.
        set_state(import_row, "cancelled", stage=None, error="Cancelled before it started")
    elif outcome is None:
        raise HTTPException(409, detail={"error": f"Import task {task_id} has already finished"})
    return _state_response(import_row, task)
