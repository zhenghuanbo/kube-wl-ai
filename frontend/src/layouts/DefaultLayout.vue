<template>
  <div class="wl-layout">
    <AppHeader />
    <AppSidebar />

    <v-main class="wl-main">
      <div class="wl-content wl-page">
        <router-view />
      </div>
    </v-main>
  </div>
</template>

<script setup lang="ts">
import AppHeader from '@/components/AppHeader.vue'
import AppSidebar from '@/components/AppSidebar.vue'
</script>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-main {
  background: $body-bg;
  min-height: calc(100vh - #{$header-height});
}
</style>
