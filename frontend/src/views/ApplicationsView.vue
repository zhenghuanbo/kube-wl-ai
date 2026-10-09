<script setup lang="ts">
/**
 * Application catalogue.
 *
 * Presented as a gallery rather than a table: an application is defined by its
 * introduction — what it is for, what is inside it — and a row of columns
 * cannot carry that. The list is already scoped and filtered by visibility on
 * the server, so this page never has to decide what the caller may see; it only
 * decides what is worth showing them.
 *
 * One page serves users and administrators. A user sees the catalogue they can
 * launch from and the entries they wrote; an administrator additionally gets a
 * workspace picker when publishing and may edit anyone's entry in their scope.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import {
  createApplication,
  deleteApplication,
  getApplication,
  launchApplication,
  listApplications,
  updateApplication,
} from '@/api/applications'
import { getResourceGroup } from '@/api/resources'
import { getMyWorkspace, listWorkspaceGrants, listWorkspaces } from '@/api/workspaces'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import ImagePicker from '@/components/image/ImagePicker.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FilterBar from '@/components/common/FilterBar.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusChip from '@/components/common/StatusChip.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notifications'
import type {
  ApplicationCategory,
  ApplicationDetail,
  ApplicationSummary,
  ApplicationVisibility,
} from '@/types/application'
import { CATEGORY_OPTIONS, VISIBILITY_OPTIONS } from '@/types/application'
import { IDE_OPTIONS, type IdeType } from '@/types/instance'
import {
  formatCpu,
  formatDuration,
  formatMemory,
  type ResourceGroupDetail,
  type ResourceSpec,
  type WorkspaceSummary,
} from '@/types/resource'

const { can, isAdmin } = usePermissions()
const auth = useAuthStore()
const notifications = useNotificationStore()

const canCreate = computed(() => can('application:create'))
const canUpdate = computed(() => can('application:update'))
const canDelete = computed(() => can('application:delete'))

const categoryTitle = (value: string): string =>
  CATEGORY_OPTIONS.find((option) => option.value === value)?.title ?? value
const visibilityMeta = (value: string) =>
  VISIBILITY_OPTIONS.find((option) => option.value === value) ?? VISIBILITY_OPTIONS[1]
const ideMeta = (value: string) => IDE_OPTIONS.find((option) => option.value === value)

// --- listing -----------------------------------------------------------------
const rows = ref<ApplicationSummary[]>([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = ref(24)

const filters = reactive({
  search: '',
  category: null as ApplicationCategory | null,
  visibility: null as ApplicationVisibility | null,
  ownedOnly: false,
  includeInactive: false,
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await listApplications({
      page: page.value,
      pageSize: pageSize.value,
      search: filters.search.trim() || undefined,
      category: filters.category ?? undefined,
      visibility: filters.visibility ?? undefined,
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
    filters.visibility,
    filters.ownedOnly,
    filters.includeInactive,
    page.value,
    pageSize.value,
  ],
  () => void load(),
)

// --- shared lookups ----------------------------------------------------------
const myWorkspace = ref<WorkspaceSummary | null>(null)
const workspaces = ref<WorkspaceSummary[]>([])

async function loadWorkspaces(): Promise<void> {
  try {
    if (isAdmin.value) {
      const result = await listWorkspaces({ pageSize: 200 })
      workspaces.value = result.items
    } else {
      myWorkspace.value = await getMyWorkspace()
    }
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

onMounted(async () => {
  await loadWorkspaces()
  await load()
})

// --- editor (create and edit share one dialog) -------------------------------
interface EditorForm {
  name: string
  displayName: string
  summary: string
  readme: string
  category: ApplicationCategory
  icon: string
  workspaceId: number | null
  image: string
  ideType: IdeType
  resourceGroupId: number | null
  resourceSpecId: number | null
  defaultCpuMilli: number
  defaultMemoryMb: number
  defaultGpuCount: number
  defaultGpuType: string
  defaultStorageGb: number
  visibility: ApplicationVisibility
}

const editorOpen = ref(false)
const editorSaving = ref(false)
const editingId = ref<number | null>(null)
const editorError = ref('')
const form = reactive<EditorForm>(emptyForm())

function emptyForm(): EditorForm {
  return {
    name: '',
    displayName: '',
    summary: '',
    readme: '',
    category: 'data_science',
    icon: 'mdi-application',
    workspaceId: null,
    image: '',
    ideType: 'jupyter',
    resourceGroupId: null,
    resourceSpecId: null,
    defaultCpuMilli: 2000,
    defaultMemoryMb: 4096,
    defaultGpuCount: 0,
    defaultGpuType: 'none',
    defaultStorageGb: 10,
    visibility: 'workspace',
  }
}

/** Icon choices offered by the editor; `value` is an `mdi-*` name. */
const ICON_OPTIONS: ReadonlyArray<{ value: string; title: string }> = [
  { value: 'mdi-application', title: '通用' },
  { value: 'mdi-brain', title: '深度学习' },
  { value: 'mdi-notebook-outline', title: '笔记本' },
  { value: 'mdi-chart-scatter-plot', title: '数据科学' },
  { value: 'mdi-database-outline', title: '数据库' },
  { value: 'mdi-web', title: 'Web 服务' },
  { value: 'mdi-code-braces', title: '编程语言' },
  { value: 'mdi-image-filter-hdr', title: '图像处理' },
  { value: 'mdi-robot-outline', title: '机器人' },
  { value: 'mdi-flask-outline', title: '科学计算' },
]

const grants = ref<{ resourceGroupId: number; displayName: string; isActive: boolean }[]>([])
const editorPool = ref<ResourceGroupDetail | null>(null)

const editorSpecs = computed<ResourceSpec[]>(() =>
  (editorPool.value?.specs ?? []).filter((spec) => spec.isActive),
)

const editorCanSubmit = computed(
  () =>
    /^[a-z0-9][a-z0-9-]*$/.test(form.name) &&
    form.name.length <= 64 &&
    form.image.trim().length > 0 &&
    (isAdmin.value ? form.workspaceId !== null : true),
)

function openCreate(): void {
  editingId.value = null
  editorError.value = ''
  Object.assign(form, emptyForm())
  form.workspaceId = isAdmin.value ? null : (myWorkspace.value?.id ?? null)
  grants.value = []
  editorPool.value = null
  editorOpen.value = true
  void loadEditorGrants(form.workspaceId)
}

async function openEdit(summary: ApplicationSummary): Promise<void> {
  editingId.value = summary.id
  editorError.value = ''
  editorOpen.value = true
  grants.value = []
  editorPool.value = null
  try {
    const detail = await getApplication(summary.id)
    Object.assign(form, {
      name: detail.name,
      displayName: detail.displayName,
      summary: detail.summary,
      readme: detail.readme ?? '',
      category: detail.category,
      icon: detail.icon,
      workspaceId: detail.workspaceId,
      image: detail.image,
      ideType: detail.ideType,
      resourceGroupId: detail.resourceGroupId ?? null,
      resourceSpecId: detail.resourceSpecId ?? null,
      defaultCpuMilli: detail.defaultCpuMilli,
      defaultMemoryMb: detail.defaultMemoryMb,
      defaultGpuCount: detail.defaultGpuCount,
      defaultGpuType: detail.defaultGpuType,
      defaultStorageGb: detail.defaultStorageGb,
      visibility: detail.visibility,
    })
    await loadEditorGrants(detail.workspaceId)
    if (detail.resourceGroupId) await selectEditorPool(detail.resourceGroupId, false)
    form.resourceSpecId = detail.resourceSpecId ?? null
  } catch (error) {
    if (error instanceof ApiError) {
      notifications.error(error.message)
      editorOpen.value = false
    } else throw error
  }
}

async function loadEditorGrants(workspaceId: number | null): Promise<void> {
  grants.value = []
  editorPool.value = null
  form.resourceGroupId = null
  form.resourceSpecId = null
  if (workspaceId === null) return
  try {
    grants.value = (await listWorkspaceGrants(workspaceId)).filter((grant) => grant.isActive)
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

async function selectEditorPool(poolId: number | null, resetSpec = true): Promise<void> {
  form.resourceGroupId = poolId
  editorPool.value = null
  if (resetSpec) form.resourceSpecId = null
  if (poolId === null) return
  try {
    editorPool.value = await getResourceGroup(poolId)
    if (resetSpec && editorSpecs.value.length === 1) {
      form.resourceSpecId = editorSpecs.value[0].id
    }
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

watch(
  () => form.workspaceId,
  (workspaceId, previous) => {
    // Only react to a user-driven change; `openEdit` loads grants itself.
    if (!editorOpen.value || editorSaving.value) return
    if (previous === undefined) return
    void loadEditorGrants(workspaceId)
  },
)

/**
 * A pool and a spec must be chosen together.
 *
 * The backend rejects a half-specified pair, so the form is normalised here
 * rather than letting the user discover the rule from a 400.
 */
function resourcePair(): { resourceGroupId: number | null; resourceSpecId: number | null } {
  if (form.resourceGroupId === null || form.resourceSpecId === null) {
    return { resourceGroupId: null, resourceSpecId: null }
  }
  return { resourceGroupId: form.resourceGroupId, resourceSpecId: form.resourceSpecId }
}

async function submitEditor(): Promise<void> {
  if (!editorCanSubmit.value) return
  editorSaving.value = true
  editorError.value = ''
  try {
    const pair = resourcePair()
    const payload = {
      displayName: form.displayName.trim() || form.name.trim(),
      summary: form.summary.trim(),
      readme: form.readme.trim() || null,
      category: form.category,
      icon: form.icon,
      image: form.image.trim(),
      ideType: form.ideType,
      ...pair,
      defaultCpuMilli: form.defaultCpuMilli,
      defaultMemoryMb: form.defaultMemoryMb,
      defaultGpuCount: form.defaultGpuCount,
      defaultGpuType: form.defaultGpuCount > 0 ? form.defaultGpuType : 'none',
      defaultStorageGb: form.defaultStorageGb,
      visibility: form.visibility,
    }

    if (editingId.value === null) {
      await createApplication({ name: form.name.trim(), workspaceId: form.workspaceId, ...payload })
      notifications.success('应用程序已创建')
    } else {
      await updateApplication(editingId.value, payload)
      notifications.success('应用程序已更新')
    }
    editorOpen.value = false
    await load()
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    // Kept in the dialog rather than only in a toast: the message usually names
    // a field, and the form is right there to fix.
    editorError.value = error.message
  } finally {
    editorSaving.value = false
  }
}

// --- detail ------------------------------------------------------------------
const detailOpen = ref(false)
const detailLoading = ref(false)
const detail = ref<ApplicationDetail | null>(null)

async function openDetail(id: number): Promise<void> {
  detailOpen.value = true
  detailLoading.value = true
  detail.value = null
  try {
    detail.value = await getApplication(id)
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    detailLoading.value = false
  }
}

/** The introduction is authored in markdown; shown preformatted, not rendered. */
const readmeLines = computed(() =>
  (detail.value?.readme ?? '').split('\n').filter((line) => line.trim().length > 0),
)

// --- launch ------------------------------------------------------------------
const launchOpen = ref(false)
const launching = ref(false)
const launchError = ref('')
const launchTarget = ref<ApplicationDetail | null>(null)
const launchForm = reactive({
  name: '',
  workspaceId: null as number | null,
  resourceGroupId: null as number | null,
  resourceSpecId: null as number | null,
  storageGb: 10,
  maxRuntimeSeconds: null as number | null,
  sshEnabled: true,
})
const launchGrants = ref<{ resourceGroupId: number; displayName: string; isActive: boolean }[]>([])
const launchPool = ref<ResourceGroupDetail | null>(null)

const launchSpecs = computed<ResourceSpec[]>(() =>
  (launchPool.value?.specs ?? []).filter((spec) => spec.isActive),
)

/** True when the application pins a pool and spec, so no choice is required. */
const launchUsesDefaults = computed(
  () => launchTarget.value?.resourceGroupId != null && launchTarget.value?.resourceSpecId != null,
)

const launchRuntimeOptions = computed(() => {
  const cap = launchPool.value?.maxRuntimeSeconds ?? null
  const steps = [1, 2, 4, 8, 12, 24, 48, 72].map((hours) => hours * 3600)
  const allowed = cap === null ? steps : steps.filter((seconds) => seconds <= cap)
  return [
    ...allowed.map((seconds) => ({ value: seconds, title: formatDuration(seconds) ?? '' })),
    {
      value: null,
      title: cap === null ? '不限制' : `资源组上限（${formatDuration(cap) ?? ''}）`,
    },
  ]
})

const launchCanSubmit = computed(() => {
  const nameOk = /^[a-z0-9][a-z0-9-]*$/.test(launchForm.name) && launchForm.name.length <= 55
  if (!nameOk) return false
  if (isAdmin.value && launchForm.workspaceId === null) return false
  if (!launchUsesDefaults.value && (!launchForm.resourceGroupId || !launchForm.resourceSpecId)) {
    return false
  }
  return true
})

/**
 * Open the launch dialog.
 *
 * Fetches the full detail even when the click came from a card: the card's
 * summary does not carry the spec id, and guessing whether the application pins
 * a resource would mean offering a choice the launch does not need — or worse,
 * omitting one it does.
 */
async function openLaunch(summary: ApplicationSummary): Promise<void> {
  launchError.value = ''
  launchOpen.value = true
  launchTarget.value = null
  launchGrants.value = []
  launchPool.value = null
  try {
    const detail = await getApplication(summary.id)
    launchTarget.value = detail
    Object.assign(launchForm, {
      name: nextInstanceName(detail.name),
      workspaceId: isAdmin.value ? null : (myWorkspace.value?.id ?? null),
      resourceGroupId: detail.resourceGroupId ?? null,
      resourceSpecId: detail.resourceSpecId ?? null,
      storageGb: detail.defaultStorageGb || 10,
      maxRuntimeSeconds: null,
      sshEnabled: true,
    })
    await loadLaunchGrants(launchForm.workspaceId)
    if (detail.resourceGroupId) await selectLaunchPool(detail.resourceGroupId, false)
    launchForm.resourceSpecId = detail.resourceSpecId ?? null
  } catch (error) {
    if (error instanceof ApiError) {
      notifications.error(error.message)
      launchOpen.value = false
    } else throw error
  }
}

/**
 * A predictable default name.
 *
 * Deliberately not random: two users launching the same application should both
 * get something they can recognise in the instance list, and a clash produces a
 * clear message rather than a name nobody chose.
 */
function nextInstanceName(base: string): string {
  return base.replace(/[^a-z0-9-]/g, '-').slice(0, 52) || 'app'
}

async function loadLaunchGrants(workspaceId: number | null): Promise<void> {
  launchGrants.value = []
  if (workspaceId === null) return
  try {
    launchGrants.value = (await listWorkspaceGrants(workspaceId)).filter((grant) => grant.isActive)
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

async function selectLaunchPool(poolId: number | null, resetSpec = true): Promise<void> {
  launchForm.resourceGroupId = poolId
  launchPool.value = null
  if (resetSpec) launchForm.resourceSpecId = null
  if (poolId === null) return
  try {
    launchPool.value = await getResourceGroup(poolId)
    if (resetSpec && launchSpecs.value.length === 1) {
      launchForm.resourceSpecId = launchSpecs.value[0].id
    }
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

watch(
  () => launchForm.workspaceId,
  (workspaceId, previous) => {
    if (!launchOpen.value || previous === undefined) return
    void loadLaunchGrants(workspaceId)
  },
)

async function submitLaunch(): Promise<void> {
  if (!launchTarget.value || !launchCanSubmit.value) return
  launching.value = true
  launchError.value = ''
  try {
    const instance = await launchApplication(launchTarget.value.id, {
      name: launchForm.name.trim(),
      workspaceId: launchForm.workspaceId,
      resourceGroupId: launchForm.resourceGroupId,
      resourceSpecId: launchForm.resourceSpecId,
      storageGb: launchForm.storageGb,
      maxRuntimeSeconds: launchForm.maxRuntimeSeconds,
      sshEnabled: launchForm.sshEnabled,
    })
    notifications.success(`实例 ${instance.name} 已开始创建`)
    launchOpen.value = false
    await load()
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    launchError.value = error.message
  } finally {
    launching.value = false
  }
}

// --- retire ------------------------------------------------------------------
const retireOpen = ref(false)
const retiring = ref(false)
const retireTarget = ref<ApplicationSummary | null>(null)

function askRetire(summary: ApplicationSummary): void {
  retireTarget.value = summary
  retireOpen.value = true
}

async function confirmRetire(): Promise<void> {
  if (!retireTarget.value) return
  retiring.value = true
  try {
    await deleteApplication(retireTarget.value.id)
    notifications.success('应用程序已下架')
    retireOpen.value = false
    await load()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    retiring.value = false
  }
}

async function restore(summary: ApplicationSummary): Promise<void> {
  try {
    await updateApplication(summary.id, { isActive: true })
    notifications.success('应用程序已重新上架')
    await load()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

/**
 * An administrator, or the author, may edit.
 *
 * The backend applies the same rule and is the actual boundary; this only
 * decides whether to offer the button, so a stale answer costs nothing worse
 * than a 404 the user was going to get anyway.
 */
function mayEdit(summary: ApplicationSummary): boolean {
  return canUpdate.value && (isAdmin.value || summary.ownerUsername === auth.user?.username)
}
</script>

<template>
  <div>
    <PageHeader title="应用程序" subtitle="可复用的实例形态：选一个，一键启动成你自己的开发环境">
      <template #actions>
        <v-btn
          v-if="canCreate"
          color="primary"
          variant="flat"
          prepend-icon="mdi-plus"
          @click="openCreate"
        >
          新建应用
        </v-btn>
      </template>
    </PageHeader>

    <FilterBar>
      <v-text-field
        v-model="filters.search"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        prepend-inner-icon="mdi-magnify"
        placeholder="搜索名称或简介"
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
        style="max-width: 180px"
      />
      <v-select
        v-model="filters.visibility"
        :items="VISIBILITY_OPTIONS"
        item-title="title"
        item-value="value"
        density="comfortable"
        variant="outlined"
        hide-details
        clearable
        label="可见范围"
        style="max-width: 180px"
      />
      <v-switch
        v-model="filters.ownedOnly"
        label="只看我创建的"
        color="primary"
        density="compact"
        hide-details
        inset
      />
      <v-switch
        v-model="filters.includeInactive"
        label="含已下架"
        color="primary"
        density="compact"
        hide-details
        inset
      />
    </FilterBar>

    <div v-if="loading" class="wl-gallery-loading">
      <v-skeleton-loader type="card" />
      <v-skeleton-loader type="card" />
      <v-skeleton-loader type="card" />
    </div>

    <EmptyState
      v-else-if="rows.length === 0"
      icon="mdi-application-outline"
      title="还没有可用的应用程序"
      :description="
        filters.search || filters.category || filters.visibility || filters.ownedOnly
          ? '换个筛选条件试试。'
          : '应用程序把一个配置好的环境分享给同学。创建第一个，或者等管理员发布。'
      "
    />

    <v-row v-else dense>
      <v-col v-for="app in rows" :key="app.id" cols="12" sm="6" lg="4">
        <v-card flat class="wl-app-card" :class="{ 'wl-app-card--retired': !app.isActive }">
          <div class="wl-app-card__head">
            <v-avatar size="44" rounded="lg" color="primary" variant="tonal">
              <v-icon :icon="app.icon" size="24" />
            </v-avatar>
            <div class="wl-app-card__title">
              <div class="wl-app-card__name">{{ app.displayName }}</div>
              <div class="wl-app-card__meta">
                {{ categoryTitle(app.category) }}
                <span v-if="app.workspaceName"> · {{ app.workspaceName }}</span>
              </div>
            </div>
            <v-menu v-if="mayEdit(app) || canDelete" location="bottom end">
              <template #activator="{ props }">
                <v-btn v-bind="props" icon="mdi-dots-vertical" variant="text" size="small" />
              </template>
              <v-list density="compact">
                <v-list-item
                  v-if="mayEdit(app)"
                  prepend-icon="mdi-pencil-outline"
                  title="编辑"
                  @click="openEdit(app)"
                />
                <v-list-item
                  prepend-icon="mdi-information-outline"
                  title="查看详情"
                  @click="openDetail(app.id)"
                />
                <v-list-item
                  v-if="canDelete && app.isActive"
                  prepend-icon="mdi-archive-arrow-down-outline"
                  title="下架"
                  @click="askRetire(app)"
                />
                <v-list-item
                  v-if="mayEdit(app) && !app.isActive"
                  prepend-icon="mdi-archive-arrow-up-outline"
                  title="重新上架"
                  @click="restore(app)"
                />
              </v-list>
            </v-menu>
          </div>

          <p class="wl-app-card__summary">{{ app.summary || '（暂无简介）' }}</p>

          <div class="wl-app-card__chips">
            <StatusChip v-if="!app.isActive" status="inactive" label="已下架" />
            <v-chip size="small" variant="tonal" label>
              <v-icon start size="x-small" :icon="ideMeta(app.ideType)?.icon ?? 'mdi-console'" />
              {{ ideMeta(app.ideType)?.title ?? app.ideType }}
            </v-chip>
            <v-chip size="small" variant="tonal" label>
              {{ formatCpu(app.defaultCpuMilli) }} / {{ formatMemory(app.defaultMemoryMb) }}
            </v-chip>
            <v-chip v-if="app.defaultGpuCount > 0" size="small" variant="tonal" color="secondary" label>
              {{ app.defaultGpuCount }}× {{ app.defaultGpuType.toUpperCase() }}
            </v-chip>
            <v-chip size="small" variant="tonal" label>
              <v-icon start size="x-small" :icon="visibilityMeta(app.visibility).icon" />
              {{ visibilityMeta(app.visibility).title }}
            </v-chip>
            <v-chip v-if="app.isCurated" size="small" variant="tonal" color="info" label>
              官方
            </v-chip>
          </div>

          <div class="wl-app-card__foot">
            <span class="wl-app-card__count">
              <v-icon icon="mdi-rocket-launch-outline" size="x-small" />
              已启动 {{ app.launchCount }} 次
            </span>
            <v-btn
              color="primary"
              variant="flat"
              size="small"
              prepend-icon="mdi-play"
              :disabled="!app.isActive || !can('instance:create')"
              @click="openLaunch(app)"
            >
              启动
            </v-btn>
          </div>
        </v-card>
      </v-col>
    </v-row>

    <div v-if="total > pageSize" class="wl-pagination">
      <v-pagination
        v-model="page"
        :length="Math.ceil(total / pageSize)"
        :total-visible="7"
        density="comfortable"
      />
    </div>

    <!-- Editor: create and edit share one form, because they differ by two
         fields (the name is immutable, so the workspace is too). -->
    <v-dialog v-model="editorOpen" max-width="820" scrollable>
      <v-card>
        <v-card-title>{{ editingId === null ? '新建应用程序' : '编辑应用程序' }}</v-card-title>
        <v-card-text>
          <v-alert v-if="editorError" type="error" variant="tonal" class="mb-4 wl-form-error">
            {{ editorError }}
          </v-alert>

          <v-row dense>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.name"
                label="标识名"
                :disabled="editingId !== null"
                :rules="[(v: string) => /^[a-z0-9][a-z0-9-]*$/.test(v) || '只能用小写字母、数字和连字符']"
                hint="创建后不可修改，会出现在实例名里"
                persistent-hint
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="form.displayName"
                label="显示名"
                placeholder="例如 PyTorch 2.4 训练环境"
                variant="outlined"
                density="comfortable"
              />
            </v-col>

            <v-col cols="12" sm="6">
              <v-select
                v-model="form.category"
                :items="CATEGORY_OPTIONS"
                item-title="title"
                item-value="value"
                label="分类"
                variant="outlined"
                density="comfortable"
              >
                <template #item="{ item, props }">
                  <v-list-item v-bind="props" :prepend-icon="item.icon" />
                </template>
              </v-select>
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.icon"
                :items="ICON_OPTIONS"
                item-title="title"
                item-value="value"
                label="图标"
                variant="outlined"
                density="comfortable"
              >
                <template #item="{ item, props }">
                  <v-list-item v-bind="props" :prepend-icon="item.value" />
                </template>
                <template #selection="{ item }">
                  <v-icon :icon="item.value" class="mr-2" />
                  {{ item.title }}
                </template>
              </v-select>
            </v-col>

            <v-col v-if="isAdmin" cols="12" sm="6">
              <v-select
                v-model="form.workspaceId"
                :items="workspaces"
                item-title="k8sNamespace"
                item-value="id"
                label="所属工作空间"
                :disabled="editingId !== null"
                hint="决定应用的归属组与数据范围，创建后不可修改"
                persistent-hint
                variant="outlined"
                density="comfortable"
              />
            </v-col>

            <v-col cols="12">
              <v-text-field
                v-model="form.summary"
                label="一句话简介"
                counter="255"
                maxlength="255"
                variant="outlined"
                density="comfortable"
              />
            </v-col>

            <v-col cols="12">
              <v-textarea
                v-model="form.readme"
                label="详细介绍（支持 Markdown 书写）"
                rows="5"
                auto-grow
                variant="outlined"
                density="comfortable"
                hint="写清楚这个环境里有什么、怎么开始、数据放在哪里"
                persistent-hint
              />
            </v-col>

            <v-col cols="12">
              <v-divider class="my-2" />
              <div class="text-subtitle-2 mb-2">启动默认值</div>
            </v-col>

            <v-col cols="12" sm="8">
              <ImagePicker v-model="form.image" label="容器镜像 *" />
            </v-col>
            <v-col cols="12" sm="4">
              <v-select
                v-model="form.ideType"
                :items="IDE_OPTIONS"
                item-title="title"
                item-value="value"
                label="Web IDE"
                variant="outlined"
                density="comfortable"
              >
                <template #item="{ item, props }">
                  <v-list-item v-bind="props" :prepend-icon="item.icon" />
                </template>
              </v-select>
            </v-col>

            <v-col cols="12" sm="6">
              <v-select
                v-model="form.resourceGroupId"
                :items="grants"
                item-title="displayName"
                item-value="resourceGroupId"
                label="资源组（可选）"
                clearable
                variant="outlined"
                density="comfortable"
                @update:model-value="selectEditorPool($event as number | null)"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="form.resourceSpecId"
                :items="editorSpecs"
                item-title="displayName"
                item-value="id"
                label="资源规格（可选）"
                clearable
                :disabled="form.resourceGroupId === null"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col v-if="form.resourceGroupId === null" cols="12">
              <v-alert type="info" variant="tonal" density="compact">
                不预设资源组和规格时，启动的人需要自己选择 —— 适合跨实验室共享的应用。
                资源组与规格必须同时选择或同时留空。
              </v-alert>
            </v-col>

            <v-col cols="12" sm="3">
              <v-text-field
                v-model.number="form.defaultCpuMilli"
                type="number"
                label="CPU（毫核）"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="3">
              <v-text-field
                v-model.number="form.defaultMemoryMb"
                type="number"
                label="内存（MB）"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="3">
              <v-text-field
                v-model.number="form.defaultGpuCount"
                type="number"
                min="0"
                label="加速卡数量"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="3">
              <v-text-field
                v-model.number="form.defaultStorageGb"
                type="number"
                min="0"
                label="存储（GB）"
                variant="outlined"
                density="comfortable"
              />
            </v-col>

            <v-col cols="12">
              <v-select
                v-model="form.visibility"
                :items="VISIBILITY_OPTIONS"
                item-title="title"
                item-value="value"
                label="可见范围"
                variant="outlined"
                density="comfortable"
              >
                <template #item="{ item, props }">
                  <v-list-item v-bind="props" :prepend-icon="item.icon" :subtitle="item.hint" />
                </template>
              </v-select>
              <div class="text-caption text-medium-emphasis mt-1">
                {{ visibilityMeta(form.visibility).hint }}
              </div>
            </v-col>
          </v-row>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="editorOpen = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="editorSaving"
            :disabled="!editorCanSubmit"
            @click="submitEditor"
          >
            {{ editingId === null ? '创建' : '保存' }}
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Launch -->
    <v-dialog v-model="launchOpen" max-width="680">
      <v-card>
        <v-card-title>启动 {{ launchTarget?.displayName }}</v-card-title>
        <v-card-text>
          <v-alert v-if="launchError" type="error" variant="tonal" class="mb-4 wl-form-error">
            {{ launchError }}
          </v-alert>

          <p class="text-body-2 text-medium-emphasis mb-4">
            {{ launchTarget?.summary || '（暂无简介）' }}
          </p>

          <v-row dense>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model="launchForm.name"
                label="实例名"
                :rules="[(v: string) => /^[a-z0-9][a-z0-9-]*$/.test(v) || '只能用小写字母、数字和连字符']"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col v-if="isAdmin" cols="12" sm="6">
              <v-select
                v-model="launchForm.workspaceId"
                :items="workspaces"
                item-title="k8sNamespace"
                item-value="id"
                label="目标工作空间"
                variant="outlined"
                density="comfortable"
              />
            </v-col>

            <template v-if="!launchUsesDefaults">
              <v-col cols="12">
                <v-alert type="warning" variant="tonal" density="compact" class="mb-1">
                  这个应用没有预设资源，请选择资源组与规格。
                </v-alert>
              </v-col>
              <v-col cols="12" sm="6">
                <v-select
                  v-model="launchForm.resourceGroupId"
                  :items="launchGrants"
                  item-title="displayName"
                  item-value="resourceGroupId"
                  label="资源组"
                  variant="outlined"
                  density="comfortable"
                  @update:model-value="selectLaunchPool($event as number | null)"
                />
              </v-col>
              <v-col cols="12" sm="6">
                <v-select
                  v-model="launchForm.resourceSpecId"
                  :items="launchSpecs"
                  item-title="displayName"
                  item-value="id"
                  label="资源规格"
                  :disabled="launchForm.resourceGroupId === null"
                  variant="outlined"
                  density="comfortable"
                />
              </v-col>
            </template>
            <v-col v-else cols="12">
              <div class="text-body-2 text-medium-emphasis">
                资源：
                {{ launchTarget?.resourceGroupName || '预设资源组' }}
                <span v-if="launchTarget">
                  · {{ formatCpu(launchTarget.defaultCpuMilli) }} /
                  {{ formatMemory(launchTarget.defaultMemoryMb) }}
                  <span v-if="launchTarget.defaultGpuCount > 0">
                    · {{ launchTarget.defaultGpuCount }}×
                    {{ launchTarget.defaultGpuType.toUpperCase() }}
                  </span>
                </span>
              </div>
            </v-col>

            <v-col cols="12" sm="6">
              <v-text-field
                v-model.number="launchForm.storageGb"
                type="number"
                min="0"
                label="存储（GB）"
                :max="launchPool?.limits?.maxStorageGbPerUser ?? undefined"
                variant="outlined"
                density="comfortable"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="launchForm.maxRuntimeSeconds"
                :items="launchRuntimeOptions"
                item-title="title"
                item-value="value"
                label="最长运行时间"
                variant="outlined"
                density="comfortable"
              />
            </v-col>

            <v-col cols="12">
              <v-switch
                v-model="launchForm.sshEnabled"
                label="开启 SSH 访问"
                color="primary"
                density="compact"
                hide-details
                inset
                hint="需要在「个人设置」里先登记公钥，否则登录会被拒绝"
                persistent-hint
              />
            </v-col>
          </v-row>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="launchOpen = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="launching"
            :disabled="!launchCanSubmit"
            @click="submitLaunch"
          >
            启动
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- Detail -->
    <v-dialog v-model="detailOpen" max-width="760" scrollable>
      <v-card>
        <v-card-title class="d-flex align-center">
          <v-icon :icon="detail?.icon ?? 'mdi-application'" class="mr-3" />
          {{ detail?.displayName ?? '加载中…' }}
        </v-card-title>
        <v-card-text>
          <v-skeleton-loader v-if="detailLoading" type="article" />
          <template v-else-if="detail">
            <div class="d-flex flex-wrap ga-2 mb-4">
              <v-chip size="small" variant="tonal" label>
                {{ categoryTitle(detail.category) }}
              </v-chip>
              <v-chip size="small" variant="tonal" label>
                <v-icon start size="x-small" :icon="visibilityMeta(detail.visibility).icon" />
                {{ visibilityMeta(detail.visibility).title }}
              </v-chip>
              <v-chip size="small" variant="tonal" label>{{ detail.image }}</v-chip>
              <v-chip size="small" variant="tonal" label>
                {{ formatCpu(detail.defaultCpuMilli) }} / {{ formatMemory(detail.defaultMemoryMb) }}
              </v-chip>
              <v-chip v-if="detail.defaultGpuCount > 0" size="small" variant="tonal" color="secondary" label>
                {{ detail.defaultGpuCount }}× {{ detail.defaultGpuType.toUpperCase() }}
              </v-chip>
              <v-chip size="small" variant="tonal" label>已启动 {{ detail.launchCount }} 次</v-chip>
            </div>

            <div class="text-caption text-medium-emphasis mb-3">
              维护者：{{ detail.ownerDisplayName || detail.ownerUsername || '—' }}
              <span v-if="detail.workspaceName"> · {{ detail.workspaceName }}</span>
            </div>

            <v-divider class="mb-3" />

            <!-- Shown preformatted rather than rendered: the introduction is
                 authored by users, and rendering HTML from user text is a
                 cross-site scripting hole for no real gain. -->
            <div v-if="readmeLines.length" class="wl-readme">
              <p v-for="(line, index) in readmeLines" :key="index">{{ line }}</p>
            </div>
            <p v-else class="text-medium-emphasis">这个应用还没有详细介绍。</p>
          </template>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="detailOpen = false">关闭</v-btn>
          <v-btn
            v-if="detail"
            color="primary"
            variant="flat"
            prepend-icon="mdi-play"
            :disabled="!detail.isActive"
            @click="detailOpen = false; openLaunch(detail)"
          >
            启动
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="retireOpen"
      title="下架应用程序"
      :text="`下架后「${retireTarget?.displayName ?? ''}」不再出现在目录里，已经用它创建的实例不受影响。记录会保留，可以重新上架。`"
      confirm-text="下架"
      confirm-color="warning"
      :loading="retiring"
      @confirm="confirmRetire"
    />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-gallery-loading {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 20px;
}

.wl-app-card {
  display: flex;
  flex-direction: column;
  height: 100%;
  padding: 20px;
  border-radius: 12px;
  box-shadow: $card-shadow;
  transition: transform 0.15s ease, box-shadow 0.15s ease;

  &:hover {
    transform: translateY(-2px);
    box-shadow: 0 6px 20px 0 rgba(83, 109, 254, 0.16);
  }
}

.wl-app-card--retired {
  opacity: 0.72;
}

.wl-app-card__head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}

.wl-app-card__title {
  flex: 1 1 auto;
  min-width: 0;
}

.wl-app-card__name {
  font-size: 16px;
  font-weight: 600;
  color: $title-grey;
  line-height: 1.3;
}

.wl-app-card__meta {
  font-size: 12px;
  color: $text-grey;
  margin-top: 2px;
}

.wl-app-card__summary {
  margin: 14px 0 0;
  font-size: 13px;
  line-height: 1.6;
  color: $text-color;
  flex: 1 1 auto;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.wl-app-card__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 14px;
}

.wl-app-card__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid rgba(0, 0, 0, 0.06);
}

.wl-app-card__count {
  font-size: 12px;
  color: $text-grey;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.wl-pagination {
  display: flex;
  justify-content: center;
  margin-top: 28px;
}

.wl-readme {
  font-size: 14px;
  line-height: 1.75;
  color: $text-color;

  p {
    margin: 0 0 8px;
    white-space: pre-wrap;
    word-break: break-word;
  }
}
</style>
