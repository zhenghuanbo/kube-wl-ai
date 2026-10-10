/**
 * Custom task wire types.
 *
 * Mirrors `backend/app/schemas/task.py`. A task is one Kubernetes Job: it runs
 * a shell command and exits. That is why there is no IDE, no SSH and no
 * start/stop here — only submit, watch, cancel and delete.
 */

export type TaskStatus =
  | 'pending'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'timeout'
  | 'cancelling'
  | 'cancelled'
  | 'error'

/** Statuses after which nothing can change; the row becomes read-only. */
export const TERMINAL_TASK_STATUSES: readonly TaskStatus[] = [
  'succeeded',
  'failed',
  'timeout',
  'cancelled',
  'error',
]

export function isTerminal(status: TaskStatus): boolean {
  return TERMINAL_TASK_STATUSES.includes(status)
}

export interface TaskSummary {
  id: number
  name: string
  displayName: string
  description?: string | null
  workspaceId: number
  workspaceName?: string | null
  ownerUserId: number
  ownerUsername?: string | null
  ownerDisplayName?: string | null
  resourceGroupId?: number | null
  resourceGroupName?: string | null
  image: string
  cpuMilli: number
  memoryMb: number
  gpuType: string
  gpuCount: number
  status: TaskStatus
  statusMessage?: string | null
  priority: number
  maxRuntimeSeconds?: number | null
  nodeName?: string | null
  exitCode?: number | null
  durationSeconds?: number | null
  startedAt?: string | null
  finishedAt?: string | null
  isActive: boolean
  createdAt: string
  updatedAt: string
}

export interface TaskDetail extends TaskSummary {
  script: string
  workingDir?: string | null
  env: Record<string, string>
  k8sJobName?: string | null
  podName?: string | null
  ttlSecondsAfterFinished: number
}

export interface TaskCreatePayload {
  name: string
  displayName?: string
  description?: string | null
  workspaceId?: number | null
  ownerUserId?: number | null
  resourceGroupId: number
  resourceSpecId: number
  image: string
  /** A shell script, run as `/bin/sh -lc <script>` inside the container. */
  script: string
  workingDir?: string | null
  env?: Record<string, string>
  /**
   * Extra volumes to mount, beyond the owner's home volume.
   *
   * The home volume is not chosen here: it is always mounted at `/home/{username}`,
   * because a task without it would have no home at all. Nothing listed here is
   * mounted by default — a task that silently saw a colleague's shared directory
   * would be a surprise rather than a convenience.
   */
  storageIds?: number[]
  priority?: number
  maxRuntimeSeconds?: number | null
  ttlSecondsAfterFinished?: number
}

/** Only the fields that can honestly change after submission. */
export interface TaskUpdatePayload {
  displayName?: string
  description?: string | null
  priority?: number
}

export interface TaskLogs {
  taskId: number
  podName?: string | null
  logs: string
  previous: boolean
}

/**
 * Ready-made scripts, so the first task does not require reading the docs.
 *
 * `value` is inserted verbatim into the editor, which is why each one is a
 * complete, runnable snippet rather than a fragment.
 */
export const SCRIPT_PRESETS: ReadonlyArray<{
  title: string
  icon: string
  description: string
  value: string
}> = [
  {
    title: '环境自检',
    icon: 'mdi-clipboard-check-outline',
    description: '打印 Python、CUDA 与可见加速卡，用来确认镜像是否可用。',
    value: `set -e
python -c "import sys; print(sys.version)"
python -c "import torch; print('torch', torch.__version__, 'cuda', torch.cuda.is_available())" || true
nvidia-smi || echo "no GPU visible"
echo "task \${WL_TASK_ID} in \${WL_WORKSPACE} done"`,
  },
  {
    title: '训练脚本',
    icon: 'mdi-brain',
    description: '从一个挂在 /data 的目录里读取数据并训练。',
    value: `set -e
python train.py \\
  --data-dir /data/dataset \\
  --output-dir /data/runs/\${WL_TASK_ID} \\
  --epochs 10`,
  },
  {
    title: '数据处理',
    icon: 'mdi-database-cog-outline',
    description: '批量转换 /data 下的文件，失败即中止。',
    value: `set -euo pipefail
for file in /data/raw/*.csv; do
  echo "processing $file"
  python convert.py "$file" -o "/data/clean/$(basename "$file" .csv).parquet"
done`,
  },
]
