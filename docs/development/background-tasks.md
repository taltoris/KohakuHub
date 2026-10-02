# Background Tasks

KohakuHub runs durable background work through a small queue stored in the
application database (`background_task` table) and a separate worker process,
`khub-worker` (`python -m kohakuhub.worker`). There is no broker. Valkey is not
involved, and losing it never loses a task.

- Code: `src/kohakuhub/tasks.py` (queue) and `src/kohakuhub/worker.py` (runtime)
- Test helper: `src/kohakuhub/task_testing.py` (`run_with_interruptions`)
- Admin: **Background Tasks** page in the admin panel, backed by `/admin/api/tasks`
- Design and rationale: issue #104 (queue); issue #111 (timeline, progress,
  cancellation, logs, handler contract)

## Writing a task

Register an async handler with a stable kind name, then enqueue it:

```python
from kohakuhub.db import db
from kohakuhub.tasks import PermanentTaskError, enqueue, task


@task("repo.recalculate_storage", max_attempts=5, timeout=600)
async def recalculate_storage(payload: dict) -> None:
    repo = Repository.get_or_none(Repository.id == payload["repo_id"])
    if repo is None:
        raise PermanentTaskError("repository no longer exists")  # no retries
    await usage.recount_repository(repo.id)


with db.atomic():
    commit_row = Commit.create(...)
    # Inserted in the same transaction: rolled back together with the commit.
    enqueue(
        "repo.recalculate_storage",
        {"repo_id": repo.id},
        dedupe_key=f"repo-storage:{repo.id}",
    )
```

The worker only claims kinds it has registered, so import every module that
defines handlers in `kohakuhub/worker.py`. The API process registers the kinds
it enqueues by importing the same module.

A handler that declares a second parameter receives a `TaskContext`:

```python
from kohakuhub.tasks import TaskCancelled, TaskContext, task


@task("repo.cleanup_storage", timeout=6 * 3600, on_failure=forget_scratch)
async def cleanup_storage(payload: dict, ctx: TaskContext) -> None:
    ctx.stage("listing objects")
    keys = await list_keys(payload["prefix"], after=(ctx.checkpoint_state or {}).get("after"))
    ctx.stage("deleting objects")
    for done, batch in enumerate(batched(keys, 1000), start=1):
        if ctx.cancel_requested:
            raise TaskCancelled()  # stop at a clean boundary
        await delete_keys(batch)  # re-runnable: deleting twice is a no-op
        ctx.checkpoint({"after": batch[-1]})  # only after the batch is durable
        ctx.progress(done * 1000, total=len(keys))
        logger.info(f"Deleted {done * 1000} of {len(keys)} objects")  # lands in the task log
```

| `TaskContext` | What it does |
| --- | --- |
| `ctx.progress(done, total=None)` | Records progress in memory. The worker stores it with its next flush, with no extra write per call. `total=None` means the total is not known. |
| `ctx.stage(name)` | Stores the current stage at once and adds it to the timeline. |
| `ctx.checkpoint(state)` / `ctx.checkpoint_state` | Persists a small JSON cursor at once. The next attempt receives it as `checkpoint_state`. Raises `LeaseLost` if the task is no longer this worker's. |
| `ctx.cancel_requested` | Becomes `True` once an admin asks to cancel. Stop by raising `TaskCancelled`. |
| `ctx.assert_owned()` | Raises `LeaseLost` unless this worker still owns the task. Call it inside the transaction that publishes the result. On PostgreSQL it also locks the task row until that transaction ends. |
| `ctx.scratch_prefix` | `tmp/tasks/{task_id}/`: a deterministic location for intermediate artifacts. |
| `ctx.task_id`, `ctx.kind`, `ctx.attempt` | Identify the run. |

Logs need no API. While a handler runs, every log record at INFO or above that
it emits, through `kohakuhub.logger`, loguru or the standard `logging` module,
is copied into the task's log.

`@task` options besides `queue`, `max_attempts`, `timeout` and `every`:

- `stall_after=timedelta(...)` (default 10 minutes, `None` to disable). A
  running task whose progress has not moved for that long is flagged
  **stalled** in the admin panel. Stalled is a warning; nothing is killed.
- `on_failure=async_fn(payload, failure)`. Runs once, as its own
  `<kind>.on_failure` task with the same retries, when a task ends `failed`
  or `cancelled`. `failure` is a `TaskFailure` with `task_id`, `kind`,
  `outcome`, `error` and `scratch_prefix`. It must be re-runnable like any
  handler.

Rules:

- **Handlers must be re-runnable.** Delivery is at-least-once. If a worker dies
  mid-task, the task runs again once its lease expires. Follow the
  [handler contract](#handler-contract).
- **Payloads carry IDs and small parameters only.** Never put credentials,
  presigned URLs, or file content in a payload. Handlers re-read current state
  from the database.
- Raise `PermanentTaskError` for failures that retrying cannot fix. Any other
  exception is retried with exponential backoff until `max_attempts`.
- Handlers are async functions and run on the worker's event loop. Offload
  heavy blocking work (hashing, large boto3 calls) the same way API handlers
  do.
- **Never `await` inside `db.atomic()`.** Handlers share the loop thread's
  database connection with the worker's own bookkeeping, just as hub-api
  request handlers share it with each other.
- Keep handler modules free of `kohakuhub.api` router imports: some routers
  touch the schema at import time, and the worker must never create tables
  before hub-api has run migrations.

## Handler contract

The system guarantees only this: **a task runs at least once, and any attempt
can stop at any `await`**. SIGKILL, the OOM killer or a lost node run no
`finally` block and no cleanup code. So the rule is that **the next attempt
cleans up after the previous one**; cleanup at exit is only an optimisation.

1. **Re-runnable.** Running the handler again from any interruption point
   converges to the same result as one clean run.
2. **Deterministic scratch space.** Intermediate artifacts live under
   `ctx.scratch_prefix`, so the next attempt can always find what a previous
   one left behind and reuse or remove it. Never use random temporary names
   for anything that outlives a single `await`.
3. **Stage, then publish atomically.** Do the work in scratch space or
   unpublished state, and make it visible in one atomic step: a database
   transaction, a pointer switch or a LakeFS commit. Never mutate
   user-visible state piece by piece. The Move data loss in #107 broke
   exactly this rule.
4. **Checkpoint long work.** Call `ctx.checkpoint(state)` only after the work
   it covers is durable, and resume from `ctx.checkpoint_state`.
5. **Interruptible everywhere.** A `finally` may do fast best-effort cleanup,
   but correctness never depends on it running.
6. **Fence the publish.** Call `ctx.assert_owned()` inside the publishing
   transaction, so a worker that lost its lease never publishes.
7. **Terminal cleanup.** Use `on_failure` for what must be undone when a task
   gives up or is cancelled, such as removing `failure.scratch_prefix`.
8. **Register external side effects.** Before creating something outside the
   database, record the intent in the payload, the checkpoint or a domain
   table. An audit can then find it even if every cleanup layer fails.

### Testing a handler

Every handler that is not trivial gets a `run_with_interruptions` test:

```python
from kohakuhub.task_testing import run_with_interruptions


async def test_cleanup_storage_survives_interruptions():
    points = await run_with_interruptions(
        cleanup_storage,
        {"prefix": "repo-7/"},
        reset=seed_objects,  # restore the initial state
        snapshot=list_remaining_objects,  # include anything under the scratch prefix
    )
    assert points > 0
```

The helper runs the handler once without interruption and records the final
state. Then, for every interruption point (just before and just after each
`progress`, `stage` and `checkpoint` call), it:

1. resets;
2. runs the handler with a crash at that point;
3. runs it again with the checkpoint the crashed run left behind;
4. asserts that the final state matches the uninterrupted run.

The crash is a `BaseException`, so the handler's `except Exception` cannot
swallow it, just as it could not swallow a real SIGKILL. The helper also
reports a handler with no interruption points, and one whose behaviour
changes between runs from the same state.

## Semantics

| Feature | How it works |
| --- | --- |
| Claim | `SELECT ... FOR UPDATE SKIP LOCKED` on PostgreSQL. On SQLite, a compare-and-set update. Only kinds registered in the worker process are claimed. |
| Lease | The claim sets `locked_until = now + lease_seconds`. The worker renews it every `lease_seconds / 3`. An expired lease makes the task claimable again. |
| Stale workers | Renewals and outcome writes only apply while the worker still owns the lease. A slow worker that lost its lease cannot overwrite the new owner's state. |
| Flush | Every `min(flush_interval_seconds, lease_seconds / 3)` the worker stores the handler's captured logs and pending progress. It renews the lease in the same update, then checks for a cancellation request. |
| Timeline | Every state change writes a `background_task_event` row in the same transaction: `created`, `claimed`, `stage`, `retry_scheduled` (with the error and next run time), `succeeded`, `failed`, `released`, `lease_expired`, `cancel_requested`, `cancelled` and `retried`. Each attempt's error is kept. |
| Progress and ETA | Stored on the task row. The ETA is computed when read: work done this attempt divided by the time since the attempt's first report. So it grows while a task stalls, and a resumed attempt counts only its own work. |
| Cancel | A queued task is cancelled at once, releasing its dedupe key. For a running task the admin sets `cancel_requested`. The next flush sets `ctx.cancel_requested`, and the handler can stop by raising `TaskCancelled`. If it has not stopped within `shutdown_grace_seconds`, it is interrupted. Either way the task ends `cancelled`, without a retry, and `on_failure` runs. A flagged task whose worker died is cancelled when its lease expires. |
| Logs | Captured per attempt into `background_task_log`, up to `log_max_bytes_per_attempt` (10 MiB); past the cap, one truncation notice is kept. A failed attempt's traceback is added. A worker that lost its lease still stores its logs, under its own attempt number. |
| Retry | `run_after = now + 5s * 2^(attempt-1)` (capped at 1 h, plus up to 25% jitter). |
| Timeout | Per-kind `timeout` seconds; a timeout counts as a retryable failure. |
| Dedupe | `dedupe_key` is unique while the task is pending. Claiming the task releases the key, so work arriving while it runs can enqueue a fresh task. |
| Delay | `enqueue(..., run_after=datetime)`. Timestamps are naive UTC (`kohakuhub.db.utcnow()`). |
| Periodic | `@task(..., every=timedelta(...))`. Claiming an occurrence inserts the next one in the same transaction, so exactly one is pending at any time. Workers re-create a missing occurrence (for example, one cancelled from the admin panel) within a minute. |
| Worker roster | Each worker registers itself in `background_worker` at startup (no configuration needed; replicas register themselves) and refreshes its row every 10 s. Each row records the worker's queues and concurrency, and the attempts it has succeeded or failed since it started. Its current load is read live from the tasks it holds, so a lost worker whose tasks were reclaimed shows none. It reports `draining` while it drains on SIGTERM and `stopped` when it exits. A worker silent for 30 s is shown as **lost**: it was killed or its host went away. Rows are never deleted. |
| Retention | The built-in periodic `tasks.cleanup` task deletes finished rows in batches after the configured retention. Cancelled tasks share the failed retention. Events and logs go with their task (`ON DELETE CASCADE`). |
| Shutdown | SIGTERM stops claiming and drains running tasks for `shutdown_grace_seconds`. It then cancels the rest and hands them back to the queue without spending an attempt. The compose service sets `stop_grace_period: 45s` so Docker does not kill the worker mid-drain. |

## Built-in tasks

| Kind | What it does |
| --- | --- |
| `tasks.cleanup` | Hourly: deletes finished task rows past their retention. |
| `storage.purge_repository` | Deletes one LakeFS repository and its `s3://{bucket}/{lakefs_repo}/` prefix. Scheduled when a repository is deleted, when a user or organization is deleted with its repositories, or from the orphan audit on the admin **Storage** page (`kohakuhub/storage_cleanup.py`). Refuses a LakeFS id a repository still points at. |
| `storage.collect_lfs` | Decides each LFS object recorded in `lfs_gc_candidate` under a per-object lock and deletes the ones nothing relies on, leaving an `lfs_object_tombstone` (`kohakuhub/lfs_gc.py`, see [LFS garbage collection](../features/core/lfs.md#garbage-collection)). |
| `storage.expire_recent_lfs` | Hourly: turns LFS objects whose 24-hour upload/commit grace period ended into candidates, so uploads that were never committed are collected. |
| `storage.review_lfs_window` | Records the versions pushed out of a repository's keep windows after its `lfs_keep_versions` was lowered. |
| `storage.record_branch_links` | Records what a new branch's head links in `lfs_head_ref`, off the branch-creation request; collection waits while one is pending. |
| `storage.reconcile_lfs_references` | Makes the database account for every LFS object a branch head links: adds the missing `lfs_head_ref` rows, which collection never deletes, and corrects the default branch's file rows. Idempotent and non-destructive, checkpointed per repository. Started from the admin **Storage** page, or by the first collection with `lfs_auto_gc` on, which waits until one has completed. |
| `usage.recount` | Sets every repository's exact storage usage (or one namespace's) from LakeFS and the database, and reports the drift of the kept counters (`kohakuhub/usage.py`). Checkpointed per repository. Scheduled by migration 023, from the admin **Storage** page, by a namespace's quota recalculation, and every `usage_recount_interval_hours` when set. |
| `usage.recount_repository` | Recounts one repository whose kept usage could not follow a change (its `main` moved from a commit the counter does not count up to). A recount racing a change tries again 30 seconds later. |
| `repo.import` | Stages every file of a repository on a fallback source into the target's LakeFS staging area and commits the snapshot as one commit (`kohakuhub/api/importer`). Checkpointed per file, so a restarted worker resumes; staging is idempotent and the commit is the fence, so delivering it twice cannot make two snapshots. Its `repo.import.on_failure` hook records the outcome on the import a page reads (see [Import](../../features/core/import.md)). |
| `storage.forget_squashed_history` | Five minutes after a repository squash (when uploads under way then are committed or staged): lists the squash commit and every branch, then forgets the history rows of LFS versions the squash commit no longer links (their objects become collection candidates), marks deleted the file rows of paths neither it nor `main` has, and deletes the regular file objects under the repository's `data/` prefix that no branch links; rows and objects written after the squash are left alone. Then sets the repository's LFS usage from the history left. Re-runnable. |

## Admin panel

**Background Tasks** in the admin panel has two tabs. The status cards and the health chip above them stay visible on both.

- **Overview** is a dashboard for a selectable window (15m, 1h, 6h, 24h, 7d), served by `GET /admin/api/tasks/stats?window=`:
  - a health verdict with the reasons behind it;
  - KPI tiles: finished and throughput, success rate, due backlog and oldest wait, retrying, running and stuck, p50/p95 duration;
  - an activity chart of succeeded and failed tasks per bucket;
  - health by kind, with a timeline strip per kind;
  - errors grouped by exception class, counting final failures and pending retries separately.

  Clicking a kind, an error's kind chip, a status card or the **Running** tile opens **Tasks** already filtered by that kind or status. The other tiles summarise the window or part of a status, so they are not links.
- **Tasks** is the filterable task list:
  - each row shows its progress bar, count, stage and ETA, plus **stalled**
    and **cancelling** badges;
  - **Cancel** applies to queued and running tasks;
  - **Retry** and **Discard** apply to failed and cancelled tasks. Discard
    deletes the task with its timeline and logs.
  - **Details** opens four tabs, and refreshes with the page's auto-refresh:
    - **Overview**: state, progress, checkpoint, payload and last error.
    - **Timeline**: every event.
    - **Attempts**: one row per run, with worker, duration, outcome, log line
      count and its error or stages.
    - **Logs**: follows new records while the task runs, filters by attempt,
      and downloads the whole log or one attempt's as a `.log` file.

- **Workers** lists the worker roster, backed by `GET /admin/api/tasks/workers`:
  - each worker shows its name, id, pid, status (online, draining, lost, stopped) and last heartbeat, queues, load (running out of its concurrency), attempts succeeded and failed since it started, and the tasks it is running, each linking to its details;
  - lost and stopped workers silent for over 24 hours are hidden unless **Show workers inactive for over 24h** is ticked (`?include_inactive=true`);
  - the page header shows how many workers are online, and a task's **Worker** field jumps to its row.

  A worker's name is its hostname, which in Docker is the container id that `docker ps` shows. Set `KOHAKU_HUB_WORKER_NAME` to prefix it, for example `gpu-box-3f9a1c2e7b10`.

  The endpoints behind these are:
  - `POST /admin/api/tasks/{id}/cancel`;
  - `GET /admin/api/tasks/{id}/logs?attempt=&after_id=&limit=`, which pages
    and tails;
  - `GET /admin/api/tasks/{id}/logs/download?attempt=`.

The health verdict uses these rules:

| Signal | Degraded | Unhealthy |
| --- | --- | --- |
| Oldest due task waiting | ≥ 60 s | ≥ 5 min (the queue is not draining) |
| Failure rate in the window (≥ 5 finished) | ≥ 10 % | ≥ 50 % |
| Failures with fewer than 5 finished | any | — |
| Tasks waiting to retry | any | — |
| Running tasks flagged stalled | any | — |
| Due tasks but no worker online | — | any |
| Running tasks with an expired lease | — | any (no worker reclaimed them) |

A queued task whose kind no running worker registers never drains, so the backlog rule flags it too.

## Running the worker

Local development (start `make backend` first; it runs migrations and writes
the LakeFS credentials the worker reuses):

```bash
make worker
```

Docker Compose runs it as the `khub-worker` service from the same image as
`hub-api`. See `docker-compose.example.yml`. Run several replicas with
`KOHAKU_HUB_WORKER_REPLICAS=3 docker compose up -d`; see
[Running several workers](../deployment/docker.md#running-several-workers).
Tune it with the `KOHAKU_HUB_WORKER_*` variables in the
[configuration reference](../reference/config.md#background-task-worker-settings).
If no worker is running, tasks accumulate as `queued`; the API does not depend
on them.
