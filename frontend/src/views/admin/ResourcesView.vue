<script setup lang="ts">
/**
 * Compute resources: pools, specs, duration options and the node inventory.
 *
 * The four tabs are the four things an administrator curates. Pools are the
 * unit users are granted; specs are the shapes offered inside a pool; duration
 * options cap how long anything may run; nodes are read-only infrastructure
 * pulled from the cluster.
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'

import { ApiError } from '@/api/client'
import { listGroups, type GroupSummary } from '@/api/groups'
import type { FieldLabels } from '@/api/validation'
import {
  createResourceGroup,
  createResourceSpec,
  createRuntimeLimit,
  deleteResourceGroup,
  deleteResourceSpec,
  deleteRuntimeLimit,
  getResourceGroup,
  listNodeGpus,
  listNodes,
  listResourceGroups,
  listResourceSpecs,
  listRuntimeLimits,
  setResourceGroupNodes,
  setResourceGroupSpecs,
  syncNodes,
  updateResourceGroup,
  updateResourceGroupLimits,
  updateResourceSpec,
  updateRuntimeLimit,
} from '@/api/resources'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import StatusChip from '@/components/common/StatusChip.vue'
import { usePermissions } from '@/composables/usePermissions'
import { useNotificationStore } from '@/stores/notifications'
import {
  formatCpu,
  formatDuration,
  formatMemory,
  GPU_MODELS,
  type ComputeNode,
  type GpuDevice,
  type GpuModel,
  type NetworkType,
  type ResourceGroupCreatePayload,
  type ResourceGroupSummary,
  type ResourceSpec,
  type ResourceSpecUpdatePayload,
  type RuntimeLimit,
  type RuntimeLimitUpdatePayload,
} from '@/types/resource'

const { can, isGlobalAdmin, adminGroupIds } = usePermissions()

/**
 * Field labels, so a rejection reads 「标识：格式不正确」 instead of
 * 「name: String should match pattern ...」.
 *
 * The API can only name a field by its wire name; the form is the only layer that
 * knows it presents `name` as 标识. These exist because the wire names alone were
 * not enough for anyone to act on.
 */
const POOL_FIELD_LABELS: FieldLabels = {
  name: '标识',
  displayName: '显示名称',
  description: '描述',
  kind: '类型',
  groupId: '所属组',
  networkType: '网络',
  maxRuntimeSeconds: '最长运行时长',
  storageClass: '存储类',
  isDefault: '默认资源池',
  k8sLabelSelector: '节点标签选择器',
}

const SPEC_FIELD_LABELS: FieldLabels = {
  name: '标识',
  displayName: '显示名称',
  cpuMilli: 'CPU（milli）',
  memoryMb: '内存（MB）',
  gpuType: '加速卡型号',
  gpuCount: '加速卡数量',
}

const LIMIT_FIELD_LABELS: FieldLabels = {
  name: '标识',
  displayName: '显示名称',
  maxRuntimeSeconds: '最长运行时长',
  isDefault: '默认时长',
}
const notifications = useNotificationStore()

const tab = ref('pools')

/** `none` reads as "no accelerator", so it is labelled rather than uppercased. */
const GPU_MODEL_ITEMS = GPU_MODELS.map((model) => ({
  value: model,
  title: model === 'none' ? '无（仅 CPU）' : model.toUpperCase(),
}))

const NETWORK_LABELS: Record<NetworkType, string> = {
  isolated: '隔离网络',
  internal: '集群内网',
  internet: '可访问外网',
}

const groups = ref<GroupSummary[]>([])
const assignableGroups = computed(() =>
  isGlobalAdmin.value
    ? groups.value
    : groups.value.filter((group) => adminGroupIds.value.includes(group.id)),
)

// --- pools -------------------------------------------------------------------
const pools = ref<ResourceGroupSummary[]>([])
const poolsLoading = ref(false)
const specs = ref<ResourceSpec[]>([])

async function loadPools(): Promise<void> {
  poolsLoading.value = true
  try {
    const result = await listResourceGroups({ pageSize: 200 })
    pools.value = result.items
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
  } finally {
    poolsLoading.value = false
  }
}

// --- pool editor -------------------------------------------------------------
const poolOpen = ref(false)
const poolEditing = ref<ResourceGroupSummary | null>(null)
const poolSaving = ref(false)
const poolError = ref<string | null>(null)
const poolForm = reactive({
  name: '',
  displayName: '',
  description: '',
  kind: 'private' as 'public' | 'private',
  groupId: null as number | null,
  networkType: 'internal' as NetworkType,
  maxRuntimeSeconds: null as number | null,
  storageClass: '',
  isDefault: false,
  specIds: [] as number[],
  limits: {
    maxGpuPerUser: 2,
    maxCpuCoresPerUser: 8,
    maxMemoryGbPerUser: 16,
    maxStorageGbPerUser: 100,
  },
})

async function openPool(pool?: ResourceGroupSummary): Promise<void> {
  poolError.value = null
  poolEditing.value = pool ?? null
  if (pool) {
    const detail = await getResourceGroup(pool.id)
    Object.assign(poolForm, {
      name: detail.name,
      displayName: detail.displayName,
      description: detail.description ?? '',
      kind: detail.kind,
      groupId: detail.groupId,
      networkType: detail.networkType,
      maxRuntimeSeconds: detail.maxRuntimeSeconds,
      storageClass: detail.storageClass ?? '',
      isDefault: detail.isDefault,
      specIds: detail.specs.map((spec) => spec.id),
      limits: {
        maxGpuPerUser: detail.limits?.maxGpuPerUser ?? 2,
        maxCpuCoresPerUser: detail.limits?.maxCpuCoresPerUser ?? 8,
        maxMemoryGbPerUser: detail.limits?.maxMemoryGbPerUser ?? 16,
        maxStorageGbPerUser: detail.limits?.maxStorageGbPerUser ?? 100,
      },
    })
  } else {
    Object.assign(poolForm, {
      name: '',
      displayName: '',
      description: '',
      kind: 'private',
      groupId: assignableGroups.value.length === 1 ? assignableGroups.value[0]!.id : null,
      networkType: 'internal',
      maxRuntimeSeconds: null,
      storageClass: '',
      isDefault: false,
      specIds: [],
      limits: {
        maxGpuPerUser: 2,
        maxCpuCoresPerUser: 8,
        maxMemoryGbPerUser: 16,
        maxStorageGbPerUser: 100,
      },
    })
  }
  poolOpen.value = true
}

async function submitPool(): Promise<void> {
  poolSaving.value = true
  poolError.value = null
  try {
    // The half of the pool body that may change after creation, typed by the
    // *create* payload minus `name`.
    //
    // Every field create permits is also valid on update, so one object serves both
    // callers; and typing it here rather than at the call site is what makes
    // TypeScript reject the literal if `name` — the pool's stable identifier, fixed
    // at creation and refused outright by `ResourceGroupUpdate` — is added back.
    // That omission is the fix for every pool edit returning 422.
    const mutable: Omit<ResourceGroupCreatePayload, 'name'> = {
      displayName: poolForm.displayName || poolForm.name,
      description: poolForm.description || null,
      kind: poolForm.kind,
      // A public pool is shared across labs, so it belongs to no single group.
      groupId: poolForm.kind === 'public' ? null : poolForm.groupId,
      networkType: poolForm.networkType,
      maxRuntimeSeconds: poolForm.maxRuntimeSeconds,
      storageClass: poolForm.storageClass || null,
      isDefault: poolForm.isDefault,
    }

    const saved = poolEditing.value
      ? await updateResourceGroup(poolEditing.value.id, mutable)
      : await createResourceGroup({ name: poolForm.name, ...mutable })

    await updateResourceGroupLimits(saved.id, poolForm.limits)
    await setResourceGroupSpecs(saved.id, poolForm.specIds)

    notifications.success(poolEditing.value ? '资源池已更新' : '资源池已创建')
    poolOpen.value = false
    await loadPools()
  } catch (error) {
    poolError.value =
      error instanceof ApiError ? error.display(POOL_FIELD_LABELS) : '保存失败'
  } finally {
    poolSaving.value = false
  }
}

// --- specs -------------------------------------------------------------------
const specOpen = ref(false)
const specEditing = ref<ResourceSpec | null>(null)
const specSaving = ref(false)
const specError = ref<string | null>(null)
const specForm = reactive({
  name: '',
  displayName: '',
  cpuMilli: 4000,
  memoryMb: 8192,
  gpuType: 'none' as GpuModel,
  gpuCount: 0,
})

async function openSpec(spec?: ResourceSpec): Promise<void> {
  specError.value = null
  specEditing.value = spec ?? null
  Object.assign(
    specForm,
    spec
      ? {
          name: spec.name,
          displayName: spec.displayName,
          cpuMilli: spec.cpuMilli,
          memoryMb: spec.memoryMb,
          gpuType: spec.gpuType as GpuModel,
          gpuCount: spec.gpuCount,
        }
      : {
          name: '',
          displayName: '',
          cpuMilli: 4000,
          memoryMb: 8192,
          gpuType: 'none' as GpuModel,
          gpuCount: 0,
        },
  )
  specOpen.value = true
}

/** The backend rejects a model with a zero count, so keep the form honest. */
watch(
  () => specForm.gpuCount,
  (count) => {
    if (count === 0) specForm.gpuType = 'none'
    else if (specForm.gpuType === 'none') specForm.gpuType = 'a100'
  },
)

async function submitSpec(): Promise<void> {
  specSaving.value = true
  specError.value = null
  try {
    if (specEditing.value) {
      // `name` is fixed at creation and refused by the update endpoint, so the
      // body carries the mutable fields only. Annotating the literal with the
      // update payload type makes TypeScript reject it if `name` creeps back in.
      const mutable: ResourceSpecUpdatePayload = {
        displayName: specForm.displayName,
        cpuMilli: specForm.cpuMilli,
        memoryMb: specForm.memoryMb,
        gpuType: specForm.gpuType,
        gpuCount: specForm.gpuCount,
      }
      await updateResourceSpec(specEditing.value.id, mutable)
      notifications.success('规格已更新')
    } else {
      await createResourceSpec(specForm)
      notifications.success('规格已创建')
    }
    specOpen.value = false
    await loadSpecs()
  } catch (error) {
    specError.value =
      error instanceof ApiError ? error.display(SPEC_FIELD_LABELS) : '保存失败'
  } finally {
    specSaving.value = false
  }
}

async function loadSpecs(): Promise<void> {
  specs.value = await listResourceSpecs()
}

// --- runtime limits ----------------------------------------------------------
const limits = ref<RuntimeLimit[]>([])
const limitOpen = ref(false)
const limitEditing = ref<RuntimeLimit | null>(null)
const limitSaving = ref(false)
const limitError = ref<string | null>(null)
const limitForm = reactive({
  name: '',
  displayName: '',
  maxRuntimeSeconds: null as number | null,
  isDefault: false,
})

function openLimit(limit?: RuntimeLimit): void {
  limitError.value = null
  limitEditing.value = limit ?? null
  Object.assign(
    limitForm,
    limit
      ? {
          name: limit.name,
          displayName: limit.displayName,
          maxRuntimeSeconds: limit.maxRuntimeSeconds,
          isDefault: limit.isDefault,
        }
      : { name: '', displayName: '', maxRuntimeSeconds: null, isDefault: false },
  )
  limitOpen.value = true
}

async function submitLimit(): Promise<void> {
  limitSaving.value = true
  limitError.value = null
  try {
    if (limitEditing.value) {
      // As with specs: `name` is fixed at creation, and sending it to the update
      // endpoint is a 422 rather than a harmless extra.
      const mutable: RuntimeLimitUpdatePayload = {
        displayName: limitForm.displayName,
        maxRuntimeSeconds: limitForm.maxRuntimeSeconds,
        isDefault: limitForm.isDefault,
      }
      await updateRuntimeLimit(limitEditing.value.id, mutable)
      notifications.success('时长限制已更新')
    } else {
      await createRuntimeLimit(limitForm)
      notifications.success('时长限制已创建')
    }
    limitOpen.value = false
    await loadLimits()
  } catch (error) {
    limitError.value =
      error instanceof ApiError ? error.display(LIMIT_FIELD_LABELS) : '保存失败'
  } finally {
    limitSaving.value = false
  }
}

async function loadLimits(): Promise<void> {
  limits.value = await listRuntimeLimits()
}

// --- nodes -------------------------------------------------------------------
const nodes = ref<ComputeNode[]>([])
const nodesLoading = ref(false)
const syncing = ref(false)
const gpuPanel = ref<{ node: ComputeNode; devices: GpuDevice[] } | null>(null)

async function loadNodes(): Promise<void> {
  nodesLoading.value = true
  try {
    nodes.value = await listNodes()
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
  } finally {
    nodesLoading.value = false
  }
}

async function runSync(): Promise<void> {
  syncing.value = true
  try {
    const result = await syncNodes()
    const parts = [
      `已同步 ${result.nodesSeen} 个节点、${result.devicesSeen} 张加速卡`,
      result.nodesCreated ? `新增 ${result.nodesCreated} 个` : '',
      result.labelsPatched ? `修正 ${result.labelsPatched} 个节点归属标签` : '',
    ].filter(Boolean)
    notifications.success(parts.join('，'))
    if (result.nodesUnpooled) {
      // Not a failure of the sync: it means nobody has told the platform where
      // unfiled nodes should go, so those machines cannot be scheduled at all.
      notifications.warning(
        `有 ${result.nodesUnpooled} 个节点没有资源池，请在资源池中指定一个默认资源池`,
      )
    }
    await loadNodes()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    syncing.value = false
  }
}

async function inspectGpus(node: ComputeNode): Promise<void> {
  gpuPanel.value = { node, devices: await listNodeGpus(node.id) }
}

// --- node assignment ---------------------------------------------------------
const nodeEditor = ref<{ pool: ResourceGroupSummary; selected: number[] } | null>(null)
const nodeSaving = ref(false)

function openNodeEditor(pool: ResourceGroupSummary): void {
  nodeEditor.value = {
    pool,
    selected: nodes.value.filter((node) => node.resourceGroupId === pool.id).map((n) => n.id),
  }
}

async function submitNodeEditor(): Promise<void> {
  const editor = nodeEditor.value
  if (!editor) return
  nodeSaving.value = true
  try {
    const result = await setResourceGroupNodes(editor.pool.id, editor.selected)
    notifications.success(
      `${editor.pool.displayName} 现有 ${editor.selected.length} 个节点` +
        (result.labelsPatched ? `，更新了 ${result.labelsPatched} 个节点标签` : ''),
    )
    nodeEditor.value = null
    // Both tabs change: the pool's node count, and every node's pool.
    await Promise.all([loadPools(), loadNodes()])
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    nodeSaving.value = false
  }
}

// --- deletion ----------------------------------------------------------------
const confirmOpen = ref(false)
const deleting = ref(false)
const pendingDelete = ref<{ kind: 'pool' | 'spec' | 'limit'; id: number; label: string } | null>(
  null,
)

function askDelete(kind: 'pool' | 'spec' | 'limit', id: number, label: string): void {
  pendingDelete.value = { kind, id, label }
  confirmOpen.value = true
}

async function confirmDelete(): Promise<void> {
  if (!pendingDelete.value) return
  deleting.value = true
  const { kind, id } = pendingDelete.value
  try {
    if (kind === 'pool') {
      await deleteResourceGroup(id)
      await loadPools()
    } else if (kind === 'spec') {
      await deleteResourceSpec(id)
      await loadSpecs()
    } else {
      await deleteRuntimeLimit(id)
      await loadLimits()
    }
    notifications.success('已删除')
    confirmOpen.value = false
    pendingDelete.value = null
  } catch (error) {
    // A pool still bound to a workspace cannot be deleted; the backend says so
    // with a 409 and the reason.
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    deleting.value = false
  }
}

onMounted(async () => {
  groups.value = (await listGroups({ pageSize: 200, isActive: true })).items
  await Promise.all([loadPools(), loadSpecs(), loadLimits(), loadNodes()])
})
</script>

<template>
  <div>
    <PageHeader title="资源与配额" subtitle="算力池、资源规格、运行时长与集群节点" />

    <v-tabs v-model="tab" class="mb-4">
      <v-tab value="pools" prepend-icon="mdi-expansion-card">资源池</v-tab>
      <v-tab value="specs" prepend-icon="mdi-chip">资源规格</v-tab>
      <v-tab value="limits" prepend-icon="mdi-timer-sand">运行时长</v-tab>
      <v-tab value="nodes" prepend-icon="mdi-server-network">集群节点</v-tab>
    </v-tabs>

    <v-window v-model="tab">
      <!-- pools -->
      <v-window-item value="pools">
        <div class="d-flex justify-end mb-3">
          <v-btn
            v-if="can('resource_group:create')"
            color="primary"
            prepend-icon="mdi-plus"
            @click="openPool()"
          >
            新建资源池
          </v-btn>
        </div>

        <v-card>
          <v-data-table
            :headers="[
              { title: '资源池', key: 'displayName' },
              { title: '类型', key: 'kind' },
              { title: '归属用户组', key: 'groupName' },
              { title: '网络', key: 'networkType' },
              { title: 'GPU 型号', key: 'gpuModels' },
              { title: '节点', key: 'nodeCount' },
              { title: '规格数', key: 'specCount' },
              { title: '状态', key: 'isActive' },
              { title: '操作', key: 'actions', align: 'end' },
            ]"
            :items="pools"
            :loading="poolsLoading"
            item-value="id"
            hover
          >
            <template #item.displayName="{ item }">
              <div class="d-flex align-center ga-2">
                <span class="font-weight-medium">{{ item.displayName }}</span>
                <v-chip v-if="item.isDefault" size="x-small" variant="flat" color="primary">
                  默认
                </v-chip>
              </div>
              <div class="text-caption text-medium-emphasis">{{ item.name }}</div>
            </template>

            <template #item.nodeCount="{ item }">
              <template v-if="item.isDefault">
                <span class="text-medium-emphasis">{{ item.nodeCount }}（其他池之外）</span>
              </template>
              <template v-else-if="item.nodeCount">
                {{ item.nodeCount }}
              </template>
              <span v-else class="text-medium-emphasis">—</span>
            </template>

            <template #item.kind="{ item }">
              <v-chip size="small" variant="tonal">
                {{ item.kind === 'public' ? '公共' : '专用' }}
              </v-chip>
            </template>

            <template #item.groupName="{ item }">
              {{ item.groupName ?? '全部用户组' }}
            </template>

            <template #item.networkType="{ item }">
              {{ NETWORK_LABELS[item.networkType] }}
            </template>

            <template #item.gpuModels="{ item }">
              <template v-if="item.gpuModels.length">
                <v-chip
                  v-for="model in item.gpuModels"
                  :key="model"
                  size="x-small"
                  variant="tonal"
                  color="primary"
                  class="me-1"
                >
                  {{ model.toUpperCase() }}
                </v-chip>
              </template>
              <span v-else class="text-medium-emphasis">仅 CPU</span>
            </template>

            <template #item.isActive="{ item }">
              <StatusChip :status="item.isActive ? 'active' : 'inactive'" />
            </template>

            <template #item.actions="{ item }">
              <v-btn
                v-if="can('resource_group:update') && !item.isDefault"
                size="small"
                variant="text"
                color="primary"
                @click="openNodeEditor(item)"
              >
                节点
              </v-btn>
              <v-btn
                v-if="can('resource_group:update')"
                size="small"
                variant="text"
                color="primary"
                @click="openPool(item)"
              >
                编辑
              </v-btn>
              <v-btn
                v-if="can('resource_group:delete')"
                size="small"
                variant="text"
                color="error"
                @click="askDelete('pool', item.id, item.displayName)"
              >
                删除
              </v-btn>
            </template>

            <template #no-data>
              <EmptyState
                icon="mdi-expansion-card"
                title="暂无资源池"
                description="资源池是授权的单位：创建工作空间后，把资源池授权给它，用户才有算力可用。"
              />
            </template>
          </v-data-table>
        </v-card>
      </v-window-item>

      <!-- specs -->
      <v-window-item value="specs">
        <div class="d-flex justify-end mb-3">
          <v-btn
            v-if="can('resource_spec:create')"
            color="primary"
            prepend-icon="mdi-plus"
            @click="openSpec()"
          >
            新建规格
          </v-btn>
        </div>

        <v-card>
          <v-data-table
            :headers="[
              { title: '规格', key: 'displayName' },
              { title: 'CPU', key: 'cpuMilli' },
              { title: '内存', key: 'memoryMb' },
              { title: '加速卡', key: 'gpuType' },
              { title: '操作', key: 'actions', align: 'end' },
            ]"
            :items="specs"
            item-value="id"
            hover
          >
            <template #item.displayName="{ item }">
              <div class="font-weight-medium">{{ item.displayName }}</div>
              <div class="text-caption text-medium-emphasis">{{ item.name }}</div>
            </template>
            <template #item.cpuMilli="{ item }">{{ formatCpu(item.cpuMilli) }}</template>
            <template #item.memoryMb="{ item }">{{ formatMemory(item.memoryMb) }}</template>
            <template #item.gpuType="{ item }">
              <span v-if="item.gpuCount === 0" class="text-medium-emphasis">—</span>
              <span v-else>{{ item.gpuCount }} × {{ item.gpuType.toUpperCase() }}</span>
            </template>
            <template #item.actions="{ item }">
              <v-btn
                v-if="can('resource_spec:update')"
                size="small"
                variant="text"
                color="primary"
                @click="openSpec(item)"
              >
                编辑
              </v-btn>
              <v-btn
                v-if="can('resource_spec:delete')"
                size="small"
                variant="text"
                color="error"
                @click="askDelete('spec', item.id, item.displayName)"
              >
                删除
              </v-btn>
            </template>
            <template #no-data>
              <EmptyState
                icon="mdi-chip"
                title="暂无资源规格"
                description="规格是用户在资源池内可选的形状，例如「4 核 8 GB」或「1 × A100」。"
              />
            </template>
          </v-data-table>
        </v-card>
      </v-window-item>

      <!-- runtime limits -->
      <v-window-item value="limits">
        <div class="d-flex justify-end mb-3">
          <v-btn
            v-if="can('runtime_limit:create')"
            color="primary"
            prepend-icon="mdi-plus"
            @click="openLimit()"
          >
            新建时长限制
          </v-btn>
        </div>

        <v-card>
          <v-data-table
            :headers="[
              { title: '名称', key: 'displayName' },
              { title: '最长运行时长', key: 'maxRuntimeSeconds' },
              { title: '默认', key: 'isDefault' },
              { title: '操作', key: 'actions', align: 'end' },
            ]"
            :items="limits"
            item-value="id"
            hover
          >
            <template #item.displayName="{ item }">
              <div class="font-weight-medium">{{ item.displayName }}</div>
              <div class="text-caption text-medium-emphasis">{{ item.name }}</div>
            </template>
            <template #item.maxRuntimeSeconds="{ item }">
              {{ formatDuration(item.maxRuntimeSeconds) }}
            </template>
            <template #item.isDefault="{ item }">
              <v-chip v-if="item.isDefault" size="small" variant="tonal" color="primary">
                默认
              </v-chip>
              <span v-else class="text-medium-emphasis">—</span>
            </template>
            <template #item.actions="{ item }">
              <v-btn
                v-if="can('runtime_limit:update')"
                size="small"
                variant="text"
                color="primary"
                @click="openLimit(item)"
              >
                编辑
              </v-btn>
              <v-btn
                v-if="can('runtime_limit:update')"
                size="small"
                variant="text"
                color="error"
                @click="askDelete('limit', item.id, item.displayName)"
              >
                删除
              </v-btn>
            </template>
            <template #no-data>
              <EmptyState
                icon="mdi-timer-sand"
                title="暂无时长限制"
                description="没有时长限制时，实例可以无限期运行。"
              />
            </template>
          </v-data-table>
        </v-card>
      </v-window-item>

      <!-- nodes -->
      <v-window-item value="nodes">
        <div class="d-flex justify-end mb-3">
          <v-btn
            v-if="can('node:sync')"
            color="primary"
            variant="tonal"
            prepend-icon="mdi-sync"
            :loading="syncing"
            @click="runSync"
          >
            从集群同步
          </v-btn>
        </div>

        <v-card>
          <v-data-table
            :headers="[
              { title: '节点', key: 'name' },
              { title: '状态', key: 'status' },
              { title: '可调度', key: 'schedulable' },
              { title: '资源池', key: 'resourceGroupName' },
              { title: 'CPU', key: 'cpuTotalMilli' },
              { title: '内存', key: 'memoryTotalMb' },
              { title: '加速卡', key: 'gpuCount' },
              { title: '操作', key: 'actions', align: 'end' },
            ]"
            :items="nodes"
            :loading="nodesLoading"
            item-value="id"
            hover
          >
            <template #item.name="{ item }">
              <div class="font-weight-medium">{{ item.displayName }}</div>
              <div class="text-caption text-medium-emphasis">{{ item.name }}</div>
            </template>
            <template #item.status="{ item }">
              <StatusChip :status="item.status" />
            </template>
            <template #item.schedulable="{ item }">
              <StatusChip :status="item.schedulable ? 'schedulable' : 'unschedulable'" />
            </template>
            <template #item.resourceGroupName="{ item }">
              <v-chip v-if="item.resourceGroupName" size="small" variant="tonal" color="primary">
                {{ item.resourceGroupName }}
              </v-chip>
              <!-- No pool means no workload can ever be scheduled here: the sync
                   could not decide where the node belongs. -->
              <v-tooltip
                v-else
                text="尚未归属任何资源池，无法被调度。请设置默认资源池，或把节点分配给某个资源池。"
                location="top"
              >
                <template #activator="{ props }">
                  <v-chip v-bind="props" size="small" variant="tonal" color="warning">
                    未归属
                  </v-chip>
                </template>
              </v-tooltip>
            </template>
            <template #item.cpuTotalMilli="{ item }">
              {{ formatCpu(item.cpuAllocatableMilli) }}
              <span class="text-caption text-medium-emphasis">
                / {{ formatCpu(item.cpuTotalMilli) }}
              </span>
            </template>
            <template #item.memoryTotalMb="{ item }">
              {{ formatMemory(item.memoryAllocatableMb) }}
              <span class="text-caption text-medium-emphasis">
                / {{ formatMemory(item.memoryTotalMb) }}
              </span>
            </template>
            <template #item.gpuCount="{ item }">
              <template v-if="item.gpuCount">
                <v-chip size="small" variant="tonal" color="primary">
                  {{ item.gpuCount }} × {{ item.gpuModels.join('/').toUpperCase() }}
                </v-chip>
              </template>
              <span v-else class="text-medium-emphasis">—</span>
            </template>
            <template #item.actions="{ item }">
              <v-btn
                v-if="item.gpuCount"
                size="small"
                variant="text"
                color="primary"
                @click="inspectGpus(item)"
              >
                查看加速卡
              </v-btn>
            </template>
            <template #no-data>
              <EmptyState
                icon="mdi-server-network"
                title="尚无节点数据"
                description="点击「从集群同步」拉取节点与加速卡清单。"
              />
            </template>
          </v-data-table>
        </v-card>
      </v-window-item>
    </v-window>

    <!-- pool editor -->
    <v-dialog v-model="poolOpen" max-width="680" persistent scrollable>
      <v-card>
        <v-card-title>{{ poolEditing ? '编辑资源池' : '新建资源池' }}</v-card-title>
        <v-card-text>
          <v-alert
            v-if="poolError"
            type="error"
            variant="tonal"
            density="compact"
            class="mb-4 wl-form-error"
          >
            {{ poolError }}
          </v-alert>

          <v-text-field v-model="poolForm.name" label="标识 *" :disabled="!!poolEditing" class="mb-2" />
          <v-text-field v-model="poolForm.displayName" label="显示名称" class="mb-2" />
          <v-textarea v-model="poolForm.description" label="描述" rows="2" auto-grow class="mb-2" />

          <v-row dense>
            <v-col cols="12" sm="6">
              <v-select
                v-model="poolForm.kind"
                :items="[
                  { value: 'private', title: '专用（归属单一用户组）' },
                  { value: 'public', title: '公共（所有用户组可用）' },
                ]"
                label="类型"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="poolForm.groupId"
                :items="assignableGroups"
                item-title="displayName"
                item-value="id"
                label="归属用户组"
                :disabled="poolForm.kind === 'public'"
                :hint="poolForm.kind === 'public' ? '公共资源池不属于任何用户组' : ''"
                persistent-hint
              />
            </v-col>
          </v-row>

          <v-row dense>
            <v-col cols="12" sm="6">
              <v-select
                v-model="poolForm.networkType"
                :items="[
                  { value: 'isolated', title: '隔离网络' },
                  { value: 'internal', title: '集群内网' },
                  { value: 'internet', title: '可访问外网' },
                ]"
                label="网络策略"
              />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field
                v-model.number="poolForm.maxRuntimeSeconds"
                type="number"
                label="最长运行时长（秒，留空不限）"
              />
            </v-col>
          </v-row>

          <v-select
            v-model="poolForm.specIds"
            :items="specs"
            item-title="displayName"
            item-value="id"
            label="提供的资源规格"
            multiple
            chips
            closable-chips
            class="mb-4"
          />

          <v-switch
            v-model="poolForm.isDefault"
            color="primary"
            hide-details="auto"
            label="设为默认资源池"
            :messages="[
              '每个节点都必须属于某个资源池。未被其他资源池认领的节点会归入默认资源池，且其节点范围由其他资源池决定，不能手工编辑。',
            ]"
            class="mb-4"
          />

          <h4 class="text-subtitle-2 mb-1">单用户上限</h4>
          <p class="text-caption text-medium-emphasis mb-2">
            该资源池内单个用户最多可占用的资源。<code>-1</code> 表示不校验该维度，<code>0</code>
            表示完全不允许。 可创建的实例、应用、自定义业务数量不在这里，而在用户关联的配额模板中：
            「最多能拥有几个」属于用户，而不是资源池。
          </p>
          <v-row dense>
            <v-col cols="12" sm="4">
              <v-text-field
                v-model.number="poolForm.limits.maxCpuCoresPerUser"
                type="number"
                label="CPU（核）"
                hint="-1 表示不限制"
                persistent-hint
              />
            </v-col>
            <v-col cols="12" sm="4">
              <v-text-field
                v-model.number="poolForm.limits.maxMemoryGbPerUser"
                type="number"
                label="内存（GB）"
                hint="-1 表示不限制"
                persistent-hint
              />
            </v-col>
            <v-col cols="12" sm="4">
              <v-text-field
                v-model.number="poolForm.limits.maxGpuPerUser"
                type="number"
                label="加速卡（张）"
                hint="-1 表示不限制"
                persistent-hint
              />
            </v-col>
            <v-col cols="12" sm="4">
              <v-text-field
                v-model.number="poolForm.limits.maxStorageGbPerUser"
                type="number"
                label="存储（GB）"
                hint="-1 表示不限制"
                persistent-hint
              />
            </v-col>
          </v-row>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="poolOpen = false">取消</v-btn>
          <v-btn
            color="primary"
            variant="flat"
            :loading="poolSaving"
            :disabled="!poolForm.name || (poolForm.kind === 'private' && !poolForm.groupId)"
            @click="submitPool"
          >
            保存
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- spec editor -->
    <v-dialog v-model="specOpen" max-width="520" persistent>
      <v-card>
        <v-card-title>{{ specEditing ? '编辑资源规格' : '新建资源规格' }}</v-card-title>
        <v-card-text>
          <v-alert
            v-if="specError"
            type="error"
            variant="tonal"
            density="compact"
            class="mb-4 wl-form-error"
          >
            {{ specError }}
          </v-alert>
          <v-text-field v-model="specForm.name" label="标识 *" :disabled="!!specEditing" class="mb-2" />
          <v-text-field v-model="specForm.displayName" label="显示名称" class="mb-2" />
          <v-row dense>
            <v-col cols="12" sm="6">
              <v-text-field v-model.number="specForm.cpuMilli" type="number" label="CPU（milli）*" />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field v-model.number="specForm.memoryMb" type="number" label="内存（MB）*" />
            </v-col>
            <v-col cols="12" sm="6">
              <v-text-field v-model.number="specForm.gpuCount" type="number" label="加速卡数量" />
            </v-col>
            <v-col cols="12" sm="6">
              <v-select
                v-model="specForm.gpuType"
                :items="GPU_MODEL_ITEMS"
                label="加速卡型号"
                :disabled="specForm.gpuCount === 0"
              />
            </v-col>
          </v-row>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="specOpen = false">取消</v-btn>
          <v-btn color="primary" variant="flat" :loading="specSaving" @click="submitSpec">
            保存
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- runtime limit editor -->
    <v-dialog v-model="limitOpen" max-width="480" persistent>
      <v-card>
        <v-card-title>{{ limitEditing ? '编辑时长限制' : '新建时长限制' }}</v-card-title>
        <v-card-text>
          <v-alert
            v-if="limitError"
            type="error"
            variant="tonal"
            density="compact"
            class="mb-4 wl-form-error"
          >
            {{ limitError }}
          </v-alert>
          <v-text-field v-model="limitForm.name" label="标识 *" :disabled="!!limitEditing" class="mb-2" />
          <v-text-field v-model="limitForm.displayName" label="显示名称" class="mb-2" />
          <v-text-field
            v-model.number="limitForm.maxRuntimeSeconds"
            type="number"
            label="最长运行时长（秒，留空不限）"
            class="mb-2"
          />
          <v-checkbox v-model="limitForm.isDefault" hide-details label="设为默认选项" />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="limitOpen = false">取消</v-btn>
          <v-btn color="primary" variant="flat" :loading="limitSaving" @click="submitLimit">
            保存
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- node assignment -->
    <v-dialog
      :model-value="nodeEditor !== null"
      max-width="560"
      persistent
      @update:model-value="nodeEditor = null"
    >
      <v-card v-if="nodeEditor">
        <v-card-title>{{ nodeEditor.pool.displayName }} 的节点</v-card-title>
        <v-card-text>
          <v-select
            v-model="nodeEditor.selected"
            :items="nodes"
            item-title="displayName"
            item-value="id"
            label="属于该资源池的节点"
            multiple
            chips
            closable-chips
            hide-details="auto"
            :menu-props="{ maxHeight: 320 }"
          >
            <template #item="{ props: itemProps, item }">
              <v-list-item v-bind="itemProps" :subtitle="item.name">
                <template #append>
                  <v-chip
                    v-if="item.resourceGroupName"
                    size="x-small"
                    variant="tonal"
                    :color="item.resourceGroupId === nodeEditor.pool.id ? 'primary' : undefined"
                  >
                    {{ item.resourceGroupName }}
                  </v-chip>
                </template>
              </v-list-item>
            </template>
          </v-select>

          <p class="text-caption text-medium-emphasis mt-3">
            取消勾选的节点会移入默认资源池，而不是变成无归属。保存时会同步修改集群上节点的
            <code>wl-pool</code> 标签——调度器读的是标签，所以这一步之后新任务才会落到新位置。
            已经在运行的实例和任务不受影响，会继续在原节点上跑完。
          </p>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="nodeEditor = null">取消</v-btn>
          <v-btn color="primary" :loading="nodeSaving" @click="submitNodeEditor">保存</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <!-- gpu detail -->
    <v-dialog :model-value="gpuPanel !== null" max-width="460" @update:model-value="gpuPanel = null">
      <v-card v-if="gpuPanel">
        <v-card-title>{{ gpuPanel.node.displayName }} 的加速卡</v-card-title>
        <v-card-text>
          <v-list density="compact">
            <v-list-item v-for="device in gpuPanel.devices" :key="device.id">
              <v-list-item-title>
                #{{ device.index }} · {{ device.model.toUpperCase() }}
              </v-list-item-title>
              <v-list-item-subtitle>
                {{ device.memoryMb ? formatMemory(device.memoryMb) : '显存未知' }}
                <span v-if="device.uuid"> · {{ device.uuid }}</span>
              </v-list-item-subtitle>
              <template #append>
                <StatusChip :status="device.status" />
              </template>
            </v-list-item>
          </v-list>
        </v-card-text>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="confirmOpen"
      title="确认删除"
      :text="
        pendingDelete
          ? `将删除「${pendingDelete.label}」。${
              pendingDelete.kind === 'pool'
                ? '若仍有工作空间在使用它，删除会被拒绝。'
                : ''
            }`
          : ''
      "
      confirm-text="删除"
      confirm-color="error"
      :loading="deleting"
      @confirm="confirmDelete"
    />
  </div>
</template>
