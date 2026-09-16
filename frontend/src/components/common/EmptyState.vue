<template>
  <div class="wl-empty-state">
    <v-icon :icon="icon" size="56" class="wl-empty-state__icon" />
    <p class="wl-empty-state__title">{{ title }}</p>
    <p v-if="description" class="wl-empty-state__description">{{ description }}</p>

    <div v-if="$slots.action" class="wl-empty-state__action">
      <slot name="action" />
    </div>
  </div>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    title: string
    description?: string
    icon?: string
  }>(),
  { icon: 'mdi-inbox-outline' },
)
</script>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 48px 24px;
}

.wl-empty-state__icon {
  color: $text-grey;
  margin-bottom: 12px;
}

.wl-empty-state__title {
  margin: 0;
  font-size: 1rem;
  color: $title-grey;
}

.wl-empty-state__description {
  margin: 6px 0 0;
  font-size: 0.875rem;
  color: $text-grey;
  max-width: 460px;
  line-height: 1.6;
}

.wl-empty-state__action {
  margin-top: 20px;
}
</style>
