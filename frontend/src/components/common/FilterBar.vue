<template>
  <v-card flat class="wl-filter-bar">
    <div class="wl-filter-bar__fields">
      <slot />
    </div>

    <div v-if="$slots.actions" class="wl-filter-bar__actions">
      <slot name="actions" />
    </div>
  </v-card>
</template>

<script setup lang="ts">
// Filter controls are supplied by the page; this component only provides the
// consistent card shell, spacing and responsive wrapping.
</script>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-filter-bar {
  padding: 16px 20px;
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  justify-content: space-between;
}

.wl-filter-bar__fields {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  flex: 1 1 auto;
}

.wl-filter-bar__actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
</style>
