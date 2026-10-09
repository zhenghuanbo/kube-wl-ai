<script setup lang="ts">
/**
 * Renders the head of the notification queue.
 *
 * Mounted once, in `App.vue`: any layer can push a message without knowing
 * which component is on screen.
 */
import { computed } from 'vue'

import { useNotificationStore } from '@/stores/notifications'

const notifications = useNotificationStore()

const TONE_META = {
  success: { color: 'success', icon: 'mdi-check-circle-outline' },
  error: { color: 'error', icon: 'mdi-alert-circle-outline' },
  warning: { color: 'warning', icon: 'mdi-alert-outline' },
  info: { color: 'info', icon: 'mdi-information-outline' },
} as const

const meta = computed(() =>
  notifications.current ? TONE_META[notifications.current.tone] : TONE_META.info,
)
</script>

<template>
  <v-snackbar
    :model-value="notifications.current !== null"
    :timeout="notifications.current?.timeout ?? 4000"
    :color="meta.color"
    location="top right"
    rounded="lg"
    elevation="6"
    class="wl-snackbar"
    @update:model-value="notifications.dismiss()"
  >
    <div class="d-flex align-start ga-3">
      <v-icon :icon="meta.icon" size="22" class="mt-1" />
      <div class="flex-grow-1">
        <div class="text-body-1 font-weight-medium wl-snackbar__message">
          {{ notifications.current?.message }}
        </div>
        <div v-if="notifications.current?.detail" class="text-caption mt-1 wl-snackbar__detail">
          {{ notifications.current.detail }}
        </div>
      </div>
    </div>

    <template #actions>
      <v-btn variant="text" size="small" @click="notifications.dismiss()">关闭</v-btn>
    </template>
  </v-snackbar>
</template>

<style scoped lang="scss">
// A validation failure appends its per-field reasons on their own lines; without
// this they would collapse into one run-on sentence.
.wl-snackbar__message {
  white-space: pre-line;
}

.wl-snackbar__detail {
  opacity: 0.85;
  word-break: break-word;
}
</style>
