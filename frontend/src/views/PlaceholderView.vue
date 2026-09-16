<template>
  <div>
    <PageHeader :title="title" :subtitle="subtitle" />

    <v-card>
      <v-card-text>
        <EmptyState
          :title="`${title} 尚未开放`"
          :description="description"
          icon="mdi-progress-wrench"
        >
          <template #action>
            <v-btn color="primary" variant="flat" to="/dashboard">返回首页</v-btn>
          </template>
        </EmptyState>
      </v-card-text>
    </v-card>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

import EmptyState from '@/components/common/EmptyState.vue'
import PageHeader from '@/components/common/PageHeader.vue'

const route = useRoute()

const title = computed(() => route.meta.title ?? '功能模块')
const phase = computed(() => route.meta.phase)

const subtitle = computed(() =>
  phase.value ? `计划交付：${phase.value}` : '计划交付阶段待定',
)

const description = computed(
  () =>
    `该模块将在 ${phase.value ?? '后续阶段'} 实现，包含原型设计中的完整列表、筛选、详情与操作能力，并接入后端真实数据与权限控制。`,
)
</script>
