<template>
  <v-app-bar app flat color="primary" :height="64" class="wl-app-bar">
    <v-app-bar-nav-icon
      :icon="ui.drawer ? 'mdi-arrow-left' : 'mdi-menu'"
      @click="ui.toggleDrawer()"
    />

    <v-toolbar-title class="wl-brand">
      <span class="text-h6 font-weight-medium">西湖 AI 平台</span>
    </v-toolbar-title>

    <v-spacer />

    <v-text-field
      v-model="search"
      class="wl-search d-none d-md-flex"
      placeholder="搜索实例 / 任务 / 应用"
      prepend-inner-icon="mdi-magnify"
      variant="solo"
      density="compact"
      rounded="pill"
      flat
      hide-details
      single-line
      @keyup.enter="onSearch"
    />

    <v-spacer />

    <v-btn icon="mdi-bell-outline" variant="text" aria-label="通知" />
    <v-btn icon="mdi-help-circle-outline" variant="text" aria-label="帮助" />

    <v-menu location="bottom end" :offset="8">
      <template #activator="{ props }">
        <v-btn v-bind="props" variant="text" icon class="mr-2" aria-label="账户">
          <v-avatar size="32" color="secondary">
            <span class="text-caption font-weight-medium">{{ initials }}</span>
          </v-avatar>
        </v-btn>
      </template>

      <v-list density="compact" min-width="240">
        <v-list-item :title="displayName" :subtitle="auth.user?.email ?? ''" />
        <v-divider class="my-1" />
        <v-list-item
          prepend-icon="mdi-account-cog-outline"
          title="个人设置"
          to="/profile"
          link
        />
        <v-list-item
          prepend-icon="mdi-logout"
          title="退出登录"
          link
          @click="onLogout"
        />
      </v-list>
    </v-menu>
  </v-app-bar>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()
const auth = useAuthStore()
const router = useRouter()

const search = ref('')

const displayName = computed(() => auth.user?.fullName || auth.user?.username || '未登录')

const initials = computed(() => {
  const source = auth.user?.fullName || auth.user?.username || '?'
  return source.slice(0, 1).toUpperCase()
})

function onSearch(): void {
  // Global search is wired up in a later phase; the field is present so the
  // shell matches the established layout.
}

function onLogout(): void {
  // Phase 0 placeholder — replaced by the real logout call in Phase 1.
  auth.clear()
  void router.push('/dashboard')
}
</script>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-app-bar {
  border-bottom: 1px solid rgba(255, 255, 255, 0.12);
}

.wl-brand {
  flex: 0 1 auto;
}

.wl-search {
  max-width: 420px;

  :deep(.v-field) {
    background: rgba(255, 255, 255, 0.14);
    color: #fff;
  }

  :deep(.v-field__input) {
    color: #fff;

    &::placeholder {
      color: rgba(255, 255, 255, 0.7);
      opacity: 1;
    }
  }

  :deep(.v-icon) {
    color: rgba(255, 255, 255, 0.75);
  }
}
</style>
