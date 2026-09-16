<template>
  <v-container class="wl-not-found fill-height" fluid>
    <div class="wl-not-found__inner">
      <p class="wl-not-found__code">404</p>
      <p class="wl-not-found__text">页面不存在或已被移除</p>
      <v-btn color="primary" variant="flat" to="/dashboard" class="mt-6">返回首页</v-btn>
    </div>
  </v-container>
</template>

<script setup lang="ts">
// Standalone route: rendered outside DefaultLayout, so it brings its own centring.
</script>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-not-found {
  background: $body-bg;
}

.wl-not-found__inner {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 70vh;
  text-align: center;
}

.wl-not-found__code {
  margin: 0;
  font-size: 6rem;
  line-height: 1;
  color: $text-grey;
}

.wl-not-found__text {
  margin: 12px 0 0;
  color: $text-color;
  font-size: 1.125rem;
}
</style>
