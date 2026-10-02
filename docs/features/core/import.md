# Importing a repository from an external source

KohakuHub can serve a repository it does not hold: with a
[fallback source](core/fallback.md) configured, browsing `a-company/their-repo`
resolves through HuggingFace (or another KohakuHub) and a `hf_hub` client reads
it as if it were local. What that cannot do is make it local — until now the
only way was `hf download a-company/their-repo` and then uploading it here by
hand.

**Import** is that missing step: the server downloads the repository itself and
commits it into your hub.

## From the web UI

1. Open a repository that is being served from a fallback source. Its header
   shows a badge naming the source and a **View on …** link.
2. Click **Import to my hub**.
3. The dialog fills in where it is going: your username, under the source
   repository's own name. Change it if you want, and choose whether the copy
   is public or private.
4. Confirm. The repository page then shows the import running, with files and
   bytes done, and an estimate.

A repository that is only on the source must first be found through the
fallback path, which is why the button appears on an external repository rather
than in the "new repository" form.

## What an import does

**It is a snapshot, not a clone.** The source's current files become **one
commit**. HuggingFace repositories have long commit histories, and replaying
theirs into LakeFS faithfully is a much larger job than copying the files; a
snapshot is what an import means here. Importing the same repository again
later commits the difference, so the repository does gain history over time.

**It is one source.** The source the repository was found at is pinned for the
whole import and recorded on the import. Falling back per file would let a
snapshot come half from one Hub and half from another, and a repository that
answers at two sources is two different repositories
([#75](https://github.com/deepghs/KohakuHub/issues/75)).

**Everything refusable is refused up front**, in the request that asks for the
import, so nothing downloads before the answer is known:

| Check | Answer |
|---|---|
| The name you asked for is free | `409`; importing into an existing repository would merge two histories |
| You may use the target namespace | `403` |
| A source has the repository, and you may read it there | `404`, or the source's own refusal — a gated repository fails in a second, not after 20 GB |
| It fits the target namespace's quota | `413`, with nothing downloaded |
| It is not absurdly large | a cap on the file count, for a job nobody can reason about |

The target repository is created as soon as the import is accepted, **before**
anything is downloaded. The import then fills it. That is deliberate: a failure
leaves a repository holding part of a snapshot, which is visible and can be
deleted, rather than work that vanishes.

**A partial copy says so.** The repository page reads the import's state on
every load and shows a banner while it runs and after it fails. A repository
whose import failed is never presented as a complete one that happens to be
small. Deleting the partial repository is offered right there, and importing
again is the retry.

## LFS and regular files

Each file is written through the same code an upload uses, so an imported file
is byte-for-byte what an uploaded one is:

- a file over the repository's LFS threshold (or matching its suffix rules) is
  streamed to object storage and linked into LakeFS as an LFS object, claimed
  against garbage collection exactly as a commit does;
- anything smaller is committed inline.

Nothing is buffered in memory: the source is streamed, and a 1.5 GB
`safetensors` never becomes one Python `bytes`. When the source answers with a
redirect (HuggingFace points at a presigned CDN URL), the redirect is followed
**without** the source token — S3 rejects a presigned request that also carries
an `Authorization` header.

## Private and gated sources

An import reads the source with the credentials it was given:

- a token you have stored for that source (Settings → External Tokens),
- a token sent in the request's `Authorization` header, or
- the source's own admin-configured token.

Your token is stored on the import, encrypted like any external token, because
the worker has no request of its own.

## Following it, and stopping it

| Endpoint | Purpose |
|---|---|
| `POST /api/repos/import` | Accept an import; answers with the task to follow |
| `GET /api/repos/import/{task_id}` | State, stage, progress, ETA, error |
| `POST /api/repos/import/{task_id}/cancel` | Ask it to stop |
| `GET /api/repos/import/latest?repo_id=…` | The newest import of a repository |

`POST /api/repos/import` takes:

```json
{
  "source_repo_id": "a-company/their-repo",
  "target_repo_id": "you/their-repo",
  "repo_type": "model",
  "revision": "main",
  "private": false,
  "source": "https://huggingface.co",
  "max_bytes": null
}
```

`target_repo_id`, `source` and `max_bytes` are optional; the target defaults to
`<your username>/<source name>`.

Cancelling stops the import where it is. What was staged stays staged and the
partial repository stays for you to delete — stopping is not the same as
undoing.

An import is a [background task](../development/background-tasks.md)
(`repo.import`), so it survives a restart: it records how far it got and
resumes, and running it twice cannot commit two snapshots. Its task id is on the
admin portal's task view, with the log and the timeline.

## Not the CLI

`kohub-cli` lives in
[its own repository](https://github.com/KohakuBlueleaf/Kohub-CLI); the import
feature is driven from the web UI. The API above is what it drives, so a CLI
command can be added there without anything changing here.
