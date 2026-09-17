<script setup lang="ts">
/**
 * System information and configuration health check.
 *
 * The page is ordered by what an administrator needs, not by what is easy to
 * render: **warnings first**, then the integration status, then the numbers, then
 * the raw settings. Somebody opens this page because something is not working, so
 * the answer to "what is wrong" is the first thing on screen rather than
 * something to be found by reading a table of values.
 *
 * Everything here is read-only, and that is a deliberate limit rather than an
 * unfinished feature. Configuration arrives through environment variables and is
 * read once at startup; a form that edited them would either be lying about the
 * running process or would need a restart trigger this service does not have. The
 * chart and the ConfigMap are the interface, and this page tells you what to put
 * in them — which is why each warning names the setting that fixes it.
 */
import { computed, onMounted, ref } from 'vue'

import { getSystemInfo } from '@/api/system'
import { ApiError } from '@/api/client'
import PageHeader from '@/components/common/PageHeader.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { useNotificationStore } from '@/stores/notifications'
import type { SystemInfo, SystemWarning } from '@/types/system'
import { bySeverity, severityColor, severityLabel } from '@/types/system'

const notifications = useNotificationStore()

const info = ref<SystemInfo | null>(null)
const loading = ref(false)

async function load(): Promise<void> {
  loading.value = true
  try {
    info.value = await getSystemInfo()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    loading.value = false
  }
}

onMounted(load)

const warnings = computed<SystemWarning[]>(() => bySeverity(info.value?.warnings ?? []))

const criticalCount = computed(
  () => warnings.value.filter((warning) => warning.severity === 'critical').length,
)

/** The counts, in the order they are worth reading. */
const metrics = computed(() => {
  const counts = info.value?.counts
  if (!counts) return []
  return [
    { label: '用户', value: `${counts.activeUsers} / ${counts.users}`, hint: '启用 / 总数' },
    { label: '用户组', value: String(counts.groups) },
    { label: '工作空间', value: String(counts.workspaces), hint: '各对应一个命名空间' },
    { label: '计算节点', value: String(counts.nodes) },
    { label: '加速卡', value: String(counts.gpus) },
    {
      label: '实例',
      value: `${counts.runningInstances} / ${counts.instances}`,
      hint: '运行中 / 总数',
    },
  ]
})

const rateLimitDetail = computed(() => {
  const value = info.value
  if (!value) return ''
  if (!value.loginRateLimitEnabled) return '已关闭'
  const minutes = Math.round(value.loginRateLimitWindowSeconds / 60)
  return `${minutes} 分钟内，单账号 ${value.loginRateLimitAttemptsPerUsername} 次、单 IP ${value.loginRateLimitAttemptsPerIp} 次`
})

/** Storage classes, with an unset one rendered as a problem rather than a blank. */
const storageRows = computed(() => {
  const classes = info.value?.storageClasses ?? {}
  const labels: Record<string, string> = { home: '个人主目录', data: '个人数据盘', share: '组公共目录' }
  return Object.entries(classes).map(([role, name]) => ({
    role,
    title: labels[role] ?? role,
    name,
  }))
})
</script>

<template>
  <div>
    <PageHeader title="系统设置" subtitle="运行时配置、集成状态与配置体检（只读）">
      <template #actions>
        <v-btn variant="text" prepend-icon="mdi-refresh" :loading="loading" @click="load">
          刷新
        </v-btn>
      </template>
    </PageHeader>

    <v-skeleton-loader v-if="loading && !info" type="card, card, card" />

    <template v-else-if="info">
      <!--
        Warnings first. Somebody is on this page because something is broken, and
        making them scroll past a table of healthy values to find the problem is
        the wrong order.
      -->
      <v-card variant="flat" class="wl-card mb-6">
        <v-card-title class="d-flex align-center">
          <v-icon
            :icon="criticalCount ? 'mdi-alert-octagon' : 'mdi-check-circle-outline'"
            :color="criticalCount ? 'error' : 'success'"
            class="me-2"
          />
          配置体检
          <v-chip v-if="warnings.length" size="small" variant="tonal" class="ms-3" label>
            {{ warnings.length }} 项
          </v-chip>
        </v-card-title>

        <v-card-text>
          <v-alert
            v-if="!warnings.length"
            type="success"
            variant="tonal"
            density="comfortable"
            icon="mdi-check-circle-outline"
          >
            未发现已知的配置问题。这不是「一切正常」的保证，只是本页覆盖的检查全部通过。
          </v-alert>

          <v-list v-else lines="three" class="pa-0">
            <template v-for="(warning, index) in warnings" :key="warning.code">
              <v-divider v-if="index" />
              <v-list-item class="px-0">
                <template #prepend>
                  <v-chip
                    size="small"
                    variant="tonal"
                    label
                    :color="severityColor(warning.severity)"
                    class="me-2"
                  >
                    {{ severityLabel(warning.severity) }}
                  </v-chip>
                </template>
                <v-list-item-title class="font-weight-medium">
                  {{ warning.title }}
                </v-list-item-title>
                <v-list-item-subtitle class="wl-wrap">
                  {{ warning.detail }}
                </v-list-item-subtitle>
                <div class="mt-1 text-caption">
                  <span class="text-medium-emphasis">修复：</span>
                  <code class="wl-mono">{{ warning.remedy }}</code>
                </div>
              </v-list-item>
            </template>
          </v-list>
        </v-card-text>
      </v-card>

      <!-- Integration status -->
      <v-card variant="flat" class="wl-card mb-6">
        <v-card-title>集成状态</v-card-title>
        <v-card-subtitle>
          每个未接入的项都不会让平台不可用，但会让对应的功能缺失或被静默跳过。
        </v-card-subtitle>
        <v-card-text>
          <v-table density="comfortable">
            <thead>
              <tr>
                <th>组件</th>
                <th style="width: 110px">状态</th>
                <th>说明</th>
                <th>配置项</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="feature in info.features" :key="feature.key">
                <td class="font-weight-medium">{{ feature.title }}</td>
                <td>
                  <v-chip
                    size="small"
                    variant="tonal"
                    label
                    :color="feature.enabled ? 'success' : 'default'"
                  >
                    {{ feature.enabled ? '已接入' : '未接入' }}
                  </v-chip>
                </td>
                <td class="text-body-2">{{ feature.detail }}</td>
                <td><code class="wl-mono text-caption">{{ feature.configKey }}</code></td>
              </tr>
            </tbody>
          </v-table>
        </v-card-text>
      </v-card>

      <v-row>
        <!-- Counts -->
        <v-col cols="12" lg="7">
          <v-card variant="flat" class="wl-card mb-6 h-100">
            <v-card-title>数据概览</v-card-title>
            <v-card-text>
              <v-row dense>
                <v-col v-for="metric in metrics" :key="metric.label" cols="6" sm="4">
                  <div class="wl-metric">
                    <div class="text-caption text-medium-emphasis">{{ metric.label }}</div>
                    <div class="wl-metric-value">{{ metric.value }}</div>
                    <div v-if="metric.hint" class="text-caption text-medium-emphasis">
                      {{ metric.hint }}
                    </div>
                  </div>
                </v-col>
              </v-row>
            </v-card-text>
          </v-card>
        </v-col>

        <!-- Runtime -->
        <v-col cols="12" lg="5">
          <v-card variant="flat" class="wl-card mb-6 h-100">
            <v-card-title>运行时</v-card-title>
            <v-card-text>
              <v-table density="comfortable">
                <tbody>
                  <tr>
                    <td class="text-medium-emphasis">版本</td>
                    <td><code class="wl-mono">{{ info.appVersion }}</code></td>
                  </tr>
                  <tr>
                    <td class="text-medium-emphasis">环境</td>
                    <td>
                      <code class="wl-mono">{{ info.environment }}</code>
                      <v-chip
                        v-if="info.debug"
                        size="x-small"
                        color="warning"
                        variant="tonal"
                        label
                        class="ms-2"
                      >
                        调试开启
                      </v-chip>
                    </td>
                  </tr>
                  <tr>
                    <td class="text-medium-emphasis">认证方式</td>
                    <td>
                      <code class="wl-mono">{{ info.authProviders.join(' → ') || '（无）' }}</code>
                    </td>
                  </tr>
                  <tr>
                    <td class="text-medium-emphasis">访问令牌</td>
                    <td>{{ info.accessTokenMinutes }} 分钟</td>
                  </tr>
                  <tr>
                    <td class="text-medium-emphasis">刷新令牌</td>
                    <td>{{ info.refreshTokenDays }} 天</td>
                  </tr>
                  <tr>
                    <td class="text-medium-emphasis">登录限流</td>
                    <td>
                      <span v-if="!info.loginRateLimitEnabled" class="text-error">已关闭</span>
                      <span v-else class="text-body-2">{{ rateLimitDetail }}</span>
                    </td>
                  </tr>
                  <tr>
                    <td class="text-medium-emphasis">SSH 接入</td>
                    <td>
                      <code class="wl-mono">{{ info.sshMode }}</code>
                      <span v-if="info.sshHost" class="ms-2 text-body-2">{{ info.sshHost }}</span>
                      <span v-else class="ms-2 text-warning text-body-2">未配置对外地址</span>
                    </td>
                  </tr>
                </tbody>
              </v-table>
            </v-card-text>
          </v-card>
        </v-col>
      </v-row>

      <!-- Storage classes -->
      <v-card variant="flat" class="wl-card">
        <v-card-title>存储类</v-card-title>
        <v-card-subtitle>
          留空会在用户「创建数据盘」时才失败，而那时的报错来自集群，看不出是配置缺失。
        </v-card-subtitle>
        <v-card-text>
          <v-table density="comfortable">
            <thead>
              <tr>
                <th>用途</th>
                <th>StorageClass</th>
                <th>配置项</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in storageRows" :key="row.role">
                <td>{{ row.title }}</td>
                <td>
                  <code v-if="row.name" class="wl-mono">{{ row.name }}</code>
                  <v-chip v-else size="small" color="warning" variant="tonal" label>未配置</v-chip>
                </td>
                <td>
                  <code class="wl-mono text-caption">
                    STORAGE_CLASS_{{ row.role.toUpperCase() }}
                  </code>
                </td>
              </tr>
            </tbody>
          </v-table>
        </v-card-text>
      </v-card>
    </template>

    <EmptyState
      v-else
      icon="mdi-cog-off-outline"
      title="无法读取系统信息"
      description="请确认当前账号是全局管理员，然后重试。"
    />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-card {
  box-shadow: $card-shadow;
}

.wl-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
}

.wl-metric {
  padding: 8px 0;
}

.wl-metric-value {
  font-size: 1.5rem;
  font-weight: 600;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
}

// Warning details are prose and must wrap; Vuetify's list subtitle truncates by
// default, which would hide the sentence that explains the problem.
.wl-wrap {
  white-space: normal;
  overflow: visible;
  text-overflow: clip;
}
</style>
