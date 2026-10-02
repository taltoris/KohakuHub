"""Database models for Kohaku Hub.

Major refactoring: Merged User and Organization into unified User model.
All relationships now use proper ForeignKey constraints for data integrity.
"""

from datetime import datetime, timezone
from functools import partial

from kohakuhub.constants import DB_ON_DELETE_SET_NULL
from peewee import (
    AutoField,
    BigAutoField,
    BigIntegerField,
    BlobField,
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    ForeignKeyField,
    IntegerField,
    Model,
    PostgresqlDatabase,
    SqliteDatabase,
    TextField,
)

from kohakuhub.config import cfg
from kohakuhub.logger import get_logger

logger = get_logger("DB")


def _sqlite_path(url: str) -> str:
    return url.replace("sqlite:///", "")


# Choose DB backend
if cfg.app.db_backend == "postgres":
    url = cfg.app.database_url.replace("postgresql://", "")
    user_pass, host_db = url.split("@")
    user, password = user_pass.split(":")
    host_port, dbname = host_db.split("/")
    if ":" in host_port:
        host, port = host_port.split(":")
    else:
        host, port = host_port, 5432

    logger.info(f"Connecting to PostgreSQL: {user}@{host}:{port}/{dbname}")

    db = PostgresqlDatabase(
        dbname,
        user=user,
        password=password,
        host=host,
        port=int(port),
    )
else:
    db = SqliteDatabase(_sqlite_path(cfg.app.database_url), pragmas={"foreign_keys": 1})


class BaseModel(Model):
    class Meta:
        database = db


class User(BaseModel):
    """Unified User/Organization model.

    When is_org=False: Regular user with email/password
    When is_org=True: Organization (no email/password required)
    """

    id = AutoField()
    username = CharField(unique=True, index=True)
    normalized_name = CharField(
        unique=True, index=True
    )  # Normalized username for fast conflict checking
    is_org = BooleanField(default=False, index=True)  # True for organizations

    # User-specific fields (nullable for orgs)
    email = CharField(unique=True, index=True, null=True)
    password_hash = CharField(null=True)
    email_verified = BooleanField(default=False)
    is_active = BooleanField(default=True)

    # Separate quotas for private and public repositories
    private_quota_bytes = BigIntegerField(null=True)  # NULL = unlimited
    public_quota_bytes = BigIntegerField(null=True)  # NULL = unlimited
    private_used_bytes = BigIntegerField(default=0)
    public_used_bytes = BigIntegerField(default=0)

    # Profile fields (shared by both users and orgs)
    full_name = CharField(null=True)
    bio = TextField(null=True)
    description = TextField(null=True)  # For backward compat with orgs
    website = CharField(null=True)
    social_media = TextField(
        null=True
    )  # JSON: {twitter_x, threads, github, huggingface}

    # Avatar (1024x1024 JPEG stored as binary)
    avatar = BlobField(null=True)  # Binary JPEG data
    avatar_updated_at = DateTimeField(null=True)  # Track updates for cache busting
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))


class EmailVerification(BaseModel):
    id = AutoField()
    user = ForeignKeyField(
        User, backref="email_verifications", on_delete="CASCADE", index=True
    )
    token = CharField(unique=True, index=True)
    expires_at = DateTimeField()
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))


class Session(BaseModel):
    id = AutoField()
    session_id = CharField(unique=True, index=True)
    user = ForeignKeyField(User, backref="sessions", on_delete="CASCADE", index=True)
    secret = CharField()
    expires_at = DateTimeField()
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))


class Token(BaseModel):
    id = AutoField()
    user = ForeignKeyField(User, backref="tokens", on_delete="CASCADE", index=True)
    token_hash = CharField(unique=True, index=True)
    name = CharField()
    last_used = DateTimeField(null=True)
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))


class UserExternalToken(BaseModel):
    """User-specific external fallback source tokens.

    Allows users to provide their own tokens for external sources (HuggingFace, etc.).
    User tokens override admin-configured tokens for matching URLs.
    """

    id = AutoField()
    user = ForeignKeyField(
        User, backref="external_tokens", on_delete="CASCADE", index=True
    )
    url = CharField()  # Base URL (e.g., "https://huggingface.co")
    encrypted_token = TextField()  # Encrypted token (using crypto.py)
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))
    updated_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        indexes = ((("user", "url"), True),)  # Unique per user+url


class Repository(BaseModel):
    id = AutoField()
    repo_type = CharField(index=True)
    namespace = CharField(index=True)
    name = CharField(index=True)
    full_id = CharField(index=True)  # Not unique - same full_id can exist across types
    # LakeFS repository backing this row. NULL means "derive from repo_type +
    # full_id" (rows written before migration 016). Always read it through
    # `kohakuhub.utils.lakefs.resolve_lakefs_repo`: a repository allocated at
    # generation > 0 does not derive back to its own id.
    lakefs_repo = CharField(null=True, index=True)
    private = BooleanField(default=False)
    owner = ForeignKeyField(
        User, backref="owned_repos", on_delete="CASCADE", index=True
    )

    # Repository-specific quota (NULL = inherit from namespace)
    quota_bytes = BigIntegerField(
        null=True
    )  # NULL = no specific limit, inherit from namespace
    used_bytes = BigIntegerField(default=0)
    # Kept up to date as the repository changes (kohakuhub.usage);
    # used_bytes is their sum. A namespace's usage is summed from these.
    main_regular_bytes = BigIntegerField(default=0)  # regular files on main
    lfs_bytes = BigIntegerField(default=0)  # stored LFS objects its history links, once each
    # The main commit main_regular_bytes counts up to; NULL = not aligned yet
    main_counted_commit = CharField(max_length=64, null=True)
    # A history operation holding the repository (see
    # api/repo/utils/operation_lock), and until when; writes wait or retry
    operation = CharField(max_length=64, null=True)
    operation_until = DateTimeField(null=True)
    # A repository squash's commit: the commits it does not reach are gone
    history_root = CharField(max_length=64, null=True)

    # LFS settings (NULL = use server defaults from config)
    lfs_threshold_bytes = IntegerField(
        null=True
    )  # NULL = use cfg.app.lfs_threshold_bytes
    lfs_keep_versions = IntegerField(null=True)  # NULL = use cfg.app.lfs_keep_versions
    lfs_suffix_rules = TextField(
        null=True
    )  # JSON list of suffixes like [".safetensors", ".bin"], NULL = no suffix rules

    # Social metrics (denormalized counters for fast queries)
    downloads = IntegerField(default=0)  # Total download sessions (not file count)
    likes_count = IntegerField(default=0)  # Total likes

    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        # Unique constraint on (repo_type, namespace, name) allows same name across types
        indexes = (
            (("repo_type", "namespace", "name"), True),
            (("namespace", "private"), False),  # summing a namespace's usage
        )


class File(BaseModel):
    id = AutoField()
    repository = ForeignKeyField(
        Repository, backref="files", on_delete="CASCADE", index=True
    )
    path_in_repo = CharField(index=True)
    size = BigIntegerField(default=0)  # Changed from IntegerField to support files >2GB
    sha256 = CharField(index=True)
    lfs = BooleanField(default=False)
    is_deleted = BooleanField(default=False, index=True)  # Soft delete flag
    owner = ForeignKeyField(
        User, backref="owned_files", on_delete="CASCADE", index=True
    )  # Repository owner (denormalized for convenience)
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))
    updated_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        indexes = ((("repository", "path_in_repo"), True),)


class StagingUpload(BaseModel):
    id = AutoField()
    repository = ForeignKeyField(
        Repository, backref="staging_uploads", on_delete="CASCADE", index=True
    )
    repo_type = CharField(index=True)
    revision = CharField(index=True)
    path_in_repo = CharField()
    sha256 = CharField(default="")
    size = BigIntegerField(default=0)  # Changed from IntegerField to support files >2GB
    upload_id = CharField(null=True)
    storage_key = CharField()
    lfs = BooleanField(default=False)
    uploader = ForeignKeyField(
        User, backref="uploads", on_delete=DB_ON_DELETE_SET_NULL, null=True, index=True
    )
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))


class UserOrganization(BaseModel):
    """User-Organization membership.

    Links regular users (is_org=False) to organizations (is_org=True).
    """

    id = AutoField()
    user = ForeignKeyField(
        User, backref="org_memberships", on_delete="CASCADE", index=True
    )
    organization = ForeignKeyField(
        User, backref="members", on_delete="CASCADE", index=True
    )
    role = CharField(default="member")  # visitor, member, admin, super-admin
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        indexes = ((("user", "organization"), True),)  # Unique membership


class Commit(BaseModel):
    """Track commits made by users (LakeFS doesn't track the actual user).

    author = actual user who made the commit
    owner = repository owner (user or org, denormalized for convenience)
    """

    id = AutoField()
    commit_id = CharField(index=True)  # LakeFS commit ID (SHA)
    repository = ForeignKeyField(
        Repository, backref="commits", on_delete="CASCADE", index=True
    )
    repo_type = CharField(index=True)  # model/dataset/space
    branch = CharField(index=True)  # Branch name
    author = ForeignKeyField(
        User, backref="authored_commits", on_delete="CASCADE", index=True
    )  # User who made commit
    owner = ForeignKeyField(
        User, backref="owned_commits", on_delete="CASCADE", index=True
    )  # Repository owner (denormalized)
    username = CharField(index=True)  # Username (denormalized for performance)
    message = TextField()  # Commit message
    description = TextField(default="")  # Optional description
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        indexes = (
            (("repository", "branch"), False),  # Query commits by repo+branch
            (("commit_id", "repository"), True),  # Unique commit per repo
        )


class LFSObjectHistory(BaseModel):
    """Track LFS object usage history for garbage collection.

    Keeps track of which commits reference which LFS objects,
    allowing us to preserve K versions of each file.
    """

    id = AutoField()
    repository = ForeignKeyField(
        Repository, backref="lfs_history", on_delete="CASCADE", index=True
    )
    path_in_repo = CharField(index=True)  # File path
    sha256 = CharField(index=True)  # LFS object hash
    size = BigIntegerField()  # Changed from IntegerField to support files >2GB
    commit_id = CharField(index=True)  # LakeFS commit ID
    # Optional link to File record for faster lookups
    # IMPORTANT: on_delete=DB_ON_DELETE_SET_NULL prevents CASCADE deletion when File is deleted
    # LFSObjectHistory must persist for quota tracking even after file deletion
    file = ForeignKeyField(
        File,
        backref="lfs_versions",
        on_delete=DB_ON_DELETE_SET_NULL,
        null=True,
        index=True,
    )
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        # Index for quick lookups by repo and path
        indexes = (
            (("repository", "path_in_repo"), False),
            (("sha256",), False),
        )


class SSHKey(BaseModel):
    """User SSH public keys for Git operations."""

    id = AutoField()
    user = ForeignKeyField(User, backref="ssh_keys", on_delete="CASCADE", index=True)
    key_type = CharField()  # "ssh-rsa", "ssh-ed25519", "ecdsa-sha2-nistp256", etc.
    public_key = TextField()  # Full public key content
    fingerprint = CharField(unique=True, index=True)  # SHA256 fingerprint for lookup
    title = CharField()  # User-friendly title/name for the key
    last_used = DateTimeField(null=True)
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        indexes = ((("user", "fingerprint"), True),)  # Unique per user


class Invitation(BaseModel):
    """Generic invitation system for various actions (org invites, account registration, etc.)."""

    id = AutoField()
    token = CharField(unique=True, index=True)  # UUID for invitation link
    action = CharField(index=True)  # "join_org", "register_account", etc.
    parameters = (
        TextField()
    )  # JSON string for action-specific data (org_id, role, etc.)
    created_by = ForeignKeyField(
        User, backref="created_invitations", on_delete="CASCADE", null=True, index=True
    )  # NULL for system/admin-generated invitations
    expires_at = DateTimeField()

    # Multi-use support
    max_usage = IntegerField(null=True)  # NULL=one-time, -1=unlimited, N=max N uses
    usage_count = IntegerField(default=0)  # Track how many times used

    # Legacy fields (kept for single-use compatibility)
    used_at = DateTimeField(null=True)  # First use timestamp
    used_by = ForeignKeyField(
        User,
        backref="used_invitations",
        on_delete=DB_ON_DELETE_SET_NULL,
        null=True,
        index=True,
    )
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        indexes = (
            (
                ("action", "created_by"),
                False,
            ),  # Query invitations by action and creator
        )


class RepositoryLike(BaseModel):
    """User likes for repositories (similar to GitHub stars)."""

    id = AutoField()
    repository = ForeignKeyField(
        Repository, backref="likes", on_delete="CASCADE", index=True
    )
    user = ForeignKeyField(User, backref="liked_repos", on_delete="CASCADE", index=True)
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        indexes = ((("repository", "user"), True),)  # UNIQUE - can't like twice


class DownloadSession(BaseModel):
    """Track download sessions for deduplication and statistics.

    A download session represents a bulk download (e.g., git clone, snapshot_download).
    Multiple files downloaded within the same session (15-minute window) count as 1 download.
    """

    id = AutoField()
    repository = ForeignKeyField(
        Repository, backref="download_sessions", on_delete="CASCADE", index=True
    )
    user = ForeignKeyField(
        User,
        backref="downloads",
        on_delete=DB_ON_DELETE_SET_NULL,
        null=True,
        index=True,
    )  # NULL if anonymous

    # Session identification for deduplication
    session_id = CharField(index=True)  # From cookie: auth session OR tracking cookie
    time_bucket = IntegerField(index=True)  # Unix timestamp / 900 (15-minute buckets)

    # Download details
    file_count = IntegerField(default=1)  # Files downloaded in this session
    first_file = CharField()  # Which file started the session

    # Timestamps
    first_download_at = DateTimeField(
        default=partial(datetime.now, tz=timezone.utc), index=True
    )
    last_download_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        indexes = (
            (
                ("repository", "session_id", "time_bucket"),
                True,
            ),  # UNIQUE - deduplication key
        )


class DailyRepoStats(BaseModel):
    """Daily aggregated statistics for repository trends.

    TODAY's stats are updated in real-time.
    Historical stats (yesterday and older) are lazily aggregated from DownloadSession.
    """

    id = AutoField()
    repository = ForeignKeyField(
        Repository, backref="daily_stats", on_delete="CASCADE", index=True
    )
    date = DateField(index=True)  # YYYY-MM-DD

    # Aggregated metrics
    download_sessions = IntegerField(default=0)  # Unique download sessions
    authenticated_downloads = IntegerField(default=0)  # Logged-in users
    anonymous_downloads = IntegerField(default=0)  # Anonymous users
    total_files = IntegerField(default=0)  # Total file /resolve calls

    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        indexes = ((("repository", "date"), True),)  # UNIQUE per repo per day


class FallbackSource(BaseModel):
    """External fallback sources for repository resolution.

    Allows KohakuHub to fall back to HuggingFace or other KohakuHub instances
    when repositories or files are not found locally.
    """

    id = AutoField()
    namespace = CharField(default="", index=True)  # "" for global, or user/org name
    url = CharField()  # Base URL: https://huggingface.co
    token = CharField(null=True)  # Admin-configured token (encrypted)
    priority = IntegerField(default=100, index=True)  # Lower = higher priority
    name = CharField()  # Display name: "HuggingFace"
    source_type = CharField()  # "huggingface" or "kohakuhub"
    enabled = BooleanField(default=True, index=True)
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))
    updated_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))

    class Meta:
        indexes = (
            (("namespace", "priority"), False),  # For ordered lookups
            (("enabled", "priority"), False),  # For active sources
        )


class ConfirmationToken(BaseModel):
    """General-purpose confirmation tokens for dangerous operations.

    Provides two-step confirmation for destructive actions with automatic expiration.
    Works across multiple workers (database-backed).

    Use cases:
    - S3 prefix deletion: action_type="delete_s3_prefix", action_data='{"prefix": "path/"}'
    - Bulk operations: action_type="bulk_delete_repos", action_data='{"repo_ids": [...]}'
    - Any operation requiring explicit user confirmation

    Example usage:
        # Step 1: Create token
        token = ConfirmationToken.create(
            token=str(uuid.uuid4()),
            action_type="delete_s3_prefix",
            action_data=json.dumps({"prefix": "hf-model-org-repo/"}),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60)
        )

        # Step 2: Validate and consume token
        conf = ConfirmationToken.get_or_none(
            ConfirmationToken.token == token_str,
            ConfirmationToken.expires_at > datetime.now(timezone.utc)
        )
        if conf:
            data = json.loads(conf.action_data)
            # Execute action
            conf.delete_instance()  # Consume token (single-use)
    """

    id = AutoField()
    token = CharField(unique=True, index=True)  # UUID token
    action_type = CharField(index=True)  # Type of action (e.g., "delete_s3_prefix")
    action_data = TextField()  # JSON data for the action
    created_at = DateTimeField(default=partial(datetime.now, tz=timezone.utc))
    expires_at = DateTimeField(index=True)  # For automatic cleanup

    class Meta:
        indexes = ((("action_type", "expires_at"), False),)  # For cleanup queries


def utcnow() -> datetime:
    """Naive UTC now; ``BackgroundTask`` stores naive UTC so SQL comparisons
    behave the same on PostgreSQL (``timestamp``) and SQLite (text)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class BackgroundTask(BaseModel):
    """Durable background task row; see ``kohakuhub.tasks`` for semantics."""

    id = BigAutoField()
    kind = CharField(max_length=255)
    queue = CharField(max_length=64, default="default")
    payload = TextField(default="{}")  # JSON: IDs and small parameters only
    status = CharField(max_length=16, default="queued")
    priority = IntegerField(default=0)
    # Unique while pending; released (NULL) when the task is claimed.
    dedupe_key = CharField(max_length=255, null=True, unique=True)
    run_after = DateTimeField(default=utcnow)
    attempts = IntegerField(default=0)
    max_attempts = IntegerField(default=5)
    locked_by = CharField(max_length=255, null=True)
    locked_until = DateTimeField(null=True)
    last_error = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    started_at = DateTimeField(null=True)
    finished_at = DateTimeField(null=True)
    # Copied from the kind at enqueue, like max_attempts; NULL disables stall detection.
    stall_seconds = IntegerField(null=True)
    cancel_requested = BooleanField(default=False)
    # Progress reported through TaskContext; the base is the attempt's first
    # report, so the ETA of a resumed attempt only counts its own work.
    progress_done = BigIntegerField(null=True)
    progress_total = BigIntegerField(null=True)
    progress_stage = CharField(max_length=255, null=True)
    progress_at = DateTimeField(null=True)
    progress_base_done = BigIntegerField(null=True)
    progress_base_at = DateTimeField(null=True)
    checkpoint = TextField(null=True)  # JSON cursor from TaskContext.checkpoint()

    class Meta:
        table_name = "background_task"
        indexes = (
            (("status", "queue", "run_after"), False),
            (("status", "locked_until"), False),
            (("status", "finished_at"), False),
        )


class BackgroundTaskEvent(BaseModel):
    """One lifecycle event of a task, written with the state change it records."""

    id = BigAutoField()
    task = ForeignKeyField(BackgroundTask, backref="events", on_delete="CASCADE", index=True)
    at = DateTimeField(default=utcnow)
    type = CharField(max_length=32)
    attempt = IntegerField(default=0)
    worker = CharField(max_length=255, null=True)
    detail = TextField(null=True)  # JSON

    class Meta:
        table_name = "background_task_event"


class LfsGcCandidate(BaseModel):
    """An LFS object whose last known reference may be gone.

    Recorded in the transaction that deletes a repository row, because the
    cascade also removes the history that says which objects it used; the
    ``storage.collect_lfs`` task deletes each one no row references any more.
    """

    sha256 = CharField(max_length=64, primary_key=True)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "lfs_gc_candidate"


class LfsObjectTombstone(BaseModel):
    """An LFS object garbage collection deleted, or is deleting.

    History rows stay, so the tombstone is what says the content is gone;
    re-uploading the same content removes it (see ``kohakuhub.lfs_gc``).
    """

    sha256 = CharField(max_length=64, primary_key=True)
    state = CharField(max_length=16)  # deleting, then deleted
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "lfs_object_tombstone"


class LfsRecentObject(BaseModel):
    """An LFS object uploaded or claimed by a commit recently.

    Garbage collection keeps it through a grace period, so a collection can
    never delete content an upload or a commit in flight relies on.
    """

    sha256 = CharField(max_length=64, primary_key=True)
    touched_at = DateTimeField(default=utcnow, index=True)

    class Meta:
        table_name = "lfs_recent_object"


class LfsHeadRef(BaseModel):
    """An LFS object the head of a branch links at a path.

    Garbage collection never deletes an object a branch head links; keep
    windows only decide how many older versions stay. Commits and branch
    operations keep these rows current, and the LFS reference reconciliation
    adds what earlier versions never recorded (see ``kohakuhub.lfs_gc``).
    """

    repository = ForeignKeyField(Repository, on_delete="CASCADE", index=True)
    branch = CharField()
    path_in_repo = CharField()
    sha256 = CharField(max_length=64, index=True)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "lfs_head_ref"
        indexes = ((("repository", "branch", "path_in_repo", "sha256"), True),)


class LfsGcState(BaseModel):
    """Durable garbage collection state, such as when the LFS references of
    every branch head were last reconciled (see ``kohakuhub.lfs_gc``)."""

    key = CharField(max_length=64, primary_key=True)
    value = TextField()
    updated_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "lfs_gc_state"


class RepositoryImport(BaseModel):
    """An import of a repository from a fallback source into this hub.

    Written when the import is accepted, kept afterwards, and read by the
    repository page: a repository whose import did not finish is a partial
    copy, and the page says so instead of showing an empty repository as if
    it were complete (see ``kohakuhub.api.importer``).

    ``state`` is the outcome as people see it (``queued``, ``running``,
    ``succeeded``, ``failed``, ``cancelled``); the task row of
    ``repo.import`` carries the attempts, logs and progress timeline.
    """

    id = BigAutoField()
    repository = ForeignKeyField(Repository, on_delete="CASCADE", index=True)
    repo_type = CharField(max_length=16)
    source_repo_id = CharField(max_length=255)  # namespace/name at the source
    source_revision = CharField(max_length=255, default="main")
    source_url = CharField(max_length=255)
    source_name = CharField(max_length=255)
    source_type = CharField(max_length=32)  # huggingface or kohakuhub
    # The token the import reads the source with, encrypted like a user's
    # external token. The source's own token when the user has none: the
    # worker has no request, so what it needs must be stored, and a task
    # payload may not carry credentials (kohakuhub.tasks.enqueue).
    source_token = TextField(null=True)
    requested_by = ForeignKeyField(
        User, on_delete=DB_ON_DELETE_SET_NULL, null=True, index=True
    )
    state = CharField(max_length=16, default="queued", index=True)
    stage = CharField(max_length=255, null=True)
    files_total = IntegerField(default=0)
    files_done = IntegerField(default=0)
    bytes_total = BigIntegerField(default=0)
    bytes_done = BigIntegerField(default=0)
    # Storage this import may use, from the target namespace's quota at the
    # time it was accepted (NULL = unlimited). Checked as bytes arrive, so a
    # source that grows past the tree it listed stops at the quota instead
    # of filling the disk (kohakuhub.api.quota.util).
    allowed_bytes = BigIntegerField(null=True)
    error = TextField(null=True)
    commit_id = CharField(max_length=64, null=True)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "repository_import"
        indexes = (
            (("repository", "state"), False),
            (("state", "created_at"), False),
        )


class RepositoryWrite(BaseModel):
    """A write about to move a branch of a repository in LakeFS.

    Registered before the lock of a history operation is checked, so the
    operation waits for it (see api/repo/utils/operation_lock); it expires in
    case its process dies.
    """

    repository = ForeignKeyField(Repository, on_delete="CASCADE", index=True)
    until = DateTimeField()

    class Meta:
        table_name = "repository_write"


class BackgroundWorker(BaseModel):
    """A worker process, registered at startup and kept fresh by heartbeats.

    Online, draining, lost and stopped are derived from ``state`` and
    ``last_heartbeat_at`` when read; see ``kohakuhub.tasks.worker_status``.
    """

    id = CharField(max_length=255, primary_key=True)  # the worker id tasks record in locked_by
    name = CharField(max_length=255)  # display name: optional prefix plus hostname
    hostname = CharField(max_length=255)
    pid = IntegerField()
    queues = TextField(default="[]")  # JSON list; empty means every queue
    concurrency = IntegerField()
    state = CharField(max_length=16)  # running, draining or stopped
    succeeded = BigIntegerField(default=0)  # attempts since this process started
    failed = BigIntegerField(default=0)
    started_at = DateTimeField()
    last_heartbeat_at = DateTimeField()
    stopped_at = DateTimeField(null=True)

    class Meta:
        table_name = "background_worker"


class BackgroundTaskLog(BaseModel):
    """A log record emitted while a task attempt ran."""

    id = BigAutoField()
    task = ForeignKeyField(BackgroundTask, backref="logs", on_delete="CASCADE", index=True)
    attempt = IntegerField()
    at = DateTimeField()
    level = CharField(max_length=16)
    message = TextField()

    class Meta:
        table_name = "background_task_log"


def init_db():
    db.connect(reuse_if_open=True)
    db.create_tables(
        [
            User,
            EmailVerification,
            Session,
            Token,
            UserExternalToken,
            Repository,
            File,
            StagingUpload,
            UserOrganization,
            Commit,
            LFSObjectHistory,
            SSHKey,
            Invitation,
            RepositoryLike,
            DownloadSession,
            DailyRepoStats,
            FallbackSource,
            ConfirmationToken,
            BackgroundTask,
            BackgroundTaskEvent,
            BackgroundTaskLog,
            BackgroundWorker,
            LfsGcCandidate,
            LfsObjectTombstone,
            LfsRecentObject,
            LfsHeadRef,
            LfsGcState,
            RepositoryWrite,
            RepositoryImport,
        ],
        safe=True,
    )
