<template>
  <v-chip :color="tone" :size="size" variant="flat" label class="wl-status-chip">
    <v-icon v-if="icon" start :icon="icon" size="small" />
    {{ text }}
  </v-chip>
</template>

<script setup lang="ts">
import { computed } from 'vue'

type Tone = 'success' | 'warning' | 'error' | 'info' | 'neutral'

interface StatusStyle {
  label: string
  tone: Tone
  icon?: string
}

/**
 * Status vocabulary shared by instances, tasks, applications, nodes and
 * workspaces. Keep in sync with the backend status enums.
 */
const STATUS_MAP: Record<string, StatusStyle> = {
  // Workload lifecycle
  pending: { label: '等待中', tone: 'warning', icon: 'mdi-clock-outline' },
  queued: { label: '排队中', tone: 'warning', icon: 'mdi-tray-full' },
  creating: { label: '创建中', tone: 'info', icon: 'mdi-progress-clock' },
  running: { label: '运行中', tone: 'success', icon: 'mdi-play-circle-outline' },
  stopping: { label: '停止中', tone: 'info', icon: 'mdi-progress-clock' },
  stopped: { label: '已停止', tone: 'neutral', icon: 'mdi-stop-circle-outline' },
  succeeded: { label: '已完成', tone: 'success', icon: 'mdi-check-circle-outline' },
  failed: { label: '失败', tone: 'error', icon: 'mdi-alert-circle-outline' },
  cancelling: { label: '取消中', tone: 'info', icon: 'mdi-progress-clock' },
  cancelled: { label: '已取消', tone: 'neutral', icon: 'mdi-cancel' },
  deleting: { label: '删除中', tone: 'warning', icon: 'mdi-delete-clock-outline' },
  error: { label: '异常', tone: 'error', icon: 'mdi-alert-outline' },

  // Workspace lifecycle
  provisioning: { label: '下发中', tone: 'info', icon: 'mdi-progress-clock' },
  suspended: { label: '已停用', tone: 'neutral', icon: 'mdi-pause-circle-outline' },

  // Storage lifecycle
  // `pending` and `deleting` are already covered above; `bound` is the healthy
  // resting state of a volume and has no equivalent in the workload enum.
  bound: { label: '已就绪', tone: 'success', icon: 'mdi-harddisk' },
  resizing: { label: '扩容中', tone: 'info', icon: 'mdi-arrow-expand-horizontal' },
  released: { label: '已释放', tone: 'neutral', icon: 'mdi-archive-outline' },

  // Task lifecycle. A `timeout` is its own outcome, not a failure of the
  // command: the algorithm was still running when its budget ran out.
  timeout: { label: '超时', tone: 'warning', icon: 'mdi-timer-off-outline' },

  // Node health
  ready: { label: '就绪', tone: 'success', icon: 'mdi-check-circle-outline' },
  not_ready: { label: '未就绪', tone: 'error', icon: 'mdi-alert-circle-outline' },
  cordoned: { label: '已封锁', tone: 'warning', icon: 'mdi-cancel' },
  unknown: { label: '未知', tone: 'neutral', icon: 'mdi-help-circle-outline' },

  // Accelerator state
  available: { label: '空闲', tone: 'success' },
  allocated: { label: '已分配', tone: 'info' },
  unhealthy: { label: '异常', tone: 'error' },

  // Entity states
  active: { label: '正常', tone: 'success' },
  inactive: { label: '停用', tone: 'neutral' },
  enabled: { label: '启用', tone: 'success' },
  disabled: { label: '禁用', tone: 'neutral' },
  schedulable: { label: '可调度', tone: 'success' },
  unschedulable: { label: '不可调度', tone: 'error' },
}

const props = withDefaults(
  defineProps<{
    status: string
    /** Override the displayed text (useful for i18n or counts). */
    label?: string
    size?: 'x-small' | 'small' | 'default' | 'large'
  }>(),
  { size: 'small' },
)

const resolved = computed<StatusStyle>(() => {
  const key = (props.status ?? '').toLowerCase()
  return (
    STATUS_MAP[key] ?? {
      label: props.status || '未知',
      tone: 'neutral',
    }
  )
})

const text = computed(() => props.label ?? resolved.value.label)
const tone = computed(() => resolved.value.tone)
const icon = computed(() => resolved.value.icon)
</script>

<style scoped lang="scss">
.wl-status-chip {
  font-weight: 500;
  letter-spacing: 0;
}
</style>
