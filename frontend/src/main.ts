import { createPinia } from 'pinia'
import { createApp } from 'vue'

import { ApiError, setErrorHandler, setSessionExpiredHandler } from '@/api/client'
import App from './App.vue'
import { vuetify } from './plugins/vuetify'
import router from './router'
import { useAuthStore } from './stores/auth'
import { useNotificationStore } from './stores/notifications'
import './styles/index.scss'

const app = createApp(App)
const pinia = createPinia()

// Pinia must be installed before the router: navigation guards use the stores.
app.use(pinia)
app.use(vuetify)
app.use(router)

const auth = useAuthStore(pinia)
const notifications = useNotificationStore(pinia)

/**
 * Wire the HTTP client to the stores.
 *
 * This lives here rather than in `api/client.ts` so the transport layer never
 * imports application state — the dependency only ever points one way.
 */
setErrorHandler((error: ApiError) => {
  // 401s are handled by the client's refresh flow; validation and permission
  // failures are surfaced by the form that triggered them, so only genuine
  // surprises are announced globally.
  if (error.status === 401) return
  if (error.status === 403 || error.isValidation) return
  notifications.error(error.message)
})

setSessionExpiredHandler(() => {
  auth.clear()
  notifications.warning('登录已过期，请重新登录')
  void router.replace({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
})

app.mount('#app')
