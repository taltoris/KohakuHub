// src/kohaku-hub-ui/src/utils/repo-import.js
//
// Importing a repository from a fallback source into this hub
// (`kohakuhub.api.importer`). The state machine, the wording and the number
// formatting live here rather than in the components so they are unit-testable
// without mounting anything — the same split `repo-paths.js` and `file-preview.js`
// use.

/**
 * The import states the backend reports (`RepositoryImport.state`).
 * `queued` and `running` are in flight; the rest are terminal.
 */
export const IMPORT_STATES = [
  "queued",
  "running",
  "succeeded",
  "failed",
  "cancelled",
];

const TERMINAL_STATES = ["succeeded", "failed", "cancelled"];

/**
 * Split `namespace/name` into its two parts.
 * @param {string} repoId
 * @returns {{namespace: string, name: string}|null} null when it is not a repo id
 */
export function splitRepoId(repoId) {
  if (typeof repoId !== "string") return null;
  const trimmed = repoId.trim().replace(/^\/+|\/+$/g, "");
  if (!trimmed) return null;
  const parts = trimmed.split("/");
  if (parts.length !== 2) return null;
  const [namespace, name] = parts;
  if (!namespace || !name) return null;
  return { namespace, name };
}

/**
 * Where an import lands when the user does not say: their own namespace, under
 * the source repository's own name.
 * @param {string} username
 * @param {string} sourceRepoId
 * @returns {string} `username/name`, or "" when the source id is malformed
 */
export function defaultTargetRepoId(username, sourceRepoId) {
  const source = splitRepoId(sourceRepoId);
  if (!source || !username) return "";
  return `${username}/${source.name}`;
}

/**
 * Validate a target the user typed, before the request is made.
 *
 * The backend refuses a taken name with a 409; this only catches what it can
 * be told about without asking, so the obvious mistakes never cost a round
 * trip.
 * @param {string} value
 * @returns {string|null} the problem, or null when it is acceptable
 */
export function validateTargetRepoId(value) {
  if (typeof value !== "string" || !value.trim()) {
    return "Enter a name for the imported repository";
  }
  const parsed = splitRepoId(value);
  if (!parsed) {
    return "Use namespace/name, for example your-username/my-model";
  }
  // Same rule as the backend's own name validation: no spaces, no slashes,
  // nothing that would need escaping in a URL path or a LakeFS id.
  if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(parsed.name)) {
    return "Use letters, numbers, dots, dashes and underscores in the name";
  }
  return null;
}

/**
 * Everything a progress bar needs, from either the import row or a task row.
 *
 * The backend reports files *and* bytes; bytes are what the bar follows while
 * the source is still being listed, because a single 8 GB safetensors is one
 * file and reporting "0 of 3 files" for twenty minutes explains nothing.
 * @param {{files_done?: number, files_total?: number, bytes_done?: number, bytes_total?: number}} state
 * @returns {{percent: number|null, filesDone: number, filesTotal: number, bytesDone: number, bytesTotal: number}}
 */
export function importProgress(state) {
  const filesDone = Number(state?.files_done) || 0;
  const filesTotal = Number(state?.files_total) || 0;
  const bytesDone = Number(state?.bytes_done) || 0;
  const bytesTotal = Number(state?.bytes_total) || 0;
  let percent = null;
  if (bytesTotal > 0 && bytesDone > 0) {
    percent = (bytesDone / bytesTotal) * 100;
  } else if (filesTotal > 0 && filesDone > 0) {
    percent = (filesDone / filesTotal) * 100;
  }
  if (percent !== null) percent = Math.min(100, Math.max(0, percent));
  return { percent, filesDone, filesTotal, bytesDone, bytesTotal };
}

/**
 * Bytes as people read them.
 * @param {number|null|undefined} bytes
 * @returns {string}
 */
export function formatBytes(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  if (value < 1000) return `${value} B`;
  if (value < 1000 * 1000) return `${(value / 1000).toFixed(1)} KB`;
  if (value < 1000 * 1000 * 1000)
    return `${(value / (1000 * 1000)).toFixed(1)} MB`;
  return `${(value / (1000 * 1000 * 1000)).toFixed(2)} GB`;
}

/**
 * A remaining-time estimate as people read it.
 * @param {number|null|undefined} seconds
 * @returns {string} "" when there is nothing worth saying
 */
export function formatEta(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return "";
  if (value < 60) return "less than a minute left";
  const minutes = Math.round(value / 60);
  if (minutes < 60)
    return `about ${minutes} minute${minutes === 1 ? "" : "s"} left`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `about ${hours} hour${hours === 1 ? "" : "s"} left`;
  const days = Math.round(hours / 24);
  return `about ${days} day${days === 1 ? "" : "s"} left`;
}

/**
 * How a state presents itself, so every surface words it the same way.
 *
 * `isPartial` is the important one: a failed or cancelled import left a
 * repository holding part of a snapshot, which must never look like a
 * complete repository that happens to be small.
 * @param {string|undefined|null} state
 * @returns {{state: string, label: string, tone: string, terminal: boolean,
 *            inFlight: boolean, isPartial: boolean, canCancel: boolean,
 *            canDelete: boolean, canRetry: boolean}}
 */
export function importStateView(state) {
  const known = IMPORT_STATES.includes(state) ? state : "queued";
  const base = {
    state: known,
    terminal: TERMINAL_STATES.includes(known),
    inFlight: !TERMINAL_STATES.includes(known),
    isPartial: known === "failed" || known === "cancelled",
    canCancel: !TERMINAL_STATES.includes(known),
    canDelete: false,
    canRetry: false,
  };
  switch (known) {
    case "queued":
      return { ...base, label: "Waiting to start", tone: "info" };
    case "running":
      return { ...base, label: "Importing", tone: "info" };
    case "succeeded":
      return { ...base, label: "Imported", tone: "success" };
    case "failed":
      // Deleting is how a partial import is undone, and the backend's own
      // advice says so; there is no resume, so a fresh import is the retry.
      return {
        ...base,
        label: "Import failed",
        tone: "danger",
        canDelete: true,
        canRetry: true,
      };
    case "cancelled":
      return {
        ...base,
        label: "Import cancelled",
        tone: "warning",
        canDelete: true,
        canRetry: true,
      };
    default:
      return { ...base, label: "Waiting to start", tone: "info" };
  }
}

/**
 * The sentence under the label, naming the source and what happened.
 * @param {object} state - an `/api/repos/import/latest` payload's `import`
 * @returns {string}
 */
export function importDescription(state) {
  const source = state?.source || state?.source_url || "the external source";
  const sourceRepo = state?.source_repo_id || "";
  const revision = state?.source_revision || "main";
  const from = sourceRepo ? `${sourceRepo} on ${source}` : source;
  switch (importStateView(state?.state).state) {
    case "running":
    case "queued":
      return `Copying ${from} into this hub. The repository stays empty until the copy finishes.`;
    case "succeeded":
      return `Imported from ${from} as a single snapshot at ${revision}.`;
    default:
      return `This repository holds a partial copy of ${from}; the import did not finish.`;
  }
}

/**
 * Whether the repository page should offer to import a repository it is
 * showing from an external source.
 *
 * Both halves matter: an external repository has nothing local to import from
 * (there is a copy to pull), and an import needs a signed-in user with a
 * namespace to put it in.
 * @param {{isExternal: boolean, isAuthenticated: boolean, isImporting?: boolean}} options
 * @returns {boolean}
 */
export function canOfferImport({
  isExternal,
  isAuthenticated,
  isImporting = false,
}) {
  return Boolean(isExternal && isAuthenticated && !isImporting);
}
