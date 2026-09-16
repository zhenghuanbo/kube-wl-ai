<template>
  <v-navigation-drawer
    v-model="drawer"
    app
    clipped
    :rail="rail"
    :permanent="mdAndUp"
    :temporary="!mdAndUp"
    width="240"
    rail-width="96"
  >
    <v-list nav density="comfortable" class="py-2">
      <template v-for="(entry, index) in entries" :key="`${entry.kind}-${index}`">
        <v-list-subheader v-if="entry.kind === 'heading'" class="wl-nav-heading">
          {{ entry.title }}
        </v-list-subheader>

        <v-list-item
          v-else
          :to="entry.to"
          :prepend-icon="entry.icon"
          :title="entry.title"
          color="primary"
          rounded="lg"
          link
        />
      </template>
    </v-list>
  </v-navigation-drawer>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useDisplay } from 'vuetify'

import { filterNavigation } from '@/config/navigation'
import { useAuthStore } from '@/stores/auth'
import { useUiStore } from '@/stores/ui'

const ui = useUiStore()
const auth = useAuthStore()
const { mdAndUp } = useDisplay()

/**
 * Desktop: the drawer is always present and the toggle collapses it to a rail.
 * Mobile: the drawer is temporary and the toggle opens/closes it.
 */
const drawer = computed({
  get: () => ui.drawer,
  set: (value: boolean) => ui.setDrawer(value),
})

const rail = computed(() => mdAndUp.value && !ui.drawer)

const entries = computed(() => filterNavigation(auth.roles))
</script>
