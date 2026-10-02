<!-- src/kohaku-hub-ui/src/components/repo/ImportStatusBanner.vue -->
<!--
  What the repository page says about an import.

  A repository whose import did not finish holds part of a snapshot. That is
  the one thing this banner exists to make impossible to miss: a partial copy
  shown as a small repository would be read as "this is all there is".
-->
<template>
  <div
    v-if="view && view.state !== 'succeeded'"
    class="card mb-6 border-l-4"
    :class="borderClass"
    data-testid="import-status-banner"
  >
    <div class="flex flex-col sm:flex-row sm:items-start gap-3">
      <div class="flex-shrink-0 mt-1">
        <div :class="iconClass" class="text-2xl" />
      </div>

      <div class="min-w-0 flex-1">
        <div class="flex items-center gap-2 flex-wrap">
          <span class="font-semibold">{{ view.label }}</span>
          <el-tag v-if="importState.source" size="small" type="info">
            {{ importState.source }}
          </el-tag>
        </div>

        <p class="text-sm text-gray-600 dark:text-gray-400 mt-1 break-words">
          {{ description }}
        </p>

        <!-- In flight: progress, counters, estimate -->
        <div v-if="view.inFlight" class="mt-3">
          <el-progress
            v-if="progress.percent !== null"
            :percentage="Math.round(progress.percent)"
            :stroke-width="10"
          />
          <el-progress
            v-else
            :percentage="0"
            :indeterminate="true"
            :stroke-width="10"
          />
          <div class="text-xs text-gray-500 dark:text-gray-400 mt-2">
            <span v-if="progress.filesTotal > 0">
              {{ progress.filesDone }} of {{ progress.filesTotal }} files
            </span>
            <span v-if="progress.bytesTotal > 0">
              · {{ formatBytes(progress.bytesDone) }} of
              {{ formatBytes(progress.bytesTotal) }}
            </span>
            <span v-if="eta"> · {{ eta }}</span>
          </div>
        </div>

        <!-- Terminal: what happened, and what to do about it -->
        <div
          v-else-if="importState.error"
          class="text-sm mt-2"
          :class="errorClass"
        >
          {{ importState.error }}
        </div>

        <!-- The task row holds the log, the attempts and the timeline; it is
             what an operator looks at in the admin portal's task view. -->
        <div
          v-if="importState.task_id"
          class="text-xs text-gray-500 dark:text-gray-400 mt-2"
        >
          Background task #{{ importState.task_id }}
        </div>

        <div class="flex flex-wrap gap-2 mt-3">
          <el-button
            v-if="view.canCancel"
            size="small"
            :loading="cancelling"
            @click="cancel"
          >
            Stop import
          </el-button>
          <el-button
            v-if="view.canDelete"
            size="small"
            type="danger"
            plain
            :loading="deleting"
            @click="deletePartial"
          >
            Delete the partial repository
          </el-button>
          <el-button
            v-if="view.canRetry"
            size="small"
            plain
            @click="$emit('retry')"
          >
            Import again
          </el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

import { repoAPI } from "@/utils/api";
import {
  formatBytes,
  formatEta,
  importDescription,
  importProgress,
  importStateView,
} from "@/utils/repo-import";

const props = defineProps({
  /** The `import` object of `/api/repos/import/latest`, or null. */
  importState: { type: Object, default: null },
  /** Repository type, for the delete call. */
  repoType: { type: String, required: true },
});

const emit = defineEmits(["cancelled", "deleted", "retry"]);

const cancelling = ref(false);
const deleting = ref(false);

const view = computed(() =>
  props.importState ? importStateView(props.importState.state) : null,
);
const progress = computed(() => importProgress(props.importState));
const description = computed(() => importDescription(props.importState));
const eta = computed(() => formatEta(props.importState?.eta_seconds));

const BORDERS = {
  info: "border-blue-500",
  success: "border-green-500",
  warning: "border-yellow-500",
  danger: "border-red-500",
};
const ICONS = {
  info: "i-carbon-in-progress",
  success: "i-carbon-checkmark-filled",
  warning: "i-carbon-warning-alt",
  danger: "i-carbon-warning-filled",
};
const borderClass = computed(() => BORDERS[view.value?.tone] || BORDERS.info);
const iconClass = computed(() => ICONS[view.value?.tone] || ICONS.info);
const errorClass = computed(() =>
  view.value?.tone === "danger"
    ? "text-red-600 dark:text-red-400"
    : "text-yellow-700 dark:text-yellow-500",
);

async function cancel() {
  if (!props.importState?.task_id) return;
  cancelling.value = true;
  try {
    const { data } = await repoAPI.cancelImport(props.importState.task_id);
    ElMessage.success("The import will stop shortly");
    emit("cancelled", data);
  } catch (err) {
    const detail = err.response?.data?.detail;
    ElMessage.error(
      (typeof detail === "string" ? detail : detail?.error) ||
        "Could not cancel the import",
    );
  } finally {
    cancelling.value = false;
  }
}

async function deletePartial() {
  try {
    await ElMessageBox.confirm(
      `Delete ${props.importState?.repo_id}? What was imported so far is removed. ` +
        "You can import it again afterwards.",
      "Delete the partial repository",
      {
        type: "warning",
        confirmButtonText: "Delete",
        cancelButtonText: "Keep it",
      },
    );
  } catch {
    return; // dismissed
  }
  deleting.value = true;
  try {
    const { repo_id } = props.importState;
    const [namespace, name] = String(repo_id).split("/");
    await repoAPI.delete({
      type: props.repoType,
      name,
      organization: namespace,
    });
    ElMessage.success("Deleted");
    emit("deleted", repo_id);
  } catch (err) {
    const detail = err.response?.data?.detail;
    ElMessage.error(
      (typeof detail === "string" ? detail : detail?.error) ||
        "Could not delete the repository",
    );
  } finally {
    deleting.value = false;
  }
}
</script>
