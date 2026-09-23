<script setup lang="ts">
/**
 * Image catalogue.
 *
 * A gallery rather than a table, for the same reason as the application
 * catalogue: an image is chosen for what is inside it — which framework, which
 * CUDA, how big — and a row of columns cannot carry that.
 *
 * Two things this page is careful about:
 *
 * - **Registering is not publishing.** Anyone may add an image to their own
 *   list; sharing it with a group or the whole installation is a claim about
 *   what every other lab will pull, so those controls appear only for holders of
 *   `image:share`.
 * - **The catalogue is a cache.** Harbor is the source of truth, so the page
 *   shows when an entry was last seen and whether a sync has ever confirmed it,
 *   rather than presenting every row as equally trustworthy.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import {
  deleteImage,
  getImage,
  getImageUsage,
  getRegistryStatus,
  listImageEvents,
  listImages,
  registerImage,
  syncImages,
  updateImage,
} from '@/api/images'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FilterBar from '@/components/common/FilterBar.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useNotificationStore } from '@/stores/notifications'
import type { WorkloadEvent } from '@/types/application'
import { CATEGORY_OPTIONS, type ApplicationCategory } from '@/types/application'
import type {
  ImageDetail,
  ImageKind,
  ImageSummary,
  ImageUsage,
  ImageVisibility,
  RegistryStatus,
} from '@/types/image'
import {
  FRAMEWORKS,
  IMAGE_KIND_META,
  IMAGE_VISIBILITY_META,
  SELF_SERVICE_VISIBILITIES,
} from '@/types/image'

const { can } = usePermissions()
const notifications = useNotificationStore()

const canCreate = computed(() => can('image:create'))
const canShare = computed(() => can('image:share'))
const canDelete = computed(() => can('image:delete'))

const categoryTitle = (value: string): string =>
  CATEGORY_OPTIONS.find((option) => option.value === value)?.title ?? value

// --- listing -----------------------------------------------------------------
const rows = ref<ImageSummary[]>([])
const total = ref(0)
const usage = ref<ImageUsage | null>(null)
const registry = ref<RegistryStatus | null>(null)
const loading = ref(false)
const page = ref(1)
const pageSize = ref(24)

const filters = reactive({
  search: '',
  category: null as ApplicationCategory | null,
  kind: null as ImageKind | null,
  framework: null as string | null,
  ownedOnly: false,
  includeInactive: false,
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listImages({
      page: page.value,
      pageSize: pageSize.value,
      search: filters.search.trim() || undefined,
      category: filters.category ?? undefined,
      kind: filters.kind ?? undefined,
      framework: filters.framework ?? undefined,
      ownedOnly: filters.ownedOnly || undefined,
      includeInactive: filters.includeInactive || undefined,
    })
    rows.value = result.items
    total.value = result.meta.total
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
  } finally {
    loading.value = false
  }
}

async function loadSidebar(): Promise<void> {
  try {
    usage.value = await getImageUsage()
  } catch {
    usage.value = null
  }
  try {
    registry.value = await getRegistryStatus()
  } catch {
    // A registry the platform cannot reach is worth showing but must not blank
    // the page: the catalogue still works from its cached rows.
    registry.value = null
  }
}

let searchTimer: ReturnType<typeof setTimeout> | undefined
watch(
  () => filters.search,
  () => {
    clearTimeout(searchTimer)
    searchTimer = setTimeout(() => {
      page.value = 1
      void load()
    }, 300)
  },
)
watch(
  () => [
    filters.category,
    filters.kind,
    filters.framework,
    filters.ownedOnly,
    filters.includeInactive,
    page.value,
    pageSize.value,
  ],
  () => void load(),
)

onMounted(async () => {
  await Promise.all([load(), loadSidebar()])
})

// --- register ----------------------------------------------------------------
const dialogOpen = ref(false)
const saving = ref(false)

const form = reactive({
  reference: '',
  displayName: '',
  description: '',
  category: 'other' as ApplicationCategory,
  kind: 'personal' as ImageKind,
  visibility: 'private' as ImageVisibility,
  framework: null as string | null,
  frameworkVersion: '',
  pythonVersion: '',
  cudaVersion: '',
  osVersion: '',
  isPinned: false,
})

/** Visibility levels this caller may choose. */
const visibilityOptions = computed(() =>
  Object.entries(IMAGE_VISIBILITY_META)
    .filter(([value]) => canShare.value || SELF_SERVICE_VISIBILITIES.includes(value as ImageVisibility))
    .map(([value, meta]) => ({ value: value as ImageVisibility, ...meta })),
)

/** Kinds this caller may publish. */
const kindOptions = computed(() =>
  (['personal', 'shared', 'official'] as ImageKind[])
    .filter((kind) => canShare.value || kind === 'personal')
    .map((kind) => ({ value: kind, ...IMAGE_KIND_META[kind] })),
)

function openRegister(): void {
  Object.assign(form, {
    reference: '',
    displayName: '',
    description: '',
    category: 'other',
    kind: 'personal',
    visibility: 'private',
    framework: null,
    frameworkVersion: '',
    pythonVersion: '',
    cudaVersion: '',
    osVersion: '',
    isPinned: false,
  })
  dialogOpen.value = true
}

const referenceValid = computed(() => form.reference.trim().length > 0)

/**
 * Where the reference will land, previewed as it is typed.
 *
 * A mirror of the backend split — the project is the first segment after the
 * host and the repository is everything after it — so the layout is visible
 * before submitting rather than discovered through a validation error. UX only:
 * the backend re-derives this and is the authority, and a wrong guess here can
 * never let a reference through.
 */
const referenceParts = computed(() => {
  const raw = form.reference.trim()
  if (!raw) return null
  const parts = raw.split('@')[0].split('/')
  if (parts.length > 1 && (parts[0].includes('.') || parts[0].includes(':') || parts[0] === 'localhost')) {
    parts.shift()
  }
  const last = parts.length - 1
  parts[last] = parts[last].replace(/:.*$/, '')
  if (parts.length <= 1) return { host: '', project: 'library', repository: parts[0] ?? '' }
  return { host: '', project: parts[0], repository: parts.slice(1).join('/') }
})

/**
 * Derive the display name from the reference as it is typed.
 *
 * Only while the user has not written one: replacing something they typed would
 * be the opposite of helpful.
 */
watch(
  () => form.reference,
  (value) => {
    if (form.displayName) return
    const tail = value.split('/').pop() ?? ''
    form.displayName = tail.replace(/[@:].*$/, '')
  },
)

async function save(): Promise<void> {
  if (!referenceValid.value) return
  saving.value = true
  try {
    const created = await registerImage({
      reference: form.reference.trim(),
      displayName: form.displayName.trim() || undefined,
      description: form.description || null,
      category: form.category,
      kind: form.kind,
      visibility: form.visibility,
      framework: form.framework,
      frameworkVersion: form.frameworkVersion || null,
      pythonVersion: form.pythonVersion || null,
      cudaVersion: form.cudaVersion || null,
      osVersion: form.osVersion || null,
      isPinned: form.isPinned || undefined,
    })
    notifications.success(`已登记 ${created.reference}`)
    dialogOpen.value = false
    await Promise.all([load(), loadSidebar()])
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    // The API confirms the reference with the registry before storing it, so a
    // failure here is usually "that image does not exist" — which is the point.
    // Two of the failures are about the layout rather than the image, and the
    // server sends the reference the caller should have used; showing only the
    // prose would make them work it out again by hand.
    const hint = typeof error.detail.hint === 'string' ? error.detail.hint : ''
    notifications.error(hint ? `${error.message}。建议：${hint}` : error.message)
  } finally {
    saving.value = false
  }
}

// --- sync --------------------------------------------------------------------
const syncing = ref(false)

/**
 * Refresh the catalogue from the registry.
 *
 * A deliberate action rather than something that happens on page load: it can
 * retire entries, and a page view must not silently change shared state.
 */
async function runSync(): Promise<void> {
  syncing.value = true
  try {
    const result = await syncImages()
    const parts = [`新增 ${result.created}`, `更新 ${result.updated}`, `回收 ${result.removed}`]
    if (result.failed) parts.push(`失败 ${result.failed}`)
    notifications.success(`同步完成：${parts.join('，')}`)
    if (result.errors.length) {
      notifications.info(result.errors.slice(0, 3).join('；'))
    }
    await Promise.all([load(), loadSidebar()])
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
  } finally {
    syncing.value = false
  }
}

// --- edit --------------------------------------------------------------------
const editOpen = ref(false)
const editing = ref<ImageDetail | null>(null)
const editSaving = ref(false)

const editForm = reactive({
  displayName: '',
  description: '',
  category: 'other' as ApplicationCategory,
  framework: null as string | null,
  frameworkVersion: '',
  pythonVersion: '',
  cudaVersion: '',
  osVersion: '',
  visibility: 'private' as ImageVisibility,
  kind: 'personal' as ImageKind,
  isPinned: false,
})

async function openEdit(row: ImageSummary): Promise<void> {
  try {
    const detail = await getImage(row.id)
    editing.value = detail
    Object.assign(editForm, {
      displayName: detail.displayName,
      description: detail.description ?? '',
      category: detail.category,
      framework: detail.framework,
      frameworkVersion: detail.frameworkVersion ?? '',
      pythonVersion: detail.pythonVersion ?? '',
      cudaVersion: detail.cudaVersion ?? '',
      osVersion: detail.osVersion ?? '',
      visibility: detail.visibility,
      kind: detail.kind,
      isPinned: detail.isPinned,
    })
    editOpen.value = true
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

async function saveEdit(): Promise<void> {
  if (!editing.value) return
  editSaving.value = true
  try {
    await updateImage(editing.value.id, {
      displayName: editForm.displayName || null,
      description: editForm.description || null,
      category: editForm.category,
      framework: editForm.framework,
      frameworkVersion: editForm.frameworkVersion || null,
      pythonVersion: editForm.pythonVersion || null,
      cudaVersion: editForm.cudaVersion || null,
      osVersion: editForm.osVersion || null,
      visibility: editForm.visibility,
      kind: editForm.kind,
      isPinned: editForm.isPinned,
    })
    notifications.success('已保存')
    editOpen.value = false
    await load()
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
  } finally {
    editSaving.value = false
  }
}

// --- delete ------------------------------------------------------------------
const deleteOpen = ref(false)
const deleteTarget = ref<ImageSummary | null>(null)
const deleting = ref(false)

function openDelete(row: ImageSummary): void {
  deleteTarget.value = row
  deleteOpen.value = true
}

async function confirmDelete(): Promise<void> {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    await deleteImage(deleteTarget.value.id)
    notifications.success('已从目录中移除')
    deleteOpen.value = false
    await Promise.all([load(), loadSidebar()])
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
  } finally {
    deleting.value = false
  }
}

// --- detail ------------------------------------------------------------------
const detailOpen = ref(false)
const detail = ref<ImageDetail | null>(null)
const events = ref<WorkloadEvent[]>([])
const detailLoading = ref(false)

async function openDetail(row: ImageSummary): Promise<void> {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  events.value = []
  try {
    detail.value = await getImage(row.id)
    events.value = await listImageEvents(row.id)
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    detailLoading.value = false
  }
}

const EVENT_LABELS: Record<string, string> = {
  created: '已登记',
  deleted: '已移除',
  updated: '已更新',
  failed: '失败',
  status_changed: '状态变化',
  started: '已开始',
  stopped: '已停止',
}

// --- helpers -----------------------------------------------------------------
async function copyReference(reference: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(reference)
    notifications.success('已复制镜像地址')
  } catch {
    // Clipboard access is denied in plenty of legitimate contexts; the
    // reference is visible on screen, so this is not worth an error dialog.
    notifications.info(reference)
  }
}

function formatSize(bytes?: number | null): string {
  if (!bytes || bytes <= 0) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`
}

function formatTime(value?: string | null): string {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

function specs(image: ImageSummary): string[] {
  const parts: string[] = []
  if (image.framework) {
    parts.push(
      image.frameworkVersion ? `${image.framework} ${image.frameworkVersion}` : image.framework,
    )
  }
  if (image.cudaVersion) parts.push(`CUDA ${image.cudaVersion}`)
  if (image.pythonVersion) parts.push(`Python ${image.pythonVersion}`)
  if (image.osVersion) parts.push(image.osVersion)
  return parts
}
</script>

<template>
  <div>
    <PageHeader title="镜像" subtitle="经过核对的镜像目录：不用再凭记忆输入镜像地址">
      <template #actions>
        <v-btn
          v-if="canShare"
          variant="tonal"
          prepend-icon="mdi-sync"
          :loading="syncing"
          @click="runSync"
        >
          同步仓库
        </v-btn>
        <v-btn
          v-if="canCreate"
          color="primary"
          variant="flat"
          prepend-icon="mdi-plus"
          @click="openRegister"
        >
          登记镜像
        </v-btn>
      </template>
    </PageHeader>

    <!--
      The registry banner is informational, not fatal: the catalogue works from
      its cached rows, and saying so is more useful than hiding the page.
    -->
    <v-alert
      v-if="registry && !registry.reachable"
      type="warning"
      variant="tonal"
      density="compact"
      class="mb-4"
      icon="mdi-server-off"
    >
      镜像仓库（{{ registry.host }}）当前不可达{{ registry.message ? `：${registry.message}` : '' }}。
      目录中的已有条目仍然可用，但登记新镜像会失败。
    </v-alert>

    <v-row v-if="usage" dense class="mb-2">
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">目录条目</div>
          <div class="text-h5">{{ usage.count }}</div>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">镜像总大小</div>
          <div class="text-h5">{{ formatSize(usage.totalBytes) }}</div>
        </v-card>
      </v-col>
      <v-col cols="12" sm="6" md="3">
        <v-card variant="flat" class="wl-stat">
          <div class="text-caption text-medium-emphasis">使用中</div>
          <div class="text-h5">{{ usage.inUse }}</div>
        </v-card>
      </v-col>
    </v-row>

    <FilterBar>
      <v-text-field
        v-model="filters.search"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        prepend-inner-icon="mdi-magnify"
        placeholder="搜索名称、仓库或标签"
        style="max-width: 280px"
      />
      <v-select
        v-model="filters.category"
        :items="CATEGORY_OPTIONS"
        item-title="title"
        item-value="value"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        label="分类"
        style="max-width: 170px"
      />
      <v-select
        v-model="filters.kind"
        :items="[
          { title: '官方镜像', value: 'official' },
          { title: '共享镜像', value: 'shared' },
          { title: '个人镜像', value: 'personal' },
        ]"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        label="类型"
        style="max-width: 160px"
      />
      <v-select
        v-model="filters.framework"
        :items="FRAMEWORKS"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        label="框架"
        style="max-width: 160px"
      />
      <v-switch
        v-model="filters.ownedOnly"
        label="只看我登记的"
        color="primary"
        density="compact"
        hide-details
        inset
      />
      <v-switch
        v-model="filters.includeInactive"
        label="包含已移除"
        color="primary"
        density="compact"
        hide-details
        inset
      />
    </FilterBar>

    <div v-if="loading" class="d-flex justify-center pa-8">
      <v-progress-circular indeterminate color="primary" />
    </div>

    <EmptyState
      v-else-if="!rows.length"
      icon="mdi-docker"
      title="镜像目录中还没有条目"
      description="登记一个镜像地址，平台会先向仓库确认它确实存在，再收进目录。"
    >
      <v-btn v-if="canCreate" color="primary" variant="flat" @click="openRegister">
        登记镜像
      </v-btn>
      <v-btn v-if="canShare" variant="tonal" class="ml-2" @click="runSync">从仓库同步</v-btn>
    </EmptyState>

    <v-row v-else dense>
      <v-col v-for="image in rows" :key="image.id" cols="12" sm="6" lg="4" xl="3">
        <v-card variant="flat" class="wl-image-card h-100 d-flex flex-column" @click="openDetail(image)">
          <div class="d-flex align-start ga-2 pa-4 pb-2">
            <v-avatar
              :color="IMAGE_KIND_META[image.kind].color"
              variant="tonal"
              size="40"
              rounded
            >
              <v-icon :icon="IMAGE_KIND_META[image.kind].icon" />
            </v-avatar>
            <div class="flex-grow-1 min-w-0">
              <div class="d-flex align-center ga-1">
                <span class="text-body-1 font-weight-medium text-truncate">
                  {{ image.displayName }}
                </span>
                <v-icon v-if="image.isPinned" icon="mdi-pin" size="x-small" color="primary" />
              </div>
              <div class="text-caption text-medium-emphasis wl-mono text-truncate">
                {{ image.repository }}:{{ image.tag }}
              </div>
            </div>
            <v-menu location="bottom end">
              <template #activator="{ props: menuProps }">
                <v-btn
                  v-bind="menuProps"
                  icon="mdi-dots-vertical"
                  size="small"
                  variant="text"
                  @click.stop
                />
              </template>
              <v-list density="compact">
                <v-list-item prepend-icon="mdi-content-copy" title="复制地址" @click="copyReference(image.reference)" />
                <v-list-item prepend-icon="mdi-pencil-outline" title="编辑" @click="openEdit(image)" />
                <v-list-item
                  prepend-icon="mdi-delete-outline"
                  title="移除"
                  :disabled="!canDelete"
                  @click="openDelete(image)"
                />
              </v-list>
            </v-menu>
          </div>

          <v-card-text class="pt-0 pb-2 flex-grow-1">
            <p v-if="image.description" class="text-body-2 mb-2 wl-clamp">{{ image.description }}</p>

            <div class="d-flex flex-wrap ga-1 mb-2">
              <v-chip size="x-small" variant="tonal" :color="IMAGE_KIND_META[image.kind].color">
                {{ IMAGE_KIND_META[image.kind].title }}
              </v-chip>
              <v-chip size="x-small" variant="tonal">
                {{ categoryTitle(image.category) }}
              </v-chip>
              <v-chip
                v-for="spec in specs(image)"
                :key="spec"
                size="x-small"
                variant="text"
              >
                {{ spec }}
              </v-chip>
            </div>
          </v-card-text>

          <v-divider />
          <div class="d-flex align-center justify-space-between px-4 py-2">
            <span class="text-caption text-medium-emphasis">{{ formatSize(image.sizeBytes) }}</span>
            <div class="d-flex align-center ga-2">
              <!--
                `verified` means a sync has actually seen this in the registry.
                Showing it matters: an entry confirmed once at registration is
                not the same as one the catalogue keeps rechecking.
              -->
              <v-tooltip
                :text="image.verified ? `已核对 · ${formatTime(image.syncedAt)}` : '尚未与仓库核对'"
                location="top"
              >
                <template #activator="{ props: tipProps }">
                  <v-icon
                    v-bind="tipProps"
                    :icon="image.verified ? 'mdi-check-decagram-outline' : 'mdi-help-circle-outline'"
                    :color="image.verified ? 'success' : 'warning'"
                    size="small"
                  />
                </template>
              </v-tooltip>
              <v-tooltip v-if="image.usageCount" :text="`${image.usageCount} 个工作负载正在使用`" location="top">
                <template #activator="{ props: tipProps }">
                  <v-chip v-bind="tipProps" size="x-small" variant="tonal" prepend-icon="mdi-cube-outline">
                    {{ image.usageCount }}
                  </v-chip>
                </template>
              </v-tooltip>
              <v-icon v-if="!image.isActive" icon="mdi-eye-off-outline" size="small" color="grey" />
            </div>
          </div>
        </v-card>
      </v-col>
    </v-row>

    <div v-if="total > pageSize" class="d-flex justify-center mt-4">
      <v-pagination
        v-model="page"
        :length="Math.ceil(total / pageSize)"
        :total-visible="7"
        density="comfortable"
      />
    </div>

    <!-- Register -->
    <v-dialog v-model="dialogOpen" max-width="700" persistent>
      <v-card>
        <v-card-title>登记镜像</v-card-title>
        <v-divider />
        <v-card-text>
          <v-alert type="info" variant="tonal" density="compact" class="mb-3">
            提交后会先向镜像仓库确认这个地址确实存在。打错的地址在这里就会被拦下，
            而不是等容器调度之后变成 ImagePullBackOff。
          </v-alert>
          <v-alert type="info" variant="tonal" density="compact" class="mb-3">
            镜像按<b>用户组</b>分仓：官方镜像放 <code>library</code>，你自己推的写成
            <code>仓库地址/组名/你的用户名/镜像名:标签</code>，组共享的写成
            <code>仓库地址/组名/share/镜像名:标签</code>。
          </v-alert>
          <v-row dense>
            <v-col cols="12">
              <v-text-field
                v-model="form.reference"
                label="镜像地址 *"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                placeholder="registry.example.edu/我的组/我的用户名/pytorch:2.4-cuda12"
                hint="完整的 host/组名/用户名/镜像名:标签；官方镜像用 host/library/…，组共享用 host/组名/share/…。也用 @sha256:… 固定到某个摘要。"
                persistent-hint
              />
            </v-col>
            <v-col v-if="referenceParts" cols="12">
              <div class="text-caption text-medium-emphasis d-flex flex-wrap ga-2 align-center">
                <span>解析为</span>
                <v-chip size="x-small" variant="tonal" color="primary">
                  项目 {{ referenceParts.project }}
                </v-chip>
                <v-chip size="x-small" variant="tonal">
                  仓库 {{ referenceParts.repository || '—' }}
                </v-chip>
                <span class="text-disabled">
                  项目名必须是用户组名或 library
                </span>
              </div>
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.displayName"
                label="显示名"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.category"
                :items="CATEGORY_OPTIONS"
                item-title="title"
                item-value="value"
                label="分类"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.framework"
                :items="FRAMEWORKS"
                label="框架"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                clearable
              />
            </v-col>
            <v-col cols="12" sm="3">
              <v-text-field
                v-model="form.frameworkVersion"
                label="框架版本"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              />
            </v-col>
            <v-col cols="12" sm="3">
              <v-text-field
                v-model="form.cudaVersion"
                label="CUDA"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                placeholder="12.1"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.pythonVersion"
                label="Python"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                placeholder="3.11"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.osVersion"
                label="基础系统"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                placeholder="ubuntu 22.04"
              />
            </v-col>
            <v-col cols="12">
              <v-textarea
                v-model="form.description"
                label="说明"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                rows="2"
                auto-grow
                placeholder="里面装了什么、适合做什么。"
              />
            </v-col>

            <v-col cols="12">
              <v-select
                v-model="form.visibility"
                :items="visibilityOptions"
                item-title="title"
                item-value="value"
                label="可见范围"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              >
                <template #item="{ props: itemProps, item }">
                  <v-list-item v-bind="itemProps" :subtitle="item.description">
                    <template #prepend><v-icon :icon="item.icon" /></template>
                  </v-list-item>
                </template>
              </v-select>
            </v-col>

            <v-col v-if="canShare" cols="12">
              <v-select
                v-model="form.kind"
                :items="kindOptions"
                item-title="title"
                item-value="value"
                label="类型"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              >
                <template #item="{ props: itemProps, item }">
                  <v-list-item v-bind="itemProps" :subtitle="item.description">
                    <template #prepend><v-icon :icon="item.icon" /></template>
                  </v-list-item>
                </template>
              </v-select>
            </v-col>

            <v-col v-if="canShare" cols="12">
              <v-switch
                v-model="form.isPinned"
                label="管理员保留（同步时不会被回收）"
                color="primary"
                density="compact"
                hide-details
                inset
              />
            </v-col>
          </v-row>
        </v-card-text>
        <v-divider />
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="dialogOpen = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="saving"
            :disabled="!referenceValid"
            @click="save"
          >
            核对并登记
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Edit -->
    <v-dialog v-model="editOpen" max-width="640" persistent>
      <v-card v-if="editing">
        <v-card-title>编辑镜像</v-card-title>
        <v-divider />
        <v-card-text>
          <div class="text-caption text-medium-emphasis wl-mono mb-3">{{ editing.reference }}</div>
          <v-row dense>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="editForm.displayName"
                label="显示名"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="editForm.category"
                :items="CATEGORY_OPTIONS"
                item-title="title"
                item-value="value"
                label="分类"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="editForm.framework"
                :items="FRAMEWORKS"
                label="框架"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                clearable
              />
            </v-col>
            <v-col cols="12" sm="3">
              <v-text-field
                v-model="editForm.frameworkVersion"
                label="框架版本"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              />
            </v-col>
            <v-col cols="12" sm="3">
              <v-text-field
                v-model="editForm.cudaVersion"
                label="CUDA"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              />
            </v-col>
            <v-col cols="12">
              <v-textarea
                v-model="editForm.description"
                label="说明"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
                rows="2"
                auto-grow
              />
            </v-col>
            <v-col cols="12">
              <v-select
                v-model="editForm.visibility"
                :items="visibilityOptions"
                item-title="title"
                item-value="value"
                label="可见范围"
                density="comfortable"
                variant="outlined"
                hide-details="auto"
              />
            </v-col>
            <v-col v-if="canShare" cols="12">
              <v-switch
                v-model="editForm.isPinned"
                label="管理员保留"
                color="primary"
                density="compact"
                hide-details
                inset
              />
            </v-col>
          </v-row>
        </v-card-text>
        <v-divider />
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="editOpen = false">取消</v-btn>
          <v-btn color="primary" variant="flat" :loading="editSaving" @click="saveEdit">
            保存
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="deleteOpen"
      title="移除镜像"
      :text="`确定要从目录中移除 ${deleteTarget?.displayName ?? ''} 吗？个人镜像的仓库 tag 也会一并删除。`"
      confirm-text="移除"
      confirm-color="error"
      :loading="deleting"
      @confirm="confirmDelete"
    />

    <!-- Detail -->
    <v-navigation-drawer v-model="detailOpen" location="right" temporary width="520">
      <div v-if="detailLoading" class="d-flex justify-center pa-8">
        <v-progress-circular indeterminate color="primary" />
      </div>
      <div v-else-if="detail" class="pa-4">
        <div class="d-flex align-center justify-space-between mb-3">
          <div class="min-w-0">
            <div class="text-h6 text-truncate">{{ detail.displayName }}</div>
            <div class="text-caption text-medium-emphasis wl-mono text-truncate">
              {{ detail.reference }}
            </div>
          </div>
          <v-btn icon="mdi-close" variant="text" @click="detailOpen = false" />
        </div>

        <div class="d-flex flex-wrap ga-2 mb-3">
          <v-chip size="small" variant="tonal" :color="IMAGE_KIND_META[detail.kind].color">
            {{ IMAGE_KIND_META[detail.kind].title }}
          </v-chip>
          <v-chip size="small" variant="tonal">
            {{ IMAGE_VISIBILITY_META[detail.visibility].title }}
          </v-chip>
          <v-chip size="small" variant="tonal">{{ categoryTitle(detail.category) }}</v-chip>
          <v-chip v-if="detail.isPinned" size="small" variant="tonal" color="primary" prepend-icon="mdi-pin">
            保留
          </v-chip>
        </div>

        <v-alert
          v-if="detail.syncError"
          type="warning"
          variant="tonal"
          density="compact"
          class="mb-3"
        >
          与仓库同步时出错：{{ detail.syncError }}
        </v-alert>

        <v-btn block variant="tonal" prepend-icon="mdi-content-copy" class="mb-3" @click="copyReference(detail.reference)">
          复制镜像地址
        </v-btn>

        <v-list density="compact" class="wl-detail-list">
          <v-list-item v-if="detail.digest">
            <template #prepend><v-icon icon="mdi-fingerprint" /></template>
            <v-list-item-title>摘要</v-list-item-title>
            <v-list-item-subtitle class="wl-mono">{{ detail.digest }}</v-list-item-subtitle>
          </v-list-item>
          <v-list-item>
            <template #prepend><v-icon icon="mdi-package-variant" /></template>
            <v-list-item-title>大小</v-list-item-title>
            <v-list-item-subtitle>
              {{ formatSize(detail.sizeBytes) }}
              <template v-if="detail.layerCount"> · {{ detail.layerCount }} 层</template>
            </v-list-item-subtitle>
          </v-list-item>
          <v-list-item v-if="specs(detail).length">
            <template #prepend><v-icon icon="mdi-cube-outline" /></template>
            <v-list-item-title>环境</v-list-item-title>
            <v-list-item-subtitle>{{ specs(detail).join(' · ') }}</v-list-item-subtitle>
          </v-list-item>
          <v-list-item>
            <template #prepend><v-icon icon="mdi-account-outline" /></template>
            <v-list-item-title>登记人</v-list-item-title>
            <v-list-item-subtitle>{{ detail.ownerUsername || '（系统同步）' }}</v-list-item-subtitle>
          </v-list-item>
          <v-list-item>
            <template #prepend><v-icon icon="mdi-sync" /></template>
            <v-list-item-title>最近核对</v-list-item-title>
            <v-list-item-subtitle>
              {{ detail.syncedAt ? formatTime(detail.syncedAt) : '尚未与仓库核对' }}
            </v-list-item-subtitle>
          </v-list-item>
          <v-list-item>
            <template #prepend><v-icon icon="mdi-cube-send" /></template>
            <v-list-item-title>使用中</v-list-item-title>
            <v-list-item-subtitle>{{ detail.usageCount }} 个工作负载</v-list-item-subtitle>
          </v-list-item>
        </v-list>

        <v-alert
          v-if="!detail.canDelete && detail.blockedReason"
          type="info"
          variant="tonal"
          density="compact"
          class="mt-3"
        >
          无法移除：{{ detail.blockedReason }}
        </v-alert>

        <div v-if="detail.description" class="mt-4">
          <div class="text-subtitle-2 mb-1">说明</div>
          <p class="text-body-2">{{ detail.description }}</p>
        </div>

        <div class="text-subtitle-2 mt-4 mb-2">操作记录</div>
        <v-timeline v-if="events.length" density="compact" side="end" truncate-line="both">
          <v-timeline-item
            v-for="event in events"
            :key="event.id"
            :dot-color="event.eventType === 'failed' ? 'error' : 'primary'"
            size="x-small"
          >
            <div class="text-body-2">{{ EVENT_LABELS[event.eventType] ?? event.eventType }}</div>
            <div class="text-caption text-medium-emphasis">
              {{ formatTime(event.createdAt) }}
              <template v-if="event.actorUsername"> · {{ event.actorUsername }}</template>
            </div>
            <div v-if="event.message" class="text-caption">{{ event.message }}</div>
          </v-timeline-item>
        </v-timeline>
        <div v-else class="text-body-2 text-medium-emphasis">暂无记录。</div>
      </div>
    </v-navigation-drawer>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-card,
.wl-image-card {
  box-shadow: $card-shadow;
}

.wl-image-card {
  cursor: pointer;
  transition:
    transform 0.15s ease,
    box-shadow 0.15s ease;

  &:hover {
    transform: translateY(-2px);
    box-shadow: 0 8px 22px 0 #e8eafc, 0 6px 8px -4px #b2b2b233;
  }
}

.wl-stat {
  padding: 14px 16px;
  border: 1px solid rgba(0, 0, 0, 0.06);
}

.wl-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace;
}

.wl-clamp {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.wl-detail-list :deep(.v-list-item) {
  padding-inline: 0;
}

.min-w-0 {
  min-width: 0;
}
</style>
