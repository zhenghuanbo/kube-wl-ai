<template>
  <v-card class="wl-tab-table">
    <v-tabs v-model="active" color="primary" density="comfortable" show-arrows>
      <v-tab v-for="tab in tabs" :key="tab.value" :value="tab.value" :prepend-icon="tab.icon">
        {{ tab.title }}
      </v-tab>
    </v-tabs>

    <v-divider />

    <div v-if="$slots.toolbar" class="wl-tab-table__toolbar">
      <slot name="toolbar" :active="active" />
    </div>

    <div class="wl-tab-table__body">
      <slot :active="active" />
    </div>
  </v-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'

export interface TabItem {
  title: string
  value: string
  icon?: string
}

const props = defineProps<{
  tabs: TabItem[]
  modelValue: string
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const active = computed({
  get: () => props.modelValue,
  set: (value: string) => emit('update:modelValue', value),
})
</script>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-tab-table {
  overflow: hidden;
}

.wl-tab-table__toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px 0;
}

.wl-tab-table__body {
  padding: 8px 4px 4px;
}
</style>
