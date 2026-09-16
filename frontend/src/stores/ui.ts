import { defineStore } from 'pinia'
import { ref } from 'vue'

/** Global chrome state (sidebar, theme). */
export const useUiStore = defineStore('ui', () => {
  const drawer = ref(true)

  function toggleDrawer(): void {
    drawer.value = !drawer.value
  }

  function setDrawer(value: boolean): void {
    drawer.value = value
  }

  return { drawer, toggleDrawer, setDrawer }
})
