import { describe, expect, it } from "vitest";

import {
  canOfferImport,
  defaultTargetRepoId,
  formatBytes,
  formatEta,
  IMPORT_STATES,
  importDescription,
  importProgress,
  importStateView,
  splitRepoId,
  validateTargetRepoId,
} from "@/utils/repo-import";

describe("repo import utilities", () => {
  describe("splitRepoId", () => {
    it("splits a plain repo id", () => {
      expect(splitRepoId("alice/my-model")).toEqual({
        namespace: "alice",
        name: "my-model",
      });
    });

    it("ignores surrounding slashes and whitespace", () => {
      expect(splitRepoId("  /alice/my-model/  ")).toEqual({
        namespace: "alice",
        name: "my-model",
      });
    });

    it("rejects anything that is not exactly namespace/name", () => {
      expect(splitRepoId("my-model")).toBeNull();
      expect(splitRepoId("a/b/c")).toBeNull();
      expect(splitRepoId("/")).toBeNull();
      expect(splitRepoId("")).toBeNull();
      expect(splitRepoId(null)).toBeNull();
      expect(splitRepoId(undefined)).toBeNull();
    });
  });

  describe("defaultTargetRepoId", () => {
    it("puts the source's own name in the user's namespace", () => {
      expect(defaultTargetRepoId("bob", "a-company/their-repo")).toBe(
        "bob/their-repo",
      );
    });

    it("is empty when it cannot tell", () => {
      expect(defaultTargetRepoId("", "a-company/their-repo")).toBe("");
      expect(defaultTargetRepoId("bob", "their-repo")).toBe("");
    });
  });

  describe("validateTargetRepoId", () => {
    it("accepts an ordinary repo id", () => {
      expect(validateTargetRepoId("bob/their-repo")).toBeNull();
      expect(validateTargetRepoId("bob/Model.v2_final")).toBeNull();
    });

    it("asks for a name when the field is empty", () => {
      expect(validateTargetRepoId("")).toMatch(/Enter a name/);
      expect(validateTargetRepoId("   ")).toMatch(/Enter a name/);
      expect(validateTargetRepoId(undefined)).toMatch(/Enter a name/);
    });

    it("asks for namespace/name when only one part is given", () => {
      expect(validateTargetRepoId("their-repo")).toMatch(/namespace\/name/);
      expect(validateTargetRepoId("a/b/c")).toMatch(/namespace\/name/);
    });

    it("rejects a name the backend would reject", () => {
      expect(validateTargetRepoId("bob/my model")).toMatch(/letters, numbers/);
      expect(validateTargetRepoId("bob/-leading")).toMatch(/letters, numbers/);
      expect(validateTargetRepoId("bob/emoji🎉")).toMatch(/letters, numbers/);
    });
  });

  describe("importProgress", () => {
    it("follows bytes when it knows the total", () => {
      const progress = importProgress({
        files_done: 1,
        files_total: 10,
        bytes_done: 500,
        bytes_total: 1000,
      });
      expect(progress.percent).toBe(50);
    });

    it("falls back to files when the size is not known yet", () => {
      // A source that does not report sizes: bytes stay 0, files still say
      // something useful.
      const progress = importProgress({ files_done: 3, files_total: 12 });
      expect(progress.percent).toBe(25);
      expect(progress.bytesDone).toBe(0);
      expect(progress.bytesTotal).toBe(0);
    });

    it("has no percentage before anything has arrived", () => {
      expect(
        importProgress({ files_done: 0, files_total: 10 }).percent,
      ).toBeNull();
      expect(importProgress({}).percent).toBeNull();
      expect(importProgress(null).percent).toBeNull();
    });

    it("never goes past 100 percent", () => {
      // The source grew between listing and downloading, or the counters
      // disagree; a bar over 100 reads as a bug.
      expect(
        importProgress({ bytes_done: 2000, bytes_total: 1000 }).percent,
      ).toBe(100);
    });

    it("never goes below zero", () => {
      expect(
        importProgress({ bytes_done: -5, bytes_total: 1000 }).percent,
      ).toBeNull();
    });
  });

  describe("formatBytes", () => {
    it("scales to the unit people read", () => {
      expect(formatBytes(0)).toBe("0 B");
      expect(formatBytes(999)).toBe("999 B");
      expect(formatBytes(1500)).toBe("1.5 KB");
      expect(formatBytes(2_500_000)).toBe("2.5 MB");
      expect(formatBytes(3_000_000_000)).toBe("3.00 GB");
    });

    it("is quiet about nothing", () => {
      expect(formatBytes(null)).toBe("0 B");
      expect(formatBytes(undefined)).toBe("0 B");
      expect(formatBytes("nonsense")).toBe("0 B");
    });
  });

  describe("formatEta", () => {
    it("says nothing when there is nothing to say", () => {
      expect(formatEta(0)).toBe("");
      expect(formatEta(null)).toBe("");
      expect(formatEta(undefined)).toBe("");
      expect(formatEta(-1)).toBe("");
    });

    it("says minutes, then hours, then days", () => {
      expect(formatEta(30)).toBe("less than a minute left");
      expect(formatEta(60)).toBe("about 1 minute left");
      expect(formatEta(120)).toBe("about 2 minutes left");
      expect(formatEta(3600)).toBe("about 1 hour left");
      expect(formatEta(7200)).toBe("about 2 hours left");
      expect(formatEta(86400 * 3)).toBe("about 3 days left");
    });
  });

  describe("importStateView", () => {
    it("knows every state the backend reports", () => {
      for (const state of IMPORT_STATES) {
        const view = importStateView(state);
        expect(view.state).toBe(state);
        expect(typeof view.label).toBe("string");
      }
    });

    it("treats an unknown or missing state as queued", () => {
      // Being wrong here would show a running import as finished, so an
      // unrecognized state must not look terminal.
      expect(importStateView("who-knows").state).toBe("queued");
      expect(importStateView(undefined).state).toBe("queued");
      expect(importStateView(null).state).toBe("queued");
      expect(importStateView("who-knows").terminal).toBe(false);
    });

    it("calls in-flight states in flight and cancellable", () => {
      for (const state of ["queued", "running"]) {
        const view = importStateView(state);
        expect(view.inFlight).toBe(true);
        expect(view.terminal).toBe(false);
        expect(view.canCancel).toBe(true);
        expect(view.isPartial).toBe(false);
      }
    });

    it("marks a failed or cancelled import as a partial copy", () => {
      // The whole point of the banner: part of a snapshot is not a small
      // repository, and must never be presented as one.
      for (const state of ["failed", "cancelled"]) {
        const view = importStateView(state);
        expect(view.isPartial).toBe(true);
        expect(view.terminal).toBe(true);
        expect(view.canCancel).toBe(false);
        expect(view.canDelete).toBe(true);
        expect(view.canRetry).toBe(true);
      }
    });

    it("leaves nothing to undo on success", () => {
      const view = importStateView("succeeded");
      expect(view.tone).toBe("success");
      expect(view.terminal).toBe(true);
      expect(view.isPartial).toBe(false);
      expect(view.canCancel).toBe(false);
      expect(view.canDelete).toBe(false);
      expect(view.canRetry).toBe(false);
    });

    it("tones danger red and cancelled yellow", () => {
      expect(importStateView("failed").tone).toBe("danger");
      expect(importStateView("cancelled").tone).toBe("warning");
    });
  });

  describe("importDescription", () => {
    const base = {
      source: "HuggingFace",
      source_repo_id: "a-company/their-repo",
      source_revision: "main",
    };

    it("names the source and says the repository stays empty while it runs", () => {
      const text = importDescription({ ...base, state: "running" });
      expect(text).toContain("a-company/their-repo");
      expect(text).toContain("HuggingFace");
      expect(text).toMatch(/empty until the copy finishes/);
    });

    it("says a finished import is one snapshot", () => {
      const text = importDescription({ ...base, state: "succeeded" });
      expect(text).toMatch(/single snapshot/);
      expect(text).toContain("main");
    });

    it("says a failed import left a partial copy", () => {
      expect(importDescription({ ...base, state: "failed" })).toMatch(
        /partial copy/,
      );
      expect(importDescription({ ...base, state: "cancelled" })).toMatch(
        /partial copy/,
      );
    });

    it("still reads sensibly without a source name or repo id", () => {
      const text = importDescription({
        state: "failed",
        source_url: "https://hf.example",
      });
      expect(text).toContain("https://hf.example");
      expect(text).toMatch(/partial copy/);
    });
  });

  describe("canOfferImport", () => {
    it("offers it on an external repository to a signed-in user", () => {
      expect(canOfferImport({ isExternal: true, isAuthenticated: true })).toBe(
        true,
      );
    });

    it("does not offer it on a local repository", () => {
      // There is no copy to pull: it is already here.
      expect(canOfferImport({ isExternal: false, isAuthenticated: true })).toBe(
        false,
      );
    });

    it("does not offer it to a visitor", () => {
      // An import needs a namespace to land in.
      expect(canOfferImport({ isExternal: true, isAuthenticated: false })).toBe(
        false,
      );
    });

    it("does not offer it while one is already running at this repository", () => {
      expect(
        canOfferImport({
          isExternal: true,
          isAuthenticated: true,
          isImporting: true,
        }),
      ).toBe(false);
    });

    it("is false rather than throwing on missing options", () => {
      expect(canOfferImport({})).toBe(false);
    });
  });
});
