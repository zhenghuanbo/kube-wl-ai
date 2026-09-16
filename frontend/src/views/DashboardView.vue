<template>
  <div>
    <PageHeader title="首页" :subtitle="subtitle" />

    <v-row dense>
      <v-col v-for="stat in stats" :key="stat.label" cols="12" sm="6" lg="3">
        <v-card class="wl-stat">
          <div class="wl-stat__body">
            <v-avatar :color="stat.color" size="48" rounded="lg" variant="tonal">
              <v-icon :icon="stat.icon" />
            </v-avatar>

            <div class="wl-stat__text">
              <p class="wl-stat__label">{{ stat.label }}</p>
              <p class="wl-stat__value">{{ stat.value }}</p>
            </div>
          </div>
        </v-card>
      </v-col>
    </v-row>

    <v-card class="mt-6">
      <v-card-title>平台建设进度</v-card-title>
      <v-card-text>
        <EmptyState
          title="业务模块正在按计划交付"
          description="当前已完成工程基础与设计系统。开发实例、应用程序、自定义任务、镜像、存储、用量与监控等模块将按 Phase 1–8 逐步接入真实数据。"
          icon="mdi-hammer-wrench"
        />
      </v-card-text>
    </v-card>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import EmptyState from '@/components/common/EmptyState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useAuthStore } from '@/stores/auth'
import { ROLE_LABELS } from '@/types/auth'

const auth = useAuthStore()

const subtitle = computed(() => {
  const parts: string[] = []
  if (auth.user) parts.push(`当前用户：${auth.user.fullName || auth.user.username}`)
  parts.push(`角色：${auth.roles.map((role) => ROLE_LABELS[role]).join(' / ') || '—'}`)
  parts.push(`工作空间：${auth.user?.workspace?.name ?? '未分配'}`)
  return parts.join('　·　')
})

/** Values are wired to real endpoints in Phase 6 (usage) and Phase 3 (workloads). */
const stats = [
  { label: '我的开发实例', value: '—', icon: 'mdi-cube-outline', color: 'primary' },
  { label: '运行中任务', value: '—', icon: 'mdi-script-text-outline', color: 'info' },
  { label: 'GPU 占用', value: '—', icon: 'mdi-expansion-card', color: 'success' },
  { label: '本月卡时 (GPU·h)', value: '—', icon: 'mdi-chart-timeline-variant', color: 'warning' },
]
</script>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-stat__body {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 20px;
}

.wl-stat__text {
  min-width: 0;
}

.wl-stat__label {
  margin: 0;
  font-size: 0.8125rem;
  color: $text-grey;
}

.wl-stat__value {
  margin: 4px 0 0;
  font-size: 1.5rem;
  color: $title-grey;
  line-height: 1.2;
}
</style>
