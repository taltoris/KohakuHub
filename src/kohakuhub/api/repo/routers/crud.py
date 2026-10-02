"""Repository CRUD operations (create, delete, move)."""

import asyncio
import json
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from peewee import IntegrityError
from pydantic import BaseModel

from kohakuhub.config import cfg
from kohakuhub.async_utils import run_in_s3_executor
from kohakuhub.db import (
    Commit,
    File,
    Repository,
    StagingUpload,
    User,
    db,
    init_db,
)
from kohakuhub.db_operations import (
    delete_repository,
    get_organization,
    get_repository,
)
from kohakuhub.logger import get_logger
from kohakuhub.auth.dependencies import get_current_user, get_current_user_or_admin
from kohakuhub.auth.permissions import (
    check_namespace_permission,
    check_repo_delete_permission,
)
from kohakuhub.utils.lakefs import (
    allocate_lakefs_repo_name,
    get_lakefs_client,
    resolve_lakefs_repo,
)
from kohakuhub.utils.s3 import copy_s3_folder, delete_objects_with_prefix, get_s3_client
from kohakuhub.api.repo.utils.hf import (
    HFErrorCode,
    hf_error_response,
    hf_repo_not_found,
    hf_server_error,
)
from kohakuhub import usage
from kohakuhub.api.commit import squash
from kohakuhub.api.commit.records import OperationRefused
from kohakuhub.api.repo.utils import operation_lock
from kohakuhub.api.quota.util import check_quota
from kohakuhub.api.fallback.cache import get_cache as get_fallback_cache
from kohakuhub.api.validation import normalize_name
from kohakuhub.api.operation_capabilities import (
    ensure_repository_operation_enabled,
    require_repository_squash_enabled,
)

logger = get_logger("REPO")
router = APIRouter()
init_db()

RepoType = Literal["model", "dataset", "space"]


def _repo_exists_response(
    repo_type: str,
    full_id: str,
    *,
    message: Optional[str] = None,
) -> Response:
    """Build the 409 response emitted when a repo already exists.

    `huggingface_hub.HfApi.create_repo(..., exist_ok=True)` swallows the 409 but
    immediately calls `r.json()["url"]` to build the returned `RepoUrl`, so the
    response body must be a JSON object containing a `url` field. The X-Error-*
    headers are preserved so clients following HF's header-based error protocol
    still get a structured error code.
    """
    body_message = message or f"Repository {full_id} already exists"
    body = json.dumps(
        {
            "url": f"{cfg.app.base_url}/{repo_type}s/{full_id}",
            "repo_id": full_id,
            "error": body_message,
        }
    )
    return Response(
        status_code=409,
        content=body,
        media_type="application/json",
        headers={
            "X-Error-Code": HFErrorCode.REPO_EXISTS,
            "X-Error-Message": body_message,
        },
    )


class CreateRepoPayload(BaseModel):
    """Payload for repository creation.

    Accepts the two on-the-wire shapes ``huggingface_hub`` clients use:

    * ``huggingface_hub<1`` sends ``{"private": true}`` directly.
    * ``huggingface_hub>=1.x`` resolves ``private=True`` into
      ``{"visibility": "private"}`` and no longer sends the legacy
      ``private`` field. The same dual-shape handling lives in the
      ``update_repo_settings`` payload (commit 19c2a5c); this mirror
      keeps the create endpoint compatible with both client versions.
    """

    type: RepoType = "model"
    name: str
    organization: Optional[str] = None
    private: Optional[bool] = None
    visibility: Optional[str] = None
    sdk: Optional[str] = None


def _is_lakefs_namespace_in_use_error(error: Exception, storage_namespace: str) -> bool:
    """Return True only for the exact LakeFS namespace-in-use error we can heal safely."""
    error_text = str(error)
    lowered = error_text.lower()
    return all(
        (
            "storage namespace already in use" in lowered,
            storage_namespace in error_text,
            "_lakefs/dummy" in error_text,
        )
    )


# Verbatim sentence that `huggingface_hub.HfApi.create_repo` matches against the
# response *body* to decide that a conflict is transient and worth retrying. The
# check has been present unchanged from 0.20.3 through 1.x:
#
#     while True:
#         r = get_session().post(path, headers=headers, json=payload)
#         if r.status_code == 409 and "Cannot create repo: another conflicting
#                                      operation is in progress" in r.text:
#             continue
#         break
#
# Getting the wording wrong is not a cosmetic issue: a 409 *without* it is
# treated as "repo already exists" by `create_repo(exist_ok=True)`, which then
# returns successfully and leaves the caller uploading into a repo that does not
# exist.
LAKEFS_CONFLICT_RETRY_MESSAGE = (
    "Cannot create repo: another conflicting operation is in progress"
)

# hf_hub's retry loop has no sleep and no attempt limit, so a client would spin
# at full request rate for as long as the conflict lasts. Holding the response
# briefly is the only way to pace it without changing the client.
LAKEFS_RECYCLING_HOLD_SECONDS = 2.0



def _is_lakefs_repo_id_taken_error(error: Exception) -> bool:
    """Return True for the LakeFS 409 raised while an id is still held.

    LakeFS answers `409 {"message":"error creating repository: not unique"}` when
    the repository id already exists in its KV store - including for a
    repository whose asynchronous deletion has not finished yet (issue #93).

    This is deliberately distinct from `_is_lakefs_namespace_in_use_error`: that
    one is a leftover S3 marker we can clean up and retry immediately, while
    this one only clears when LakeFS finishes its own cleanup.
    """
    lowered = str(error).lower()
    if "not unique" not in lowered:
        return False

    # `LakeFSRestClient._check_response` raises `httpx.HTTPStatusError`, so the
    # status is available structurally; prefer it over matching "409" in the
    # message, which could also appear inside a repository name or URL.
    status_code = getattr(getattr(error, "response", None), "status_code", None)
    if status_code is not None:
        return status_code == 409

    return "409" in lowered


def _repo_recycling_response(repo_type: str, full_id: str) -> Response:
    """Build the retryable 409 for a repo id LakeFS has not released yet.

    The body carries `LAKEFS_CONFLICT_RETRY_MESSAGE` so huggingface_hub retries
    on its own. `X-Error-Code` is informational: `hf_raise_for_status` has no 409
    branch, so no HF client consumes it for this status.
    """
    body = json.dumps(
        {
            "error": LAKEFS_CONFLICT_RETRY_MESSAGE,
            "repo_id": full_id,
            "repo_type": repo_type,
        }
    )
    return Response(
        status_code=409,
        content=body,
        media_type="application/json",
        headers={
            "X-Error-Code": HFErrorCode.REPO_NAME_RECYCLING,
            "X-Error-Message": LAKEFS_CONFLICT_RETRY_MESSAGE,
            # Ignored by hf_hub (its create_repo bypasses http_backoff), but
            # correct for any client that does honour it.
            "Retry-After": str(int(LAKEFS_RECYCLING_HOLD_SECONDS) or 1),
        },
    )


# How many LakeFS ids `_create_lakefs_repository` tries before giving up. Each
# failed id is excluded from the next allocation, and allocation itself falls
# back to random ids, so running out means LakeFS is refusing every new id.
LAKEFS_CREATE_ATTEMPTS = 4


class _RepoIdClaimed(Exception):
    """A concurrent request created the KHub repository while we were creating."""


async def _create_lakefs_repository(
    client,
    repo_type: str,
    repo_id: str,
    *,
    still_unclaimed=None,
) -> str | None:
    """Allocate a LakeFS id for `repo_id` and create the repository.

    Every path that creates a LakeFS repository goes through here, so each one
    steps over ids that turn out to be unusable at create time instead of
    surfacing LakeFS's error:

    - `409 not unique`: LakeFS still holds the id (asynchronous deletion, #93,
      or a probe/create race). The id is excluded and a fresh one allocated.
    - storage namespace already in use: an orphaned namespace. It is healed when
      that is provably safe; otherwise the id is excluded as well.

    Args:
        client: LakeFS client.
        repo_type: Repository type (model/dataset/space).
        repo_id: KHub repository id the LakeFS repository will back.
        still_unclaimed: Optional check run when an id is taken. Returning False
            means a concurrent request now owns `repo_id`; the create stops with
            `_RepoIdClaimed` rather than making a second, orphaned repository.

    Returns:
        The created LakeFS id, which the caller must persist on the row, or
        None if every attempt found its id unusable.

    Raises:
        _RepoIdClaimed: See `still_unclaimed`.
        Exception: Any other LakeFS error, unchanged.
    """
    tried: set[str] = set()
    for _ in range(LAKEFS_CREATE_ATTEMPTS):
        lakefs_repo = await allocate_lakefs_repo_name(client, repo_type, repo_id, exclude=tried)
        storage_namespace = f"s3://{cfg.s3.bucket}/{lakefs_repo}"
        try:
            await client.create_repository(
                name=lakefs_repo,
                storage_namespace=storage_namespace,
                default_branch="main",
            )
            return lakefs_repo
        except Exception as e:
            if _is_lakefs_repo_id_taken_error(e):
                if still_unclaimed is not None and not still_unclaimed():
                    raise _RepoIdClaimed(repo_id) from e
                logger.warning(f"LakeFS id {lakefs_repo} for {repo_id} is taken; trying another")
            elif _is_lakefs_namespace_in_use_error(e, storage_namespace):
                healed = await _cleanup_orphan_namespace_if_safe(
                    client, lakefs_repo, allow_empty_internal_marker=True
                )
                logger.warning(
                    f"Storage namespace of {lakefs_repo} for {repo_id} is in use "
                    f"(healed={healed}); {'retrying it' if healed else 'trying another id'}"
                )
                if healed:
                    continue  # the same id is usable again
            else:
                raise
        tried.add(lakefs_repo)
    return None


async def _drop_unclaimed_lakefs_repository(client, lakefs_repo: str) -> None:
    """Best-effort removal of a LakeFS repository no row points at."""
    try:
        await client.delete_repository(repository=lakefs_repo, force=True)
    except Exception as e:
        logger.warning(f"Failed to remove unclaimed LakeFS repository {lakefs_repo}: {e}")


def _has_only_internal_lakefs_markers(
    keys: list[str], repo_prefix: str, allow_empty: bool = False
) -> bool:
    """Allow cleanup only when all sampled objects are LakeFS internal markers."""
    if not keys:
        return allow_empty

    normalized_prefix = repo_prefix.rstrip("/") + "/"
    for key in keys:
        if not key.startswith(normalized_prefix):
            return False

        relative_key = key[len(normalized_prefix):]
        if not relative_key.startswith("_lakefs/"):
            return False

    return True


async def _list_repo_namespace_keys(repo_prefix: str, max_keys: int = 20) -> list[str]:
    """List a small sample of objects under the exact repo namespace for safety checks."""

    def _list() -> list[str]:
        s3 = get_s3_client()
        response = s3.list_objects_v2(Bucket=cfg.s3.bucket, Prefix=repo_prefix, MaxKeys=max_keys)
        return [obj["Key"] for obj in response.get("Contents", [])]

    return await run_in_s3_executor(_list)


async def _delete_exact_repo_dummy_marker(repo_prefix: str) -> bool:
    """Delete the exact LakeFS dummy marker for a repo namespace."""

    def _delete() -> bool:
        s3 = get_s3_client()
        key = f"{repo_prefix}_lakefs/dummy"
        try:
            s3.delete_object(Bucket=cfg.s3.bucket, Key=key)
            logger.warning(f"Deleted exact orphan dummy marker: {key}")
            return True
        except Exception as e:
            logger.warning(f"Failed to delete exact orphan dummy marker {key}: {e}")
            return False

    return await run_in_s3_executor(_delete)


async def _cleanup_orphan_namespace_if_safe(
    client, lakefs_repo: str, allow_empty_internal_marker: bool = False
) -> bool:
    """Delete an orphan namespace only if it is provably the current repo's internal residue."""
    try:
        if await client.repository_exists(lakefs_repo):
            logger.warning(
                f"Skip orphan cleanup for {lakefs_repo}: LakeFS repository still exists"
            )
            return False
    except Exception as e:
        logger.warning(f"Failed to verify LakeFS repository existence for {lakefs_repo}: {e}")
        return False

    repo_prefix = f"{lakefs_repo}/"
    sample_keys = await _list_repo_namespace_keys(repo_prefix)
    if not _has_only_internal_lakefs_markers(
        sample_keys,
        repo_prefix,
        allow_empty=allow_empty_internal_marker,
    ):
        logger.warning(
            f"Skip orphan cleanup for {lakefs_repo}: namespace contains non-internal objects; "
            f"sample_keys={sample_keys[:10]}"
        )
        return False

    if not sample_keys and allow_empty_internal_marker:
        logger.warning(
            f"Proceed orphan cleanup for {lakefs_repo}: LakeFS reported only internal marker conflict "
            f"but S3 listing returned no visible objects; deleting exact prefix {repo_prefix}"
        )
        return await _delete_exact_repo_dummy_marker(repo_prefix)

    deleted_count = await delete_objects_with_prefix(cfg.s3.bucket, repo_prefix)
    logger.warning(
        f"Auto-cleaned orphan namespace for {lakefs_repo}: deleted {deleted_count} object(s) under {repo_prefix}"
    )
    return deleted_count > 0


def _resolve_create_repo_private(payload: CreateRepoPayload) -> bool:
    """Collapse ``private`` and ``visibility`` into a single bool.

    Mirrors the resolution used by ``update_repo_settings`` so the create
    endpoint is compatible with both ``huggingface_hub<1`` (sends
    ``private``) and ``huggingface_hub>=1.x`` (sends ``visibility``).
    Explicit ``private`` takes precedence; ``visibility`` is only consulted
    when ``private`` was not sent. Defaults to public when neither is set.
    """
    if payload.private is not None:
        return bool(payload.private)
    if payload.visibility is None:
        return False
    if payload.visibility == "private":
        return True
    if payload.visibility == "public":
        return False
    raise HTTPException(
        400,
        detail={
            "error": (
                "Unsupported repository visibility. "
                "Only 'public' and 'private' are supported."
            )
        },
    )


@router.post("/repos/create")
async def create_repo(
    payload: CreateRepoPayload, user: User = Depends(get_current_user)
):
    """Create a new repository.

    Args:
        payload: Repository creation parameters
        user: Current authenticated user

    Returns:
        Created repository information
    """
    logger.info(
        f"Creating repository: {payload.organization or user.username}/{payload.name}"
    )
    resolved_private = _resolve_create_repo_private(payload)
    namespace = payload.organization or user.username

    # Check if user has permission to use this namespace
    check_namespace_permission(namespace, user)

    full_id = f"{namespace}/{payload.name}"

    # Check for exact match.
    # `huggingface_hub` only honors `exist_ok=True` when the server returns 409 (see
    # HfApi.create_repo in huggingface_hub/hf_api.py). Additionally, after the 409 is
    # caught the client unconditionally parses the response body as JSON to build the
    # returned RepoUrl (`d = r.json(); RepoUrl(d["url"], ...)`), so the body cannot be
    # empty — it must include a `url` field even though the error info also lives in
    # X-Error-* headers per HF's header-based error protocol.
    existing_repo = get_repository(payload.type, namespace, payload.name)
    if existing_repo:
        return _repo_exists_response(payload.type, full_id)

    # Check for normalized name conflicts
    normalized = normalize_name(payload.name)
    all_repos = Repository.select().where(
        (Repository.repo_type == payload.type) & (Repository.namespace == namespace)
    )
    for repo in all_repos:
        if normalize_name(repo.name) == normalized:
            conflict_full_id = f"{namespace}/{repo.name}"
            return _repo_exists_response(
                payload.type,
                conflict_full_id,
                message=f"Repository name conflicts with existing repository: {repo.name}",
            )

    # Create the LakeFS repository under a freshly allocated id. The helper
    # steps over ids that turn out to be unusable (still being deleted, or a
    # leftover storage namespace), so users only see an error for real failures.
    client = get_lakefs_client()
    try:
        lakefs_repo = await _create_lakefs_repository(
            client,
            payload.type,
            full_id,
            still_unclaimed=lambda: get_repository(payload.type, namespace, payload.name)
            is None,
        )
    except _RepoIdClaimed:
        # A concurrent create won the race. The name is taken for good, so
        # telling the client to retry would only make it spin before learning
        # the same thing.
        logger.info(
            f"Concurrent create won the race for {full_id}; "
            f"reporting it as an existing repository"
        )
        return _repo_exists_response(payload.type, full_id)
    except Exception as e:
        logger.exception(f"LakeFS repository creation failed for {full_id}", e)
        return hf_server_error(f"LakeFS repository creation failed: {str(e)}")

    if lakefs_repo is None:
        # Every fresh id was taken as well - LakeFS is still releasing several
        # previous incarnations. Hand the client a conflict it knows to retry,
        # after a short hold to pace hf_hub's sleepless retry loop.
        logger.warning(f"No usable LakeFS id for {full_id} yet; returning retryable conflict")
        await asyncio.sleep(LAKEFS_RECYCLING_HOLD_SECONDS)
        return _repo_recycling_response(payload.type, full_id)

    # Store in database for listing/metadata.
    # `lakefs_repo` records which LakeFS repository this row owns; every read
    # path resolves through it (see `resolve_lakefs_repo`).
    try:
        _row, created = Repository.get_or_create(
            repo_type=payload.type,
            namespace=namespace,
            name=payload.name,
            full_id=full_id,
            defaults={
                "private": resolved_private,
                # The account or organization the namespace names, not the
                # creator: deleting a member must not take an org's repos
                "owner": _namespace_owner(namespace),
                "lakefs_repo": lakefs_repo,
            },
        )
    except Exception as e:
        # A LakeFS repository without a row is unreachable; do not leave it.
        logger.exception(f"Failed to record repository {full_id}", e)
        await _drop_unclaimed_lakefs_repository(client, lakefs_repo)
        return hf_server_error(f"Failed to record repository: {str(e)}")
    if not created:
        # A concurrent create inserted the row first: its LakeFS create landed on
        # another id before this request's did (the loser steps over a taken id
        # while the winner's row is not visible yet). The name is theirs.
        logger.info(f"Concurrent create won the race for {full_id}; dropping {lakefs_repo}")
        await _drop_unclaimed_lakefs_repository(client, lakefs_repo)
        return _repo_exists_response(payload.type, full_id)
    # Its usage counts from main's first commit (a failure leaves it to a recount)
    try:
        head = await client.get_branch(repository=lakefs_repo, branch=usage.MAIN)
        usage.main_started(_row.id, head["commit_id"])
    except Exception as e:
        logger.warning(f"Could not start counting the usage of {full_id}: {e}")

    # Strict-freshness invalidation (#79): a fallback ghost binding for
    # this repo (written before the local repo existed) must be evicted
    # so a future ``with_repo_fallback`` 404 path cannot resurrect a
    # stale upstream binding for the now-occupied namespace slot.
    # ``invalidate_repo`` also bumps ``repo_gens[(rt, ns, name)]`` so
    # any fallback probe currently in flight has its ``safe_set``
    # rejected.
    get_fallback_cache().invalidate_repo(payload.type, namespace, payload.name)

    return {
        "url": f"{cfg.app.base_url}/{payload.type}s/{full_id}",
        "repo_id": full_id,
    }


async def create_repository_for_import(
    *, repo_type: str, namespace: str, name: str, private: bool
) -> Repository:
    """Create the repository an import lands in (``kohakuhub.api.importer``).

    The same steps as ``create_repo`` once its request-shaped validation is
    done, for a caller that is not an ``hf_hub`` client: it raises the HTTP
    error instead of building the responses ``huggingface_hub`` parses (a
    409 whose body must still carry a ``url``, and a retryable conflict while
    LakeFS recycles a name). An import reserves its target *before* any work
    starts, so a failure beyond this point leaves an empty repository the
    user can delete - never a task that downloads 40 GB into nothing.

    Raises:
        HTTPException: 409 when the name is taken or LakeFS is recycling it,
            500 when LakeFS or the database refuses the create.
    """
    full_id = f"{namespace}/{name}"
    client = get_lakefs_client()
    try:
        lakefs_repo = await _create_lakefs_repository(
            client,
            repo_type,
            full_id,
            still_unclaimed=lambda: get_repository(repo_type, namespace, name) is None,
        )
    except _RepoIdClaimed:
        raise HTTPException(
            409,
            detail={"error": f"Repository {full_id} already exists"},
            headers={"X-Error-Code": HFErrorCode.REPO_EXISTS},
        )
    except Exception as e:
        logger.exception(f"LakeFS repository creation failed for {full_id}", e)
        raise HTTPException(500, detail={"error": f"LakeFS repository creation failed: {e}"})

    if lakefs_repo is None:
        # Every fresh id was taken as well: LakeFS is still releasing several
        # previous incarnations of this name (#93). Not retried here - the
        # import's caller decides, and the name is genuinely unavailable now.
        raise HTTPException(
            409,
            detail={
                "error": (
                    f"Repository {full_id} is still being recycled by the storage "
                    "layer; try the import again in a moment."
                )
            },
            headers={"X-Error-Code": HFErrorCode.REPO_EXISTS},
        )

    try:
        row, created = Repository.get_or_create(
            repo_type=repo_type,
            namespace=namespace,
            name=name,
            full_id=full_id,
            defaults={
                "private": private,
                # The account the namespace names, not the request's user
                "owner": _namespace_owner(namespace),
                "lakefs_repo": lakefs_repo,
            },
        )
    except Exception as e:
        # A LakeFS repository without a row is unreachable; do not leave it.
        logger.exception(f"Failed to record repository {full_id}", e)
        await _drop_unclaimed_lakefs_repository(client, lakefs_repo)
        raise HTTPException(500, detail={"error": f"Failed to record repository: {e}"})
    if not created:
        logger.info(f"Concurrent create won the race for {full_id}; dropping {lakefs_repo}")
        await _drop_unclaimed_lakefs_repository(client, lakefs_repo)
        raise HTTPException(
            409,
            detail={"error": f"Repository {full_id} already exists"},
            headers={"X-Error-Code": HFErrorCode.REPO_EXISTS},
        )

    # Its usage counts from main's first commit (a failure leaves it to a recount)
    try:
        head = await client.get_branch(repository=lakefs_repo, branch=usage.MAIN)
        usage.main_started(row.id, head["commit_id"])
    except Exception as e:
        logger.warning(f"Could not start counting the usage of {full_id}: {e}")

    # Any fallback binding for this name was a ghost: the local repository
    # exists now, so a later 404 path must not resurrect the binding (#79).
    get_fallback_cache().invalidate_repo(repo_type, namespace, name)
    return row


class DeleteRepoPayload(BaseModel):
    """Payload for repository deletion."""

    type: RepoType = "model"
    name: str
    organization: Optional[str] = None
    sdk: Optional[str] = None


@router.delete("/repos/delete")
async def delete_repo(
    payload: DeleteRepoPayload,
    auth: tuple[User | None, bool] = Depends(get_current_user_or_admin),
):
    """Delete a repository. (NOTE: This is IRREVERSIBLE)

    Accepts both user authentication and admin token (X-Admin-Token header).

    Args:
        name: Repository name.
        organization: Organization name (optional, defaults to user namespace).
        type: Repository type.
        auth: Tuple of (user, is_admin) from authentication

    Returns:
        Success message or error response.
    """
    user, is_admin = auth
    repo_type = payload.type

    # Determine namespace
    if is_admin:
        # Admin must specify organization (no default namespace)
        if not payload.organization:
            raise HTTPException(400, detail="Admin must specify organization parameter")
        namespace = payload.organization
    else:
        namespace = payload.organization or user.username

    full_id = f"{namespace}/{payload.name}"

    # 1. Check if repository exists in database
    repo_row = get_repository(repo_type, namespace, payload.name)

    if not repo_row:
        return hf_repo_not_found(full_id, repo_type)

    # 2. Check if user has permission to delete this repository (admin bypasses)
    check_repo_delete_permission(repo_row, user, is_admin=is_admin)
    operation_lock.ensure_free(repo_row)

    # 3. Delete the row; its LakeFS repository and storage are purged in the
    # background (a task scheduled in the same transaction), so the answer
    # does not wait for however many objects it holds
    try:
        delete_repository(repo_row)
        logger.success(f"Deleted {full_id}; its storage is purged in the background")
    except Exception as e:
        logger.exception(f"Database deletion failed for {full_id}", e)
        return hf_server_error(f"Database deletion failed for {full_id}: {str(e)}")

    # Strict-freshness invalidation (#79): the local repo is gone so
    # subsequent reads will pass through ``with_repo_fallback`` to the
    # chain. Wipe any stale fallback binding for this repo (across all
    # user buckets) and bump ``repo_gens`` so any in-flight probe's
    # cache write is rejected. Without this, a ghost binding written
    # earlier (when the repo was absent) could be resurrected within
    # the cache TTL window.
    get_fallback_cache().invalidate_repo(repo_type, namespace, payload.name)

    # 4. Return success response (200 OK with a simple message)
    # HuggingFace Hub delete_repo returns a simple 200 OK.
    return {"message": f"Repository '{full_id}' of type '{repo_type}' deleted."}


class MoveRepoPayload(BaseModel):
    """Payload for repository move/rename."""

    fromRepo: str  # format: "namespace/repo-name"
    toRepo: str  # format: "namespace/repo-name"
    type: str = "model"


class SquashRepoPayload(BaseModel):
    """Payload for repository squashing (clear history)."""

    repo: str  # format: "namespace/repo-name"
    type: str = "model"
    message: Optional[str] = None  # the squash commit's message


def _namespace_owner(namespace: str) -> User | None:
    """The account or organization a namespace names."""
    return User.get_or_none(User.username == namespace)


def _update_repository_database_records(
    repo_row: Repository,
    from_id: str,
    to_id: str,
    from_namespace: str,
    to_namespace: str,
    to_name: str,
    moving_namespace: bool,
    to_lakefs_repo: str,
    preserve_quota: bool = True,
    to_owner: User | None = None,
) -> None:
    """Update database records for repository move (must be called within db.atomic()).

    Args:
        repo_row: Repository database record
        from_id: Source repository ID
        to_id: Target repository ID
        from_namespace: Source namespace
        to_namespace: Target namespace
        to_name: Target repository name
        moving_namespace: Whether namespace is changing
        to_lakefs_repo: LakeFS repository the row should point at afterwards:
            the one a move keeps, or the one a squash migration created. It is
            stored explicitly because it need not derive back from `to_id`.
        preserve_quota: Whether to preserve repository quota settings (default: True)
        to_owner: The account or organization the target namespace names. It
            owns the repository, its files and its commits afterwards (a
            deleted account takes what it owns along); ``None`` keeps them.
    """
    # Preserve current quota settings before update
    # NOTE: When moving to different namespace, reset quota to inherit from new namespace
    # When staying in same namespace (rename/squash), preserve quota settings
    # Its usage goes along: a namespace's is summed from its repositories
    current_quota_bytes = repo_row.quota_bytes if preserve_quota and not moving_namespace else None

    # Update repository record
    Repository.update(
        namespace=to_namespace,
        name=to_name,
        full_id=to_id,
        lakefs_repo=to_lakefs_repo,
        quota_bytes=current_quota_bytes,
    ).where(Repository.id == repo_row.id).execute()

    # File and Commit rows follow the repository by its id; only the owner
    # they carry (denormalized) changes with the namespace.
    if to_owner is not None and to_owner.id != repo_row.owner_id:
        Repository.update(owner=to_owner).where(Repository.id == repo_row.id).execute()
        File.update(owner=to_owner).where(File.repository == repo_row).execute()
        Commit.update(owner=to_owner).where(Commit.repository == repo_row).execute()


@router.post("/repos/move")
async def move_repo(
    payload: MoveRepoPayload,
    auth: tuple[User | None, bool] = Depends(get_current_user_or_admin),
):
    """Move/rename a repository.

    Matches HuggingFace Hub API: POST /api/repos/move
    Accepts both user authentication and admin token (X-Admin-Token header).

    Args:
        payload: Move parameters
        auth: Tuple of (user, is_admin) from authentication

    Returns:
        Success message with new URL
    """
    user, is_admin = auth
    from_id = payload.fromRepo
    to_id = payload.toRepo
    repo_type = payload.type

    # Parse IDs
    from_parts = from_id.split("/", 1)
    to_parts = to_id.split("/", 1)

    if len(from_parts) != 2:
        return hf_error_response(
            400, HFErrorCode.INVALID_REPO_ID, "Invalid source repository ID"
        )
    if len(to_parts) != 2:
        return hf_error_response(
            400, HFErrorCode.INVALID_REPO_ID, "Invalid destination repository ID"
        )

    from_namespace, from_name = from_parts
    to_namespace, to_name = to_parts

    # Check if source repository exists
    repo_row = get_repository(repo_type, from_namespace, from_name)
    if not repo_row:
        return hf_repo_not_found(from_id, repo_type)

    # Check permissions (admin bypasses)
    check_repo_delete_permission(repo_row, user, is_admin=is_admin)
    check_namespace_permission(to_namespace, user, is_admin=is_admin)
    operation_lock.ensure_free(repo_row)
    # The repository goes to the account or organization the namespace names
    to_owner = _namespace_owner(to_namespace)
    if to_owner is None:
        return hf_error_response(
            404, HFErrorCode.INVALID_REPO_ID, f"Namespace not found: {to_namespace}"
        )

    # Check if destination already exists. See `_repo_exists_response` for why the
    # response includes a JSON body as well as X-Error-* headers.
    existing = get_repository(repo_type, to_namespace, to_name)
    if existing:
        return _repo_exists_response(repo_type, to_id)

    # Create refuses names that differ from another repository only by case,
    # "-" or "_"; a move must not be a way around that. The repository being
    # moved is skipped, so renaming it to a variant of its own name is fine.
    normalized = normalize_name(to_name)
    for sibling in Repository.select().where(
        (Repository.repo_type == repo_type) & (Repository.namespace == to_namespace)
    ):
        if sibling.id != repo_row.id and normalize_name(sibling.name) == normalized:
            return _repo_exists_response(
                repo_type,
                f"{to_namespace}/{sibling.name}",
                message=f"Repository name conflicts with existing repository: {sibling.name}",
            )

    # Check storage quota (only for users, admin bypasses)
    moving_namespace = from_namespace != to_namespace

    if moving_namespace and not is_admin:
        repo_size = repo_row.used_bytes
        logger.info(
            f"Checking storage quota for moving {from_id} to {to_namespace} namespace"
        )

        # Check if target namespace is an organization
        target_org = get_organization(to_namespace)
        is_target_org = target_org is not None

        # Check quota for the target namespace based on repository privacy
        allowed, error_msg = check_quota(
            namespace=to_namespace,
            additional_bytes=repo_size,
            is_private=repo_row.private,
            is_org=is_target_org,
        )

        if not allowed:
            logger.warning(
                f"Quota check failed for moving {from_id} to {to_namespace}: {error_msg}"
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "error": error_msg,
                    "repo_size_bytes": repo_size,
                },
            )

        logger.info(
            f"Quota check passed for moving {from_id} to {to_namespace} "
            f"(repo size: {repo_size:,} bytes)"
        )

    # A move only renames the KHub row. Since migration 016 the LakeFS
    # repository id is stored on the row rather than derived from the repo id,
    # so the row keeps its LakeFS repository - with every commit, branch and
    # tag - and no data is copied or deleted (#107). The old repo id is free
    # at once: a new repository created under it allocates a different LakeFS
    # id because this one stays taken.
    lakefs_repo = resolve_lakefs_repo(repo_row)
    try:
        with db.atomic():
            _update_repository_database_records(
                repo_row=repo_row,
                from_id=from_id,
                to_id=to_id,
                from_namespace=from_namespace,
                to_namespace=to_namespace,
                to_name=to_name,
                moving_namespace=moving_namespace,
                to_lakefs_repo=lakefs_repo,
                to_owner=to_owner,
            )
    except IntegrityError:
        # Lost a race for the target name to a concurrent create or move; the
        # unique (repo_type, namespace, name) index rejected this update.
        return _repo_exists_response(repo_type, to_id)

    # Strict-freshness invalidation (#79): both ids change occupancy.
    # The old id transitions from "local-occupied" to "fallback-eligible";
    # any prior fallback binding for it must not survive. The new id
    # transitions from "potentially-fallback-eligible" to "local-occupied";
    # any ghost binding for the new id must be cleared so a later
    # delete-after-rename cannot resurrect the ghost.
    cache = get_fallback_cache()
    cache.invalidate_repo(repo_type, from_namespace, from_name)
    cache.invalidate_repo(repo_type, to_namespace, to_name)

    return {
        "success": True,
        "url": f"{cfg.app.base_url}/{repo_type}s/{to_id}",
        "message": f"Repository moved from {from_id} to {to_id}",
    }


@router.post(
    "/repos/squash",
    dependencies=[Depends(require_repository_squash_enabled)],
)
async def squash_repo(
    payload: SquashRepoPayload,
    auth: tuple[User | None, bool] = Depends(get_current_user_or_admin),
):
    """Squash a repository's history into one commit; only the current state stays.

    Main becomes a single commit with its current tree, in place (see
    ``kohakuhub.api.commit.squash``), and the other branches and tags are
    deleted. It takes about a second whatever the repository's size; the
    versions that became unreachable are forgotten and collected in the
    background.

    Accepts both user authentication and admin token (X-Admin-Token header).
    """
    ensure_repository_operation_enabled("squash")

    user, is_admin = auth
    repo_id = payload.repo
    repo_type = payload.type

    parts = repo_id.split("/", 1)
    if len(parts) != 2:
        return hf_error_response(
            400, HFErrorCode.INVALID_REPO_ID, "Invalid repository ID"
        )
    namespace, name = parts

    repo_row = get_repository(repo_type, namespace, name)
    if not repo_row:
        return hf_repo_not_found(repo_id, repo_type)

    check_repo_delete_permission(repo_row, user, is_admin=is_admin)

    try:
        await squash.squash(
            get_lakefs_client(),
            repo_row,
            resolve_lakefs_repo(repo_row),
            "main",
            user or repo_row.owner,  # an admin squashes on the owner's behalf
            payload.message or squash.SQUASH_MESSAGE,
            whole_repository=True,
        )
    except OperationRefused as e:
        raise HTTPException(status_code=e.status, detail=e.detail)

    return {
        "success": True,
        "message": f"Repository {repo_id} squashed successfully. All commit history has been cleared.",
    }
