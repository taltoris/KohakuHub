import { flushPromises, mount } from "@vue/test-utils";
import { createPinia, setActivePinia } from "pinia";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { http } from "@/testing/msw";
import { ElementPlusStubs, RouterLinkStub } from "../helpers/vue";
import {
  cloneFixture,
  jsonResponse,
  uiApiFixtures,
} from "../helpers/api-fixtures";
import { server } from "../setup/msw-server";

const mocks = vi.hoisted(() => ({
  router: {
    push: vi.fn(),
    back: vi.fn(),
  },
}));

vi.mock("vue-router/auto", () => ({
  useRouter: () => mocks.router,
}));

import RepoViewer from "@/components/repo/RepoViewer.vue";
import ImportStatusBanner from "@/components/repo/ImportStatusBanner.vue";
import ImportRepoDialog from "@/components/repo/ImportRepoDialog.vue";
import { useAuthStore } from "@/stores/auth";

const NAMESPACE = "open-media-lab";

function repoInfoFor(name, extra = {}) {
  return {
    ...cloneFixture(uiApiFixtures.repo.info),
    id: `${NAMESPACE}/${name}`,
    ...extra,
  };
}

/** A local (imported) repository: no `_source`, so it is not an external one. */
function importedRepo(name, importState) {
  return repoInfoFor(name, {
    _source: "local",
    _import: importState,
  });
}

/** A repository served from a fallback source, as the fallback path returns it. */
function externalRepo(name, source = "HuggingFace") {
  return repoInfoFor(name, {
    _source: source,
    _source_url: "https://huggingface.co",
  });
}

describe("RepoViewer import-from-source UI", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setActivePinia(createPinia());
    vi.spyOn(console, "error").mockImplementation(() => {});
    vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  function installHandlers(repoResponse, { tree = true, type = true } = {}) {
    const handlers = [];
    if (type) {
      handlers.push(
        http.get("/api/users/:namespace/type", () =>
          jsonResponse({ type: "org" }),
        ),
      );
    }
    handlers.push(
      http.get("/api/datasets/:namespace/:name", () =>
        jsonResponse(repoResponse),
      ),
    );
    if (tree) {
      handlers.push(
        http.get("/api/datasets/:namespace/:name/tree/:revision", () =>
          jsonResponse(cloneFixture(uiApiFixtures.repo.tree)),
        ),
      );
    }
    server.use(...handlers);
  }

  function mountViewer(props = {}) {
    return mount(RepoViewer, {
      props: {
        repoType: "dataset",
        namespace: NAMESPACE,
        name: "their-repo",
        branch: "main",
        currentPath: "",
        tab: "files",
        ...props,
      },
      global: {
        stubs: {
          ...ElementPlusStubs,
          RouterLink: RouterLinkStub,
          MarkdownViewer: true,
          MetadataHeader: true,
          DetailedMetadataPanel: true,
          ReferencedDatasetsCard: true,
          SidebarRelationshipsCard: true,
          DatasetViewerTab: true,
        },
      },
    });
  }

  it("offers the import on an external repository to a signed-in user", async () => {
    setupSignedInUser();
    installHandlers(externalRepo("their-repo"));

    const wrapper = mountViewer();
    await flushPromises();

    const button = wrapper.find('[data-testid="import-to-hub"]');
    expect(button.exists()).toBe(true);
    expect(button.text()).toContain("Import to my hub");
  });

  it("does not offer the import on a local repository", async () => {
    setupSignedInUser();
    installHandlers(repoInfoFor("their-repo", { _source: "local" }));

    const wrapper = mountViewer();
    await flushPromises();

    // It is already here: there is no copy to pull.
    expect(wrapper.find('[data-testid="import-to-hub"]').exists()).toBe(false);
  });

  it("does not offer the import to a visitor", async () => {
    installHandlers(externalRepo("their-repo"));

    const wrapper = mountViewer();
    await flushPromises();

    // An import needs a namespace to land in.
    expect(wrapper.find('[data-testid="import-to-hub"]').exists()).toBe(false);
  });

  it("says a repository whose import is still running is being copied", async () => {
    setupSignedInUser();
    installHandlers(
      importedRepo("their-repo", {
        import_id: 7,
        repo_id: `${NAMESPACE}/their-repo`,
        state: "running",
        stage: "staging",
        source: "HuggingFace",
        source_repo_id: "a-company/their-repo",
        source_revision: "main",
        files_total: 10,
        files_done: 4,
        bytes_total: 1000,
        bytes_done: 400,
        task_id: 42,
      }),
    );

    const wrapper = mountViewer();
    await flushPromises();

    const banner = wrapper.findComponent(ImportStatusBanner);
    expect(banner.exists()).toBe(true);
    // The banner is the only thing standing between "partially imported" and
    // "this is all there is", so it must name both the source and the progress.
    expect(banner.text()).toContain("Importing");
    expect(banner.text()).toContain("a-company/their-repo");
    expect(banner.text()).toContain("4 of 10 files");
    expect(banner.text()).toContain("Background task #42");
  });

  it("says a failed import left a partial copy and offers to delete it", async () => {
    setupSignedInUser();
    installHandlers(
      importedRepo("their-repo", {
        import_id: 8,
        repo_id: `${NAMESPACE}/their-repo`,
        state: "failed",
        source: "HuggingFace",
        source_repo_id: "a-company/their-repo",
        source_revision: "main",
        files_total: 10,
        files_done: 3,
        bytes_total: 1000,
        bytes_done: 300,
        error: "The source stopped answering.",
        task_id: 43,
      }),
    );

    const wrapper = mountViewer();
    await flushPromises();

    const banner = wrapper.findComponent(ImportStatusBanner);
    expect(banner.exists()).toBe(true);
    expect(banner.text()).toContain("Import failed");
    expect(banner.text()).toContain("partial copy");
    expect(banner.text()).toContain("The source stopped answering.");
    expect(banner.text()).toContain("Delete the partial repository");
    expect(banner.text()).toContain("Import again");
  });

  it("stays quiet about an import that succeeded", async () => {
    setupSignedInUser();
    installHandlers(
      importedRepo("their-repo", {
        import_id: 9,
        repo_id: `${NAMESPACE}/their-repo`,
        state: "succeeded",
        source: "HuggingFace",
        source_repo_id: "a-company/their-repo",
        source_revision: "main",
        files_total: 10,
        files_done: 10,
        bytes_total: 1000,
        bytes_done: 1000,
      }),
    );

    const wrapper = mountViewer();
    await flushPromises();

    // Nothing to report: it is an ordinary local repository now, so no
    // banner renders (the component is mounted but decides to stay silent).
    expect(wrapper.find('[data-testid="import-status-banner"]').exists()).toBe(
      false,
    );
  });

  it("offers the import dialog with the source and a default target filled in", async () => {
    setupSignedInUser();
    installHandlers(externalRepo("their-repo"));

    const wrapper = mountViewer();
    await flushPromises();

    const dialog = wrapper.findComponent(ImportRepoDialog);
    expect(dialog.exists()).toBe(true);
    expect(dialog.props("sourceRepoId")).toBe(`${NAMESPACE}/their-repo`);
    expect(dialog.props("sourceName")).toBe("HuggingFace");
    // The default destination is the signed-in user's namespace.
    expect(dialog.props("username")).toBe("owner");
  });
});

function setupSignedInUser() {
  const auth = useAuthStore();
  auth.user = { username: "owner" };
  auth.userOrganizations = [];
  return auth;
}
