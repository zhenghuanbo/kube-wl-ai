/**
 * Application-wide notifications.
 *
 * One queue, one host component, so any layer — an API interceptor, a form
 * submit, a background poll — can surface a message without threading a
 * callback through the component tree.
 */
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

export type NotificationTone = 'success' | 'error' | 'warning' | 'info'

export interface AppNotification {
  id: number
  tone: NotificationTone
  message: string
  detail?: string
  timeout: number
}

interface PushOptions {
  detail?: string
  /** Milliseconds; 0 keeps it until dismissed. Errors default to 0. */
  timeout?: number
}

export const useNotificationStore = defineStore('notifications', () => {
  const items = ref<AppNotification[]>([])
  let sequence = 0

  /** Only the head of the queue is rendered; the rest wait their turn. */
  const current = computed<AppNotification | null>(() => items.value[0] ?? null)

  function push(tone: NotificationTone, message: string, options: PushOptions = {}): number {
    const id = ++sequence
    items.value.push({
      id,
      tone,
      message,
      detail: options.detail,
      timeout: options.timeout ?? (tone === 'error' ? 0 : 4000),
    })
    return id
  }

  function dismiss(id?: number): void {
    if (id === undefined) {
      items.value.shift()
      return
    }
    items.value = items.value.filter((item) => item.id !== id)
  }

  return {
    items,
    current,
    push,
    dismiss,
    success: (message: string, options?: PushOptions) => push('success', message, options),
    error: (message: string, options?: PushOptions) => push('error', message, options),
    warning: (message: string, options?: PushOptions) => push('warning', message, options),
    info: (message: string, options?: PushOptions) => push('info', message, options),
  }
})
