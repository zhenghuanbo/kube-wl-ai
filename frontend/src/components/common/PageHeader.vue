<template>
  <div class="wl-page-header">
    <div class="wl-page-header__text">
      <h1 class="page-title">{{ title }}</h1>
      <p v-if="subtitle" class="wl-page-header__subtitle">{{ subtitle }}</p>
    </div>

    <div v-if="$slots.actions" class="wl-page-header__actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{
  title: string
  subtitle?: string
}>()
</script>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-page-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 24px;
}

.wl-page-header__subtitle {
  margin: 8px 0 0;
  color: $text-grey;
  font-size: 0.875rem;
  line-height: 1.5;
}

.wl-page-header__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 4px;
}
</style>
