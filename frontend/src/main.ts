import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { vuetify } from './plugins/vuetify'
import router from './router'
import './styles/index.scss'

const app = createApp(App)

// Pinia must be installed before the router: navigation guards use the stores.
app.use(createPinia())
app.use(vuetify)
app.use(router)

app.mount('#app')
