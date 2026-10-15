/**
 * Mount every view for real and assert it renders its data.
 *
 * `vue-tsc` proves the templates are well typed and `vite build` proves they
 * compile, but neither one ever *runs* `setup()`. A view can pass both gates and
 * still fail on first paint — reading a field the API does not send, indexing a
 * map with a value it does not contain, iterating something the API returns in a
 * different shape. Those are exactly the bugs that reach a user, and they are
 * invisible to the compiler.
 *
 * So this script bundles the views for the browser, mounts each one into a
 * `happy-dom` document against stubbed API modules and a seeded store, waits for
 * its data to arrive, and then asserts that the fixture's own text is in the
 * result. That last step is what makes it worth running: "it did not throw" is a
 * weak claim, because a view whose data never arrives renders a perfectly valid
 * empty state.
 *
 * Two approaches were tried and rejected before this one:
 *
 * - **`renderToString`.** SSR never runs `onMounted`, where every one of these
 *   views fetches its data, and re-rendering builds a fresh component instance,
 *   so the loaded state never survives to a second pass. There is no way to make
 *   it observe data flow.
 * - **`vite.ssrLoadModule` plus a DOM.** The modules come back compiled for the
 *   server (`ssrRender`, `useSSRContext`), so Vuetify's client components fail
 *   on a missing SSR context. The views have to be built for the browser.
 *
 * Hence: a real `vite build`, then a dynamic import once the DOM exists.
 *
 *     node scripts/smoke_frontend_views.mjs [ViewName ...]
 *
 * With no arguments it mounts every view in `src/views/`. Exits non-zero if any
 * view throws or renders without its expected content.
 */

import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { basename, extname, join, relative } from 'node:path'
import { fileURLToPath, pathToFileURL, URL } from 'node:url'

const FRONTEND = fileURLToPath(new URL('../frontend', import.meta.url))
const SRC = join(FRONTEND, 'src')
const VIEWS = join(SRC, 'views')

const { build } = await import(
  pathToFileURL(join(FRONTEND, 'node_modules/vite/dist/node/index.js')).href
)

// ---------------------------------------------------------------------------
// Fixtures
//
// One realistic row per collection, not zero: an empty list renders the
// `EmptyState` branch and skips the table and card markup entirely, which is
// where nearly all of the interesting template code lives.
// ---------------------------------------------------------------------------

const WORKSPACE = {
  id: 1,
  name: 'nlp',
  displayName: '自然语言处理',
  description: 'NLP 实验室',
  groupId: 1,
  groupName: 'nlp-lab',
  status: 'ready',
  isActive: true,
  quota: { cpuMilli: 64000, memoryMb: 262144, gpuCount: 8, storageGb: 4096 },
  used: { cpuMilli: 16000, memoryMb: 65536, gpuCount: 2, storageGb: 512 },
  createdAt: '2025-01-01T00:00:00Z',
  updatedAt: '2025-01-02T00:00:00Z',
}

const STORAGE = {
  id: 7,
  name: 'my-data',
  displayName: '我的数据盘',
  description: '训练数据',
  kind: 'user_volume',
  status: 'bound',
  statusMessage: null,
  // The directory *is* the volume now, so it is the field the table has to show.
  // Left as `undefined` by an out-of-date fixture, it renders an empty cell that
  // still passes every "did not throw" check — hence it is in `EXPECTS` below.
  hostPath: '/storage/kube-wl/nlp/zhangsan',
  dirName: null,
  isGroupLevel: false,
  capacityGb: 50,
  usedGb: 12,
  defaultMountPath: '/home/zhangsan',
  accessMode: 'ReadWriteOnce',
  pvcName: 'wl-vol-my-data-7',
  k8sNamespace: 'wl-nlp-nlp',
  source: null,
  isReadOnly: false,
  isActive: true,
  workspaceId: 1,
  workspaceName: '自然语言处理',
  groupId: 1,
  groupName: 'nlp',
  ownerUserId: 2,
  ownerUsername: 'zhangsan',
  ownerDisplayName: '张三',
  attachedInstanceIds: [3],
  attachedInstanceNames: ['train-a'],
  checkedAt: '2025-01-03T00:00:00Z',
  createdAt: '2025-01-01T00:00:00Z',
  updatedAt: '2025-01-03T00:00:00Z',
}

const IMAGE = {
  id: 11,
  reference: 'registry.local/ai/pytorch:2.4-cuda12',
  displayName: 'PyTorch 2.4 CUDA 12',
  description: 'PyTorch 官方镜像，含 CUDA 12.1 运行时。',
  registry: 'registry.local',
  project: 'ai',
  repository: 'pytorch',
  tag: '2.4-cuda12',
  digest: 'sha256:abc123',
  kind: 'official',
  visibility: 'global',
  sizeBytes: 7516192768,
  layerCount: 14,
  verified: true,
  syncedAt: '2025-01-03T00:00:00Z',
  syncError: null,
  ownerUserId: null,
  ownerUsername: null,
  workspaceId: null,
  groupId: null,
  isPinned: true,
  isActive: true,
  usageCount: 3,
  canDelete: false,
  blockedReason: '官方镜像由管理员维护',
  createdAt: '2025-01-01T00:00:00Z',
  updatedAt: '2025-01-03T00:00:00Z',
}

const INSTANCE = {
  id: 3,
  name: 'train-a',
  displayName: '训练 A',
  description: null,
  status: 'running',
  image: IMAGE.reference,
  ideType: 'jupyter',
  workspaceId: 1,
  resourceGroupId: 1,
  resourceGroupName: 'gpu-a100',
  resourceSpecId: 1,
  resourceSpecName: 'a100-x1',
  cpuMilli: 8000,
  memoryMb: 32768,
  gpuCount: 1,
  gpuType: 'a100',
  sshEnabled: true,
  url: 'https://ide.example.edu/instances/3',
  startedAt: '2025-01-03T00:00:00Z',
  maxRuntimeSeconds: 86400,
  createdAt: '2025-01-03T00:00:00Z',
  updatedAt: '2025-01-03T00:00:00Z',
}

// The exact `UserSummary` wire shape. It used to carry a `groups` array that the
// API has never sent, and no `fullName` at all — a mismatch nothing caught,
// because `views/admin/` was outside the harness's non-recursive discovery and
// the view was therefore never mounted. The moment discovery went recursive, the
// name column rendered "—" and the mistake surfaced.
const USER = {
  id: 2,
  username: 'zhangsan',
  fullName: '张三',
  displayName: '张三',
  email: 'zhangsan@example.edu',
  source: 'ldap',
  isActive: true,
  isSuperuser: false,
  expiresAt: null,
  lastLoginAt: '2025-01-03T00:00:00Z',
  roles: ['user'],
  primaryGroupId: 1,
  primaryGroupName: 'NLP 实验室',
}

const GROUP = {
  id: 1,
  name: 'nlp-lab',
  displayName: 'NLP 实验室',
  description: null,
  isActive: true,
  memberCount: 12,
  createdAt: '2025-01-01T00:00:00Z',
  updatedAt: '2025-01-01T00:00:00Z',
}

/**
 * A resource pool, in the shape `ResourceGroupDetail` actually declares.
 *
 * This fixture used to carry `nodeSelector` and a `limits` block with invented
 * field names (`maxGpuCountPerUser`, `maxInstancesPerUser`). Nothing failed,
 * because no view's assertions reached those fields — which is exactly the
 * problem: a fixture that does not match the API is a stub that cannot catch a
 * view reading a field the server never sends. It is reconstructed from
 * `frontend/src/types/resource.ts`, and `check-frontend-types.py` is what keeps
 * the two honest.
 */
const RESOURCE_GROUP = {
  id: 1,
  name: 'gpu-a100',
  displayName: 'A100 资源组',
  description: null,
  kind: 'public',
  groupId: null,
  groupName: null,
  networkType: 'default',
  maxRuntimeSeconds: 86400,
  storageClass: 'ceph-rbd',
  isActive: true,
  gpuModels: ['A100'],
  specCount: 1,
  createdAt: '2025-01-01T00:00:00Z',
  updatedAt: '2025-01-01T00:00:00Z',
  // Detail-only fields.
  k8sLabelSelector: { 'gpu-type': 'a100' },
  k8sTolerations: [],
  limits: {
    // Cores and GB, matching what the API actually returns: there is no `id` on
    // this object, and the milli/MB fields are gone.
    maxGpuPerUser: 2,
    maxCpuCoresPerUser: 32,
    maxMemoryGbPerUser: 128,
    maxStorageGbPerUser: 500,
  },
  specs: [
    {
      id: 1,
      name: 'a100-x1',
      displayName: 'A100 × 1',
      cpuMilli: 8000,
      memoryMb: 32768,
      gpuCount: 1,
      gpuType: 'a100',
      isActive: true,
    },
  ],
}

const APPLICATION = {
  id: 5,
  name: 'jupyter-pytorch',
  displayName: 'Jupyter + PyTorch',
  description: '一键启动的开发环境',
  category: 'development',
  visibility: 'workspace',
  image: IMAGE.reference,
  ideType: 'jupyter',
  defaultCpuMilli: 8000,
  defaultMemoryMb: 32768,
  defaultGpuType: 'a100',
  defaultGpuCount: 1,
  workspaceId: 1,
  isCurated: false,
  isActive: true,
  ownerUserId: 2,
  ownerUsername: 'zhangsan',
  launchCount: 4,
  createdAt: '2025-01-01T00:00:00Z',
  updatedAt: '2025-01-01T00:00:00Z',
}

const TASK = {
  id: 9,
  name: 'train-run-1',
  displayName: '训练第一轮',
  status: 'succeeded',
  image: IMAGE.reference,
  script: 'python train.py',
  workspaceId: 1,
  resourceGroupId: 1,
  resourceSpecId: 1,
  cpuMilli: 8000,
  memoryMb: 32768,
  gpuType: 'a100',
  gpuCount: 1,
  exitCode: 0,
  submittedAt: '2025-01-03T00:00:00Z',
  startedAt: '2025-01-03T00:00:00Z',
  finishedAt: '2025-01-03T01:00:00Z',
  createdAt: '2025-01-03T00:00:00Z',
  updatedAt: '2025-01-03T01:00:00Z',
}

const EVENT = {
  id: 1,
  eventType: 'created',
  message: '已创建',
  actorUsername: 'zhangsan',
  createdAt: '2025-01-03T00:00:00Z',
}

const page = (items) => ({
  items,
  meta: { page: 1, pageSize: 20, total: items.length, pages: items.length ? 1 : 0 },
})

/**
 * Named return values, keyed by API function name.
 *
 * Anything unlisted gets a paginated empty result. That is the safe default: it
 * renders, and it stops an unlisted endpoint from appearing to return data the
 * view would then mishandle.
 */
/**
 * A full totals object with only the GPU figure varied.
 *
 * The series fixture needs one point per day and the other measures are noise
 * for the chart under test; spelling all ten fields out three times would make
 * the interesting number harder to find, not easier.
 */
function dayTotals(gpuHours) {
  return {
    runtimeSeconds: gpuHours * 3600,
    gpuSeconds: gpuHours * 3600,
    cpuMilliSeconds: gpuHours * 36000,
    memoryMbSeconds: gpuHours * 20480,
    workloadCount: gpuHours > 0 ? 2 : 0,
    gpuHours,
    cpuCoreHours: gpuHours * 0.01,
    memoryGbHours: gpuHours * 0.02,
    userCount: gpuHours > 0 ? 1 : 0,
    workspaceCount: gpuHours > 0 ? 1 : 0,
  }
}

const RETURNS = {
  listStorages: page([STORAGE]),
  listImages: page([IMAGE]),
  listInstances: page([INSTANCE]),
  listApplications: page([APPLICATION]),
  listTasks: page([TASK]),
  listUsers: page([USER]),
  listGroups: page([GROUP]),
  listWorkspaces: page([WORKSPACE]),
  listResourceGroups: page([RESOURCE_GROUP]),
  listResourceSpecs: page([RESOURCE_GROUP.specs[0]]),
  getResourceGroup: RESOURCE_GROUP,
  // One node, so `ComputeView`'s node table renders a row rather than an empty
  // state. The view only shows this section to a caller holding `node:read`, and
  // the fixture role below is `global_admin`, so it is reached.
  listNodes: [
    {
      id: 1,
      name: 'gpu-node-01',
      displayName: 'gpu-node-01',
      status: 'ready',
      schedulable: true,
      cpuTotalMilli: 128000,
      memoryTotalMb: 524288,
      cpuAllocatableMilli: 126000,
      memoryAllocatableMb: 500000,
      labels: { 'gpu-type': 'a100' },
      isManaged: true,
      gpuCount: 8,
      gpuModels: ['A100'],
      lastSeenAt: '2025-01-01T00:00:00Z',
    },
  ],
  // The system page is the one screen whose whole payload is a single object with
  // no list inside it; a missing fixture would leave it rendering against
  // `undefined` fields, so every field the template touches is present here.
  getSystemInfo: {
    appName: 'kube-wl-ai',
    appVersion: '0.8.0',
    environment: 'production',
    debug: false,
    authProviders: ['ldap', 'local'],
    features: [
      {
        key: 'ldap',
        title: 'LDAP / FreeIPA 目录',
        enabled: true,
        detail: '绑定 ldaps://ipa.example.edu，基准 DN dc=example,dc=edu',
        configKey: 'LDAP_URI',
      },
      {
        key: 'grafana',
        title: 'Grafana 监控',
        enabled: false,
        detail: '未配置，监控中心只显示平台自身统计',
        configKey: 'GRAFANA_PUBLIC_URL',
      },
    ],
    warnings: [
      {
        code: 'storage_class_missing',
        severity: 'warning',
        title: '有存储类型没有配置 StorageClass',
        detail: '对应的「创建数据盘」会在运行时才失败。',
        remedy: 'STORAGE_CLASS_DATA',
      },
      {
        code: 'debug_in_production',
        severity: 'critical',
        title: '生产环境开启了 DEBUG',
        detail: '调试模式会输出更详细的错误信息。',
        remedy: 'DEBUG=false',
      },
    ],
    accessTokenMinutes: 60,
    refreshTokenDays: 14,
    loginRateLimitEnabled: true,
    loginRateLimitWindowSeconds: 900,
    loginRateLimitAttemptsPerIp: 30,
    loginRateLimitAttemptsPerUsername: 8,
    counts: {
      users: 128,
      activeUsers: 120,
      groups: 6,
      workspaces: 9,
      nodes: 12,
      gpus: 96,
      instances: 41,
      runningInstances: 23,
    },
    sshMode: 'nodeport',
    sshHost: 'ssh.example.edu',
    storageClasses: { home: 'ceph-rbd', data: null, share: 'cephfs' },
  },
  listQuotas: [],
  listRuntimeLimits: [],
  listRoles: [{ id: 1, code: 'user', name: '普通用户', description: null, permissions: [] }],
  listPermissions: { permissions: [], roles: [] },
  listWorkspaceMembers: [],
  listWorkspaceGrants: [],
  listSshKeys: [
    {
      id: 1,
      name: 'laptop',
      fingerprint: 'SHA256:abc',
      publicKey: 'ssh-ed25519 AAAA',
      createdAt: '2025-01-01T00:00:00Z',
      lastUsedAt: null,
    },
  ],
  listAuditLogs: page([
    {
      id: 41,
      createdAt: '2025-01-03T10:15:00Z',
      actorUserId: 2,
      actorUsername: 'zhangsan',
      action: 'instance:create',
      objectType: 'instance',
      objectId: '7',
      status: 'success',
      detail: { method: 'POST', path: 'instances' },
      ipAddress: '10.0.0.9',
      requestId: 'req-abcdef123456',
      errorMessage: null,
    },
    {
      // Unattributed: a failed login carries the name that was *typed*, and the
      // view must mark it rather than present it as a verified identity.
      id: 40,
      createdAt: '2025-01-03T10:14:00Z',
      actorUserId: null,
      actorUsername: 'root',
      action: 'auth:login',
      objectType: 'user',
      objectId: null,
      status: 'denied',
      detail: { username: 'root', provider: 'failed' },
      ipAddress: '10.0.0.9',
      requestId: 'req-abcdef123455',
      errorMessage: '用户名或密码错误',
    },
  ]),
  getAuditMeta: {
    actions: ['instance:create', 'auth:login'],
    objectTypes: ['instance', 'user'],
    statuses: ['success', 'denied', 'error'],
  },
  listApiTokens: [
    {
      id: 1,
      name: 'ci-deploy',
      prefix: 'wl_abcdefgh',
      scopes: ['instance:read', 'task:create'],
      expiresAt: '2026-01-01T00:00:00Z',
      lastUsedAt: '2025-01-03T09:00:00Z',
      createdAt: '2025-01-01T00:00:00Z',
    },
    {
      id: 2,
      name: 'inherit-all',
      prefix: 'wl_ijklmnop',
      scopes: [],
      expiresAt: null,
      lastUsedAt: null,
      createdAt: '2025-01-02T00:00:00Z',
    },
  ],
  listStorageEvents: [EVENT],
  listImageEvents: [EVENT],
  listApplicationEvents: [EVENT],
  listTaskEvents: [EVENT],
  listAttachableStorages: [STORAGE],
  listNodeGpus: [],

  getMyWorkspace: WORKSPACE,
  getWorkspace: WORKSPACE,
  getStorage: STORAGE,
  getImage: IMAGE,
  getInstance: INSTANCE,
  getApplication: APPLICATION,
  getTask: TASK,
  getUser: USER,
  getGroup: GROUP,
  getResourceGroup: RESOURCE_GROUP,
  getStorageUsage: { count: 3, capacityGb: 137 },
  // Figures chosen so no expected string is a substring of any other: a total of
  // 150 once made the assertion "50 GB" pass against the wrong field, which is
  // how a planted display bug slipped through.
  getUsageSummary: {
    dayFrom: '2026-03-01',
    dayTo: '2026-03-30',
    totals: {
      runtimeSeconds: 432000,
      gpuSeconds: 918000,
      cpuMilliSeconds: 72000000,
      memoryMbSeconds: 54000000,
      workloadCount: 17,
      gpuHours: 255,
      cpuCoreHours: 20,
      memoryGbHours: 14.6,
      userCount: 4,
      workspaceCount: 2,
    },
    selfTotals: {
      runtimeSeconds: 108000,
      gpuSeconds: 91800,
      cpuMilliSeconds: 7200000,
      memoryMbSeconds: 5400000,
      workloadCount: 3,
      gpuHours: 25.5,
      cpuCoreHours: 2,
      memoryGbHours: 1.46,
      userCount: 1,
      workspaceCount: 1,
    },
    covered: true,
  },
  getUsageSeries: {
    dayFrom: '2026-03-01',
    dayTo: '2026-03-03',
    points: [
      { day: '2026-03-01', totals: dayTotals(37) },
      { day: '2026-03-02', totals: dayTotals(41) },
      { day: '2026-03-03', totals: dayTotals(0) },
    ],
  },
  getUsageBreakdown: {
    by: 'user',
    dayFrom: '2026-03-01',
    dayTo: '2026-03-30',
    truncated: false,
    rows: [
      {
        key: '1',
        label: '张三',
        entityId: 1,
        totals: {
          runtimeSeconds: 108000,
          gpuSeconds: 61200,
          cpuMilliSeconds: 7200000,
          memoryMbSeconds: 5400000,
          workloadCount: 3,
          gpuHours: 17,
          cpuCoreHours: 2,
          memoryGbHours: 1.46,
          userCount: 1,
          workspaceCount: 1,
        },
        kinds: { instance: 61200 },
      },
      {
        key: '2',
        label: '李四',
        entityId: 2,
        totals: {
          runtimeSeconds: 72000,
          gpuSeconds: 43200,
          cpuMilliSeconds: 3600000,
          memoryMbSeconds: 2700000,
          workloadCount: 2,
          gpuHours: 12,
          cpuCoreHours: 1,
          memoryGbHours: 0.73,
          userCount: 1,
          workspaceCount: 1,
        },
        kinds: { task: 43200 },
      },
    ],
  },
  getUsageMeta: {
    dimensions: [
      { key: 'user', label: '按用户' },
      { key: 'group', label: '按用户组' },
      { key: 'workspace', label: '按工作空间' },
      { key: 'gpu_type', label: '按加速卡型号' },
      { key: 'workload_kind', label: '按负载类型' },
    ],
    kinds: [
      { value: 'instance', label: '开发实例' },
      { value: 'task', label: '自定义任务' },
    ],
    maxRangeDays: 731,
    defaultRollupLookbackDays: 3,
  },
  rollupUsage: {
    dayFrom: '2026-03-01',
    dayTo: '2026-03-30',
    rowsWritten: 9,
    instancesScanned: 6,
    tasksScanned: 3,
    runtimeSeconds: 432000,
    gpuSeconds: 918000,
    outsideWindow: 4,
  },
  downloadUsageCsv: undefined,
  getMonitorOverview: {
    enabled: true,
    disabledReason: null,
    scope: {
      namespaces: ['wl-nlp-alpha'],
      pods: ['wl-dev-a-1-abc'],
      unrestricted: false,
      empty: false,
    },
    inventory: {
      nodes: [
        {
          id: 1,
          name: 'gpu-node-01',
          displayName: '计算节点一',
          status: 'ready',
          schedulable: true,
          gpuCount: 4,
          cpuTotalMilli: 64000,
          memoryTotalMb: 262144,
        },
        {
          id: 2,
          name: 'gpu-node-02',
          displayName: '计算节点二',
          status: 'cordoned',
          schedulable: false,
          gpuCount: 2,
          cpuTotalMilli: 32000,
          memoryTotalMb: 131072,
        },
      ],
      nodesReady: 1,
      gpuByModel: [
        { model: 'A100', total: 6, allocated: 3 },
        { model: 'V100', total: 4, allocated: 0 },
      ],
      instances: [
        { status: 'running', count: 5 },
        { status: 'stopped', count: 2 },
      ],
    },
    containers: [
      {
        id: 7,
        name: 'dev-dana',
        displayName: '戴娜的开发环境',
        namespace: 'wl-nlp-alpha',
        podName: 'wl-dev-dana-7-abcde',
        ownerUsername: 'dana',
        status: 'running',
        gpuType: 'A100',
        gpuCount: 1,
        nodeName: 'gpu-node-01',
      },
    ],
    dashboards: [
      {
        key: 'my-containers',
        uid: 'wl-my-containers',
        title: '我的容器',
        description: '本人容器的资源曲线。',
        icon: 'mdi-cube-outline',
      },
    ],
    embeds: {
      'my-containers': {
        key: 'my-containers',
        url: '/grafana/d/wl-my-containers?auth_token=stub&var-namespace=wl-nlp-alpha',
        expiresIn: 300,
      },
    },
  },
  mintMonitorEmbed: {
    key: 'my-containers',
    url: '/grafana/d/wl-my-containers?auth_token=stub2&var-namespace=wl-nlp-alpha',
    expiresIn: 300,
  },
  getImageUsage: { count: 2, totalBytes: 7516192768, inUse: 3 },
  getRegistryStatus: {
    reachable: true,
    version: 'v2.9.0',
    message: null,
    host: 'registry.local',
    projects: ['ai'],
  },
  fetchCurrentUser: USER,
  fetchPermissions: {
    permissions: [
      'storage:read', 'storage:manage',
      'image:read', 'image:create', 'image:share', 'image:delete',
      'instance:read', 'instance:create', 'application:read', 'application:create',
      'task:read', 'task:create',
    ],
    scope: 'group',
    scopeGroupIds: [1],
    scopeWorkspaceIds: [1],
    globalAdmin: false,
    roles: ['user'],
  },
  fetchLoginOptions: { providers: ['local'], ldapEnabled: false, oidcEnabled: false },
  getInstanceLogs: { logs: 'hello\n' },
  getTaskLogs: { logs: 'hello\n' },
  getIdeAccess: { url: 'https://ide.example.edu/instances/3', expiresAt: '2025-01-03T01:00:00Z' },
  terminalSocketUrl: 'wss://example.edu/ws/instances/3/terminal',
  getUserPermissions: {
    permissions: [],
    scope: 'group',
    scopeGroupIds: [1],
    scopeWorkspaceIds: [1],
    globalAdmin: false,
  },
  syncGroups: { created: 0, updated: 0, removed: 0, failed: 0 },
  syncNodes: { created: 0, updated: 0, removed: 0, failed: 0 },
  syncImages: { created: 0, updated: 0, unchanged: 0, removed: 0, failed: 0, errors: [], total: 0 },
  refreshUsageCounts: { updated: 0 },
  reconcileStorage: STORAGE,
  reconcileInstance: INSTANCE,
  reconcileTask: TASK,
  reconcileWorkspace: WORKSPACE,
  createStorage: STORAGE,
  updateStorage: STORAGE,
  resizeStorage: STORAGE,
  deleteStorage: STORAGE,
  registerImage: IMAGE,
  updateImage: IMAGE,
  deleteImage: IMAGE,
  login: { accessToken: 'token', expiresIn: 900 },
  refresh: { accessToken: 'token', expiresIn: 900 },
  logout: { message: 'ok' },
}

const DEFAULT_RETURN = page([])

/**
 * Text that must never appear, in any view.
 *
 * This is the check that earns its keep. A view that renders a field the API
 * does not send prints the literal `undefined` and otherwise looks fine — it
 * passes the compiler, it passes "did not throw", and it passes a substring
 * assertion whenever a fixture value happens to contain the expected text. (A
 * planted bug in `StorageView` escaped exactly that way: the expected `50 GB`
 * matched inside the fixture's unrelated `150 GB`.)
 *
 * Catching the symptom directly is fixture-independent, so it works for views
 * nobody remembered to write expectations for.
 */
const NEVER_RENDER = ['undefined', 'NaN', '[object Object]']

/**
 * Interactions to run after a view has loaded, before it is asserted on.
 *
 * Some of the most damaging bugs live behind a click: a dialog whose select
 * offers a choice the server will always reject, a form that submits into a
 * guaranteed failure. Those branches are unreachable from a first-paint snapshot,
 * so a view can pass every existing check while the dialog it opens is useless.
 *
 * Each hook receives the mounted container and `{ flush, auth }`, and returns extra
 * HTML to append to what gets asserted on.
 */
const INTERACTIONS = {
  StorageView: async (container, { flush, auth }) => {
    // StorageView is mounted as a *reader* on purpose, so the page's read-only form
    // is exercised. Its create dialog only exists for a manager, so the code is
    // granted here rather than by weakening that default.
    auth.permissions = new Set([...auth.permissions, 'storage:manage'])
    await flush()

    const button = [...container.querySelectorAll('button')].find((node) =>
      node.textContent?.includes('新建存储'),
    )
    if (!button) throw new Error('no 「新建存储」 button to click after granting storage:manage')
    button.click()
    await flush()

    // The dialog is teleported to the body, not nested inside the view.
    return container.ownerDocument.body.innerHTML
  },
}

/**
 * Text that must appear in the mounted DOM.
 *
 * "It did not throw" is a weak claim: a view whose data never arrives renders a
 * perfectly valid empty state. Requiring the fixture's own text proves the row
 * reached the template and came out the other side.
 */
const EXPECTS = {
  // `/mnt/data` and the attached-instance list live in the detail drawer, which
  // is closed on first paint; the table is what this asserts. The last entry is
  // from the create dialog that `INTERACTIONS.StorageView` opens: with no group
  // owning a workspace — the live state that produced a silent `group_has_no_workspace`
  // — the dialog has to explain the ordering instead of submitting into a refusal.
  StorageView: [
    '我的数据盘',
    'my-data',
    '/storage/kube-wl/nlp/zhangsan',
    '50 GB',
    '24%',
    '已就绪',
    '用户存储卷',
    '张三',
    '还没有用户具备存储条件',
  ],
  // The table carries the reference and the kind chip; the framework chips it
  // used to show are gone with the columns behind them.
  ImagesView: [
    'PyTorch 2.4 CUDA 12',
    'registry.local/ai/pytorch:2.4-cuda12',
    '官方镜像',
    '含 CUDA 12.1 运行时',
    '7.0 GB',
  ],
  InstancesView: ['训练 A', 'train-a', '运行中'],
  TasksView: ['训练第一轮', 'train-run-1', '已完成'],
  // The gallery shows the display name and the introduction; the slug appears
  // only in the detail drawer.
  ApplicationsView: ['Jupyter + PyTorch', '开发环境', '8 核 / 32 GB'],
  UsersView: ['用户管理', 'zhangsan', '张三', 'NLP 实验室', 'zhangsan@example.edu'],
  GroupsView: ['NLP 实验室', 'nlp-lab'],
  // Phase 8. The audit table's own columns, plus the two things that are easy to
  // lose: the raw action code next to its label, and the mark on an
  // unattributed row.
  AuditView: ['审计日志', '创建实例', 'instance:create', 'zhangsan', '成功', '10.0.0.9'],
  // The remaining admin screens are smoke-tested for the first time; before the
  // discovery fix they were never mounted at all. Assertions are kept to text the
  // fixtures actually drive, so a failure here means the screen changed.
  ResourcesView: ['资源与配额'],
  WorkspacesView: ['工作空间管理', 'nlp', '自然语言处理', 'nlp-lab'],
  DashboardView: ['首页', '平台建设进度'],
  // The profile page is where tokens live. Asserted here rather than in a view
  // of its own because the card is a child component, and the interesting thing
  // is that it renders *inside* the page a user already visits.
  ProfileView: ['个人设置', 'API 令牌', 'ci-deploy', 'wl_abcdefgh…', '跟随我的角色'],
  // Driven by grants, not by the workspace record: with none issued the view
  // correctly reports that no pools are available.
  MyWorkspaceView: ['我的工作空间', '算力配额', '可用资源池'],
  // `covered` is true in the fixture, so the "not yet summarised" alert must be
  // absent — asserted below. These are the figures a reader would look for.
  // The fixture is Grafana-enabled, so the panel frame and the scope banner both
  // render. The counts are chosen so none is a substring of another.
  MonitorView: [
    '监控中心',
    '1/2',
    '就绪 / 总数',
    '3/10',
    '运行中实例',
    '监控范围已限定',
    'wl-nlp-alpha',
    '戴娜的开发环境',
    'dev-dana',
    'wl-dev-dana-7-abcde',
    '1 × A100',
    'gpu-node-01',
    '我的容器',
    'A100',
    'V100',
  ],
  // Pools plus the node inventory. `A100` and `gpu-node-01` are the two values
  // that prove the conditional node section actually rendered — a role-gated
  // section that silently disappears is the failure this is here to catch.
  ComputeView: ['计算资源', '资源池', 'A100 资源组', 'gpu-a100', 'A100', '集群节点', 'gpu-node-01'],
  // The warning list is the page. `STORAGE_CLASS_DATA` proves the remedy renders,
  // and the critical one must sort above the warning despite arriving second.
  SystemView: [
    '系统设置',
    '配置体检',
    '生产环境开启了 DEBUG',
    'STORAGE_CLASS_DATA',
    '集成状态',
    'LDAP / FreeIPA 目录',
    '已接入',
    '未接入',
    '运行时',
    '0.8.0',
    '存储类',
    'ceph-rbd',
  ],
  UsageView: [
    '用量中心',
    '255',
    'GPU 卡时',
    '20.0',
    '14.6',
    '5.0 天',
    '17 个负载',
    '其中你自己',
    '25.5 GPU·h',
    '张三',
    '李四',
    '开发实例',
  ],
}

/**
 * Text that must **not** appear, per view.
 *
 * The complement of `EXPECTS`, and the check that does not depend on anybody
 * having written an expectation: a fixture optionally overridden, a date range
 * the user never picked, an alert that belongs to a different branch. The usage
 * page is where this matters most, because its two most important states — "the
 * rollup ran and found nothing" and "the rollup never ran" — differ by a single
 * boolean and look identical otherwise.
 */
const EXPECTS_ABSENT = {
  UsageView: [
    '该时间段内还没有用量汇总数据',
    // ECharts' fallback notice. Its absence is what proves the canvas stub was
    // enough and `setOption` actually ran — the chart itself renders no text, so
    // there is no positive string to assert on.
    '图表无法绘制',
  ],
  // The fixture has records, so the empty branches must not render. Each of these
  // is a plausible-looking screen that means "nothing happened" — the exact
  // opposite of what the data says.
  AuditView: ['还没有审计记录', '没有符合条件的记录', 'No data available'],
  MonitorView: [
    // The fixture has Grafana configured, so neither "unavailable" notice may
    // appear. Both render convincingly and mean the opposite of the truth.
    '未接入 Grafana',
    '没有正在运行的容器',
    '没有登记的加速卡',
    '没有同步到节点',
  ],
  // Mounted as a reader (see `VIEW_PERMISSIONS_ABSENT`). The one row in the
  // fixture is still there — what must be gone is every control that would 403.
  StorageView: ['新建存储', '操作'],
}

/**
 * Extra permission codes granted for one view only.
 *
 * The mount seeds `auth.permissions` from a single baseline, so every screen
 * renders as the same caller. That is fine for layout, but it makes a
 * role-gated branch untestable: `ComputeView` hides its node inventory unless
 * the caller holds `node:read`, and with no way to say "mount this one as a
 * group administrator" the section could break entirely and the check would
 * still pass — which is precisely the silent narrowing this harness exists to
 * catch.
 *
 * Codes here are *added* to the baseline, and the view's `EXPECTS_ABSENT`
 * entries still have to hold, so a grant cannot be used to paper over a branch
 * that should not have rendered.
 */
const VIEW_PERMISSIONS = {
  // The one code `ComputeView` gates on. A group administrator holds it; a plain
  // user does not, and the API refuses the call either way.
  ComputeView: ['node:read'],
}

/**
 * Codes *withheld* for one view — the mirror of `VIEW_PERMISSIONS`.
 *
 * The baseline is an administrator, which is the right default (a view that only
 * ever renders its read-only form is barely tested). But the opposite mistake is
 * just as easy and much harder to see: a screen that shows a plain user controls
 * the API will refuse. Nothing throws, nothing looks broken, and the failure only
 * surfaces when somebody clicks.
 *
 * So a view whose read-only form is a requirement in its own right gets mounted
 * a second time here.
 */
const VIEW_PERMISSIONS_ABSENT = {
  // 「普通用户只需要看到自己的存储卷和组共享存储卷」— every storage mutation needs
  // `storage:manage`, which only a global administrator holds. Mounting as a
  // reader is what proves the page does not advertise the rest.
  StorageView: ['storage:manage'],
}

// ---------------------------------------------------------------------------
// Stubs
//
// One module per real API module, exporting the same names, so the views' own
// imports resolve unchanged. The exports are enumerated from the real files
// rather than hand-written: a missing export is a hard import failure, and a
// hand-written list would rot the first time an endpoint was added.
// ---------------------------------------------------------------------------

const workDir = mkdtempSync(join(tmpdir(), 'wl-views-'))
const stubDir = join(workDir, 'stubs')
const outDir = join(workDir, 'out')
mkdirSync(stubDir, { recursive: true })

/**
 * Names a module exports, which of them are types, and — for functions — whether
 * the declared return type is an array.
 *
 * The array flag matters more than it looks. `listWorkspaceGrants` returns
 * `WorkspaceResourceGrant[]` while `listWorkspaces` returns `Page<...>`, and a
 * stub that hands one the other's shape makes the view throw on `.map` or render
 * an empty state. Deriving it from the signature means a new endpoint cannot
 * silently get the wrong default.
 */
function exportsOf(source) {
  const names = new Map()
  const patterns = [
    [/^export\s+const\s+([A-Za-z0-9_$]+)/gm, 'value', 1],
    [/^export\s+class\s+([A-Za-z0-9_$]+)/gm, 'value', 1],
    [/^export\s+(?:type|interface)\s+([A-Za-z0-9_$]+)/gm, 'type', 1],
  ]
  for (const [pattern, kind, group] of patterns) {
    for (const match of source.matchAll(pattern)) names.set(match[group], kind)
  }

  const shapes = new Map()
  for (const match of source.matchAll(
    /^export\s+(?:async\s+)?function\s+([A-Za-z0-9_$]+)\s*\([\s\S]*?\)\s*:\s*Promise<([^>]+)>/gm,
  )) {
    names.set(match[1], 'value')
    shapes.set(match[1], match[2].trim().endsWith('[]') ? 'array' : 'page')
  }

  // A function without a `: Promise<...>` annotation (rare, but possible) still
  // has to be exported as a value.
  for (const match of source.matchAll(/^export\s+(?:async\s+)?function\s+([A-Za-z0-9_$]+)/gm)) {
    if (!names.has(match[1])) names.set(match[1], 'value')
  }

  return { names, shapes }
}

const apiDir = join(SRC, 'api')
const aliases = []
const SHAPES = {}

for (const file of readdirSync(apiDir).filter((name) => name.endsWith('.ts'))) {
  // `client.ts` is left alone: the views import its error class and the stores
  // import its token setters, and both must keep working for real.
  if (file === 'client.ts') continue

  const name = basename(file, extname(file))
  const source = readFileSync(join(apiDir, file), 'utf8')
  const { names, shapes } = exportsOf(source)
  const lines = []
  for (const [exportName, kind] of names) {
    if (kind === 'type') {
      lines.push(`export const ${exportName} = undefined`)
      continue
    }
    if (shapes.has(exportName)) SHAPES[exportName] = shapes.get(exportName)
    lines.push(
      `export const ${exportName} = (...args: unknown[]) => stub(${JSON.stringify(exportName)}, ...args)`,
    )
  }

  const stubPath = join(stubDir, `${name}.ts`)
  writeFileSync(
    stubPath,
    `import { stub } from ${JSON.stringify(join(stubDir, 'runtime.ts'))}\n${lines.join('\n')}\n`,
  )
  aliases.push({ find: new RegExp(`^@/api/${name}$`), replacement: stubPath })
}

writeFileSync(
  join(stubDir, 'runtime.ts'),
  [
    `const RETURNS: Record<string, unknown> = ${JSON.stringify(RETURNS)}`,
    `const DEFAULT_RETURN: unknown = ${JSON.stringify(DEFAULT_RETURN)}`,
    `const SHAPES: Record<string, 'array' | 'page'> = ${JSON.stringify(SHAPES)}`,
    'export function stub(name: string, ..._args: unknown[]): Promise<unknown> {',
    '  if (Object.prototype.hasOwnProperty.call(RETURNS, name)) return Promise.resolve(RETURNS[name])',
    '  return Promise.resolve(SHAPES[name] === \'array\' ? [] : DEFAULT_RETURN)',
    '}',
    '',
  ].join('\n'),
)

// ---------------------------------------------------------------------------
// Build
//
// The views must be built for the browser. `ssrLoadModule` returns them compiled
// for the server, where Vuetify's client components fail on a missing SSR
// context — which is why this goes through a real `vite build` instead.
// ---------------------------------------------------------------------------

const wanted = process.argv.slice(2)
/**
 * Every `*View.vue` under `src/views`, at any depth.
 *
 * Recursive on purpose. A flat `readdirSync` silently skipped `views/admin/` —
 * five screens, including the audit trail — while still reporting a confident
 * "13/13 views mounted". A check that quietly narrows its own scope is worse
 * than no check: it reports the same success either way, so nobody looks.
 */
function discoverViews(dir) {
  const found = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) found.push(...discoverViews(full))
    else if (entry.name.endsWith('View.vue')) found.push(full)
  }
  return found
}

const viewFiles = discoverViews(VIEWS)
  .filter((file) => !wanted.length || wanted.some((w) => file.toLowerCase().includes(w.toLowerCase())))
  .sort()

if (!viewFiles.length) {
  console.error(`No views matched ${JSON.stringify(wanted)}`)
  process.exit(2)
}

// The entry lives inside the app, not in the temp directory: bare specifiers
// like `pinia` are resolved relative to the importing file, and `/tmp` has no
// `node_modules` to resolve them from.
const entryPath = join(FRONTEND, '.wl-views-entry.ts')
writeFileSync(
  entryPath,
  [
    // Everything the harness needs comes from the same bundle as the views. Two
    // copies of Vue in one process fail with "app instance not found" from
    // inside Vuetify, which names nothing that helps.
    "export { createApp, h } from 'vue'",
    "export { VApp } from 'vuetify/components'",
    "export { createPinia, setActivePinia } from 'pinia'",
    "export { createVuetify } from 'vuetify'",
    "export { createRouter, createMemoryHistory } from 'vue-router'",
    "export { useAuthStore } from '@/stores/auth'",
    ...viewFiles.map(
      (file) => `export { default as ${basename(file, '.vue')} } from './${relative(FRONTEND, file)}'`,
    ),
    '',
  ].join('\n'),
)

// The project's plugin set injects one `import '.../VBtn.css'` per Vuetify
// component, and a library build externalises raw CSS, which Node cannot load.
// Styles are irrelevant to a behavioural check, so they are switched off.
// Both plugins are imported by absolute path: this script lives outside the
// frontend, so a bare specifier would not resolve from here.
const vuePlugin = (
  await import(pathToFileURL(join(FRONTEND, 'node_modules/@vitejs/plugin-vue/dist/index.mjs')).href)
).default
const vuetifyPlugin = (
  await import(
    pathToFileURL(join(FRONTEND, 'node_modules/vite-plugin-vuetify/dist/index.mjs')).href
  )
).default

await build({
  root: FRONTEND,
  configFile: false,
  logLevel: 'error',
  plugins: [vuePlugin(), vuetifyPlugin({ autoImport: true, styles: false })],
  resolve: { alias: [...aliases, { find: /^@\//, replacement: `${SRC}/` }] },
  define: {
    __VUE_OPTIONS_API__: 'true',
    __VUE_PROD_DEVTOOLS__: 'false',
    __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false',
  },
  build: {
    outDir,
    emptyOutDir: true,
    target: 'es2022',
    minify: false,
    sourcemap: false,
    cssCodeSplit: false,
    // A single file, so there is exactly one copy of Vue in the process. The
    // library build already emits one for a single format; splitting is switched
    // off explicitly rather than via the deprecated `inlineDynamicImports`.
    lib: { entry: entryPath, formats: ['es'], fileName: () => 'views.mjs' },
  },
})

const bundleUrl = pathToFileURL(join(outDir, 'views.mjs')).href

// ---------------------------------------------------------------------------
// Mount
// ---------------------------------------------------------------------------

const failures = []
const results = []

try {
  const { Window } = await import(
    pathToFileURL(join(FRONTEND, 'node_modules/happy-dom/lib/index.js')).href
  )

  // The DOM must exist before the bundle is imported: Vuetify touches `document`
  // while its modules are evaluating, not only when mounted.
  const window = new Window({ url: 'https://wl.example.edu/' })

  /**
   * Give the canvas element a 2D context.
   *
   * happy-dom returns `null` from `getContext('2d')`, which makes ECharts throw
   * on `init` — and a chart that throws takes the whole view down with it, so the
   * usage page could not be mounted at all. The stub is deliberately thin: it
   * exists so that `setOption` *runs*, which is what catches a malformed option
   * (a missing series, a category axis fed numbers). It proves nothing about
   * pixels, and it is not meant to.
   */
  window.HTMLCanvasElement.prototype.getContext = function getContext() {
    const target = {
      canvas: this,
      measureText: (text) => ({ width: String(text).length * 7, height: 12 }),
      createLinearGradient: () => ({ addColorStop() {} }),
      createRadialGradient: () => ({ addColorStop() {} }),
      createPattern: () => ({}),
      getImageData: () => ({ data: new Uint8ClampedArray(4) }),
      createImageData: () => ({ data: new Uint8ClampedArray(4) }),
      setLineDash() {},
      getLineDash: () => [],
      isPointInPath: () => false,
      save() {},
      restore() {},
    }
    return new Proxy(target, {
      get: (object, key) => (key in object ? object[key] : () => undefined),
      set: (object, key, value) => {
        object[key] = value
        return true
      },
    })
  }

  /**
   * Give the window a `visualViewport`.
   *
   * happy-dom does not implement it, and Vuetify's overlay teardown reads it while
   * a dialog is being unmounted — so a view whose dialog was *opened* by an
   * interaction fails during `app.unmount()` with `visualViewport is not defined`,
   * after every assertion has already passed. That reads like a view bug and is
   * not one. The stub only has to be an event target with viewport-shaped numbers;
   * nothing here is measured.
   */
  if (!window.visualViewport) {
    const viewport = new window.EventTarget()
    Object.assign(viewport, {
      width: 1280,
      height: 800,
      offsetLeft: 0,
      offsetTop: 0,
      pageLeft: 0,
      pageTop: 0,
      scale: 1,
    })
    window.visualViewport = viewport
  }

  /**
   * Populate the global scope from the DOM window.
   *
   * The constructors are enumerated rather than listed by hand, because Vuetify
   * and the views reach for a long tail of browser classes (`CSS`, `ShadowRoot`,
   * `ResizeObserver`, …) and a hand-written list fails one at a time, each
   * failure surfacing as a Vue error inside whichever component touched it.
   *
   * Only *constructors* are copied wholesale. Copying every window property
   * would also install `self`, `top`, `parent` and `frames` — each of which
   * happy-dom resolves back to the window — and those interfere with the module
   * graph in ways that show up much later and look nothing like the cause.
   */
  const DOM_GLOBALS = [
    'window',
    'document',
    'navigator',
    'location',
    'localStorage',
    'performance',
    'getComputedStyle',
    'matchMedia',
    'requestAnimationFrame',
    'cancelAnimationFrame',
    'visualViewport',
  ]

  for (const name of Object.getOwnPropertyNames(window)) {
    if (!/^[A-Z]/.test(name)) continue
    if (name in globalThis) continue
    try {
      Object.defineProperty(globalThis, name, {
        value: window[name],
        writable: true,
        configurable: true,
      })
    } catch {
      // A locked-down name is not worth aborting the whole run over.
    }
  }

  for (const name of DOM_GLOBALS) {
    let value
    try {
      value = window[name]
    } catch {
      continue
    }
    if (value === undefined) continue
    if (typeof value === 'function') value = value.bind(window)
    // `defineProperty`, not assignment: Node exposes some of these on
    // `globalThis` as getter-only accessors, and a plain assignment throws
    // before any view is reached.
    Object.defineProperty(globalThis, name, { value, writable: true, configurable: true })
  }

  const bundle = await import(bundleUrl)
  const { createApp, h, VApp, createPinia, setActivePinia, createVuetify, createRouter, createMemoryHistory } =
    bundle

  for (const file of viewFiles) {
    const label = basename(file, '.vue')
    const component = bundle[label]
    let html = ''
    try {
      if (!component) throw new Error(`the bundle did not export ${label}`)

      const container = window.document.createElement('div')
      window.document.body.appendChild(container)

      // `v-app`, because the views' detail drawers (`v-navigation-drawer`) read
      // a layout that the real shell in `App.vue` provides. Without it they throw
      // an injection error that has nothing to do with the view's own logic.
      const app = createApp({
        render: () => h(VApp, null, { default: () => h(component) }),
      })
      const pinia = createPinia()
      app.use(pinia)
      setActivePinia(pinia)
      app.use(createVuetify())
      // Views link out with `to="..."`, which needs a router to resolve.
      app.use(
        createRouter({
          history: createMemoryHistory(),
          routes: [{ path: '/:pathMatch(.*)*', component: { render: () => null } }],
        }),
      )

      // Seed the store so `can()` answers true and the admin-only branches
      // render. A view that only ever renders its read-only form is barely
      // tested — except where `VIEW_PERMISSIONS_ABSENT` deliberately withholds a
      // code, so that the read-only form is tested too.
      const auth = bundle.useAuthStore()
      auth.user = USER
      auth.permissions = new Set(
        [
          ...RETURNS.fetchPermissions.permissions,
          ...(VIEW_PERMISSIONS[label] ?? []),
        ].filter((code) => !(VIEW_PERMISSIONS_ABSENT[label] ?? []).includes(code)),
      )
      auth.scopeKind = 'group'
      auth.scopeGroupIds = [1]
      auth.scopeWorkspaceIds = [1]
      auth.status = 'authenticated'

      app.mount(container)

      // Let the stubbed promises resolve and Vue flush. Several turns, because a
      // view may chain a second `load()` off a lookup it just received.
      const flush = async () => {
        for (let turn = 0; turn < 30; turn += 1) {
          await new Promise((resolve) => setTimeout(resolve, 0))
        }
      }
      await flush()

      html = container.innerHTML
      if (!html || html.length < 50) throw new Error(`rendered only ${html.length} bytes`)

      // What the page showed before anything was clicked. Kept separately because
      // `EXPECTS_ABSENT` asserts on the *reader's* view of the page — a view that
      // only reveals mutations to an administrator is the point of mounting it
      // without `storage:manage` — and an interaction may deliberately grant a code
      // and open the very controls that check forbids.
      const firstPaint = html

      // Branches behind a click are asserted on too, where a view declares one.
      const interact = INTERACTIONS[label]
      if (interact) {
        const after = await interact(container, { flush, auth })
        if (typeof after !== 'string' || after.length < 50) {
          throw new Error(`the interaction rendered only ${String(after).length} bytes`)
        }
        html += after
      }

      // Attribute values and inline styles are not user-visible, so the leak
      // check runs against the text the page would actually show.
      const text = html
        .replace(/<style[\s\S]*?<\/style>/g, ' ')
        .replace(/<[^>]+>/g, ' ')
        .replace(/&nbsp;/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()

      const leaked = NEVER_RENDER.filter((needle) => text.includes(needle))
      if (leaked.length) {
        const around = leaked.map((needle) => {
          const at = text.indexOf(needle)
          return `${JSON.stringify(needle)} in …${text.slice(Math.max(0, at - 60), at + 30)}…`
        })
        throw new Error(`rendered placeholder values: ${around.join('; ')}`)
      }

      const absent = (EXPECTS[label] ?? []).filter((needle) => !html.includes(needle))
      if (absent.length) {
        throw new Error(`rendered, but without: ${absent.map((a) => JSON.stringify(a)).join(', ')}`)
      }

      const unexpected = (EXPECTS_ABSENT[label] ?? []).filter((needle) =>
        firstPaint.includes(needle),
      )
      if (unexpected.length) {
        throw new Error(
          `rendered something that should not be there: ${unexpected
            .map((n) => JSON.stringify(n))
            .join(', ')}`,
        )
      }

      app.unmount()
      container.remove()
      results.push([label, html.length])
    } catch (error) {
      failures.push([label, error, html])
    }
  }
} finally {
  rmSync(workDir, { recursive: true, force: true })
  rmSync(entryPath, { force: true })
}

for (const [label, size] of results) console.log(`  \u2713 ${label} (${size} bytes)`)
for (const [label, error, html] of failures) {
  console.error(`\n  \u2717 ${label}`)
  const detail = error?.stack ? error.stack.split('\n').slice(0, 7).join('\n    ') : String(error)
  console.error(`    ${detail}`)

  // Print what *did* render. A missing-content failure is nearly always a
  // fixture that does not match what the view expects, and the fastest way to
  // see that is to read the text the view actually produced.
  if (html) {
    const text = html
      .replace(/<style[\s\S]*?<\/style>/g, ' ')
      .replace(/<[^>]+>/g, ' ')
      .replace(/&nbsp;/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()
    console.error(`    rendered: ${text.slice(0, 600)}`)
  }
}

console.log(
  `\n${results.length}/${results.length + failures.length} views mounted and rendered their data`,
)
process.exit(failures.length ? 1 : 0)
