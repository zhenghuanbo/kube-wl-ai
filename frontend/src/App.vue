<template>
  <v-app>
    <router-view />
    <AppSnackbar />
  </v-app>
</template>

<script setup lang="ts">
// Root component. The Vuetify layout comes from the routed layout
// (DefaultLayout / the bare login screen), so this stays intentionally thin.
import AppSnackbar from '@/components/AppSnackbar.vue'
</script>
