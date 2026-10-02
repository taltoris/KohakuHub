<!-- src/kohaku-hub-ui/src/components/repo/ImportRepoDialog.vue -->
<template>
  <el-dialog
    v-model="visible"
    title="Import to my hub"
    width="620px"
    :close-on-click-modal="false"
    @closed="onClosed"
  >
    <div class="space-y-4">
      <el-alert type="info" :closable="false" show-icon>
        <template #title> A copy, not a clone </template>
        <div class="text-sm">
          The source's current files are copied into your namespace as one
          commit. Its commit history is not replayed, and a later import of the
          same repository adds the difference as a new commit.
        </div>
      </el-alert>

      <div>
        <div class="text-sm font-medium mb-1">From</div>
        <div class="font-mono text-sm break-all">
          {{ sourceRepoId }}
          <span class="text-gray-500 dark:text-gray-400">
            ({{ sourceName || "external source" }})
          </span>
        </div>
      </div>

      <div>
        <div class="text-sm font-medium mb-1">To</div>
        <el-input
          v-model="target"
          placeholder="your-username/repository-name"
          :disabled="submitting"
          @input="touched = true"
        />
        <div v-if="targetError" class="text-xs text-red-500 mt-1">
          {{ targetError }}
        </div>
        <div v-else class="text-xs text-gray-500 dark:text-gray-400 mt-1">
          The name must be free: importing into an existing repository would
          merge two histories.
        </div>
      </div>

      <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <div class="text-sm font-medium mb-1">Revision</div>
          <el-input
            v-model="revision"
            :disabled="submitting"
            placeholder="main"
          />
        </div>
        <div>
          <div class="text-sm font-medium mb-1">Visibility</div>
          <el-radio-group v-model="visibility" :disabled="submitting">
            <el-radio value="public">Public</el-radio>
            <el-radio value="private">Private</el-radio>
          </el-radio-group>
        </div>
      </div>

      <div
        v-if="sourceUrl"
        class="text-xs text-gray-500 dark:text-gray-400 break-all"
      >
        Read from {{ sourceUrl }}. Add a token for this source in your settings
        if the import is refused.
      </div>
    </div>

    <template #footer>
      <el-button :disabled="submitting" @click="visible = false"
        >Cancel</el-button
      >
      <el-button
        type="primary"
        :loading="submitting"
        :disabled="Boolean(targetError)"
        @click="submit"
      >
        Import
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { ElMessage } from "element-plus";

import { repoAPI } from "@/utils/api";
import { defaultTargetRepoId, validateTargetRepoId } from "@/utils/repo-import";

const props = defineProps({
  /** Whether the dialog is shown (v-model). */
  modelValue: { type: Boolean, default: false },
  /** Repository type the import creates. */
  repoType: { type: String, required: true },
  /** `namespace/name` at the source. */
  sourceRepoId: { type: String, required: true },
  /** The source's display name, e.g. "HuggingFace". */
  sourceName: { type: String, default: "" },
  /** The source's base URL, shown so a refusal names something actionable. */
  sourceUrl: { type: String, default: "" },
  /** The signed-in username, the default destination namespace. */
  username: { type: String, default: "" },
});

const emit = defineEmits(["update:modelValue", "started"]);

const visible = computed({
  get: () => props.modelValue,
  set: (value) => emit("update:modelValue", value),
});

const target = ref(defaultTargetRepoId(props.username, props.sourceRepoId));
const revision = ref("main");
const visibility = ref("public");
const submitting = ref(false);
// Only complain after the user has touched the field or tried to submit, so
// the dialog does not open already showing an error.
const touched = ref(false);

watch(
  () => [props.username, props.sourceRepoId, props.modelValue],
  () => {
    if (props.modelValue) {
      target.value = defaultTargetRepoId(props.username, props.sourceRepoId);
      revision.value = "main";
      visibility.value = "public";
      touched.value = false;
    }
  },
);

const targetError = computed(() => {
  if (!touched.value) return null;
  return validateTargetRepoId(target.value);
});

function onClosed() {
  submitting.value = false;
}

async function submit() {
  touched.value = true;
  const problem = validateTargetRepoId(target.value);
  if (problem) return;

  submitting.value = true;
  try {
    const { data } = await repoAPI.startImport({
      source_repo_id: props.sourceRepoId,
      target_repo_id: target.value.trim(),
      repo_type: props.repoType,
      revision: revision.value.trim() || "main",
      private: visibility.value === "private",
    });
    ElMessage.success("Import started");
    emit("started", data);
    visible.value = false;
  } catch (err) {
    // The backend decided this in the request, so its message is the useful
    // one: "the name is taken", "it does not fit the quota", "you cannot read
    // it at the source".
    const detail = err.response?.data?.detail;
    const message =
      (typeof detail === "string" ? detail : detail?.error) ||
      "The import could not be started";
    ElMessage.error(message);
  } finally {
    submitting.value = false;
  }
}
</script>
