<script setup lang="ts">
/**
 * Image picker.
 *
 * Replaces a free-text field that asked people to recall an image reference from
 * memory. Free text there produces exactly one class of failure — a typo that
 * surfaces as `ImagePullBackOff` after the pod has already been scheduled — and
 * it makes "which images do we actually support?" unanswerable.
 *
 * The catalogue is a convenience, not a gate. Typing a reference the catalogue
 * has never seen still works, because a user building something new should not
 * be blocked by a list somebody else curates. The picker's job is to make the
 * common case a selection and the unusual case explicit, not to forbid the
 * unusual case.
 */
import { computed, onMounted, ref } from 'vue'

import { listImages } from '@/api/images'
import { ApiError } from '@/api/client'
import type { ImageSummary } from '@/types/image'
import { IMAGE_KIND_META } from '@/types/image'

const props = withDefaults(
  defineProps<{
    modelValue: string
    label?: string
    hint?: string
    disabled?: boolean
    /** Only offer entries already verified against the registry. */
    verifiedOnly?: boolean
  }>(),
  {
    label: '镜像',
    hint: '',
    disabled: false,
    verifiedOnly: false,
  },
)

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const images = ref<ImageSummary[]>([])
const loading = ref(false)
/** True once the catalogue has been fetched, so the fallback hint can be shown. */
const catalogueLoaded = ref(false)

/**
 * The catalogue is capped rather than paginated: this is a picker, and a picker
 * that needs "next page" to find the official base image has failed at its job.
 * Search is server-side, so the cap only limits what is browsable at once.
 */
const PAGE_SIZE = 200

async function load(search?: string): Promise<void> {
  loading.value = true
  try {
    const result = await listImages({ pageSize: PAGE_SIZE, search: search?.trim() || undefined })
    images.value = props.verifiedOnly ? result.items.filter((row) => row.verified) : result.items
    catalogueLoaded.value = true
  } catch (error) {
    // A picker that cannot reach the catalogue must not become an unusable form:
    // the text field below still accepts a reference.
    if (error instanceof ApiError) images.value = []
    else throw error
  } finally {
    loading.value = false
  }
}

onMounted(() => void load())

const options = computed(() =>
  images.value.map((image) => ({
    value: image.reference,
    title: image.displayName || image.reference,
    image,
  })),
)

/** The selected entry, when the current value names one we know about. */
const selected = computed<ImageSummary | null>(
  () => images.value.find((image) => image.reference === props.modelValue) ?? null,
)

/** True when the caller has typed something the catalogue does not contain. */
const isCustom = computed(
  () => props.modelValue.trim().length > 0 && catalogueLoaded.value && selected.value === null,
)

function onSelect(value: string | null): void {
  emit('update:modelValue', value ?? '')
}

function formatSize(bytes?: number | null): string {
  if (!bytes || bytes <= 0) return ''
  const gb = bytes / 1024 ** 3
  if (gb >= 1) return `${gb.toFixed(1)} GB`
  return `${(bytes / 1024 ** 2).toFixed(0)} MB`
}

function subtitle(image: ImageSummary): string {
  const parts: string[] = []
  if (image.framework) {
    parts.push(image.frameworkVersion ? `${image.framework} ${image.frameworkVersion}` : image.framework)
  }
  if (image.cudaVersion) parts.push(`CUDA ${image.cudaVersion}`)
  if (image.pythonVersion) parts.push(`Python ${image.pythonVersion}`)
  const size = formatSize(image.sizeBytes)
  if (size) parts.push(size)
  return parts.join(' · ') || image.reference
}
</script>

<template>
  <div>
    <v-select
      :model-value="modelValue || null"
      :items="options"
      item-title="title"
      item-value="value"
      :label="label"
      :disabled="disabled"
      :loading="loading"
      density="comfortable"
      variant="outlined"
      hide-details="auto"
      clearable
      :menu-props="{ maxHeight: 420 }"
      :no-data-text="loading ? '正在读取镜像目录…' : '镜像目录中还没有条目'"
      @update:model-value="onSelect"
    >
      <template #item="{ props: itemProps, item }">
        <v-list-item v-bind="itemProps" :subtitle="subtitle(item.image)">
          <template #prepend>
            <v-icon
              :icon="IMAGE_KIND_META[item.image.kind].icon"
              :color="IMAGE_KIND_META[item.image.kind].color"
            />
          </template>
          <template #append>
            <!-- `digest` present is what makes an entry reproducible; a tag can
                 be repushed under the same name. -->
            <v-icon
              v-if="item.image.isPinned"
              icon="mdi-pin"
              size="small"
              color="primary"
              title="管理员保留项"
            />
            <v-tooltip
              v-if="item.image.syncError"
              :text="`同步异常：${item.image.syncError}`"
              location="top"
            >
              <template #activator="{ props: tipProps }">
                <v-icon v-bind="tipProps" icon="mdi-alert-outline" size="small" color="warning" />
              </template>
            </v-tooltip>
          </template>
        </v-list-item>
      </template>

      <template #append-item>
        <v-divider class="my-1" />
        <v-list-item
          prepend-icon="mdi-open-in-new"
          title="管理镜像目录"
          subtitle="登记新镜像、查看详情与同步状态"
          to="/images"
        />
      </template>
    </v-select>

    <!--
      The selected entry's details, so the choice is informed rather than blind:
      "does this have PyTorch, and which CUDA does it need?" is the question a
      person is actually answering here.
    -->
    <v-expand-transition>
      <v-sheet
        v-if="selected"
        border
        rounded
        class="pa-3 mt-2"
        color="surface"
      >
        <div class="d-flex align-center flex-wrap ga-2 mb-1">
          <span class="text-body-2 font-weight-medium">{{ selected.displayName }}</span>
          <v-chip size="x-small" variant="tonal" :color="IMAGE_KIND_META[selected.kind].color">
            {{ IMAGE_KIND_META[selected.kind].title }}
          </v-chip>
          <v-chip v-if="selected.verified" size="x-small" variant="tonal" color="success">
            已核对
          </v-chip>
          <v-chip v-else size="x-small" variant="tonal" color="warning">未核对</v-chip>
        </div>
        <div class="text-caption text-medium-emphasis wl-mono text-truncate">
          {{ selected.reference }}
        </div>
        <div v-if="selected.digest" class="text-caption text-medium-emphasis wl-mono text-truncate">
          {{ selected.digest }}
        </div>
        <div v-if="selected.description" class="text-caption mt-1">{{ selected.description }}</div>
      </v-sheet>
    </v-expand-transition>

    <!--
      Free text stays available. A catalogue that cannot be typed past would
      block exactly the people building something the catalogue has not seen yet.
    -->
    <v-text-field
      v-if="isCustom"
      :model-value="modelValue"
      label="自定义镜像地址"
      density="comfortable"
      variant="outlined"
      hide-details="auto"
      class="mt-2"
      placeholder="registry.example.edu/project/name:tag"
      :disabled="disabled"
      :hint="hint || '目录中没有这个地址。请确认仓库与标签都存在，否则容器会停在 ImagePullBackOff。'"
      persistent-hint
      @update:model-value="onSelect"
    />
    <div v-else-if="hint" class="text-caption text-medium-emphasis mt-1">{{ hint }}</div>
  </div>
</template>

<style scoped lang="scss">
.wl-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace;
}
</style>
