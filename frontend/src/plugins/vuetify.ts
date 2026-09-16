/**
 * Vuetify 4 setup.
 *
 * The colour palette mirrors the material-vue theme
 * (legacy/frontend/src/config.js) so the rebuilt UI is visually identical
 * to the established design language.
 */
import 'vuetify/styles'
import '@mdi/font/css/materialdesignicons.css'

import { createVuetify, type ThemeDefinition } from 'vuetify'
import { aliases, mdi } from 'vuetify/iconsets/mdi'

/** Palette ported from legacy material-vue `src/config.js` (light theme). */
export const lightTheme: ThemeDefinition = {
  dark: false,
  colors: {
    primary: '#536DFE',
    secondary: '#FF5C93',
    error: '#FF4081',
    warning: '#FFC260',
    success: '#3CD4A0',
    info: '#9013FE',
    background: '#F6F7FF',
    surface: '#FFFFFF',
    'on-surface': '#6e6e6e',
  },
  variables: {
    'border-color': '#E8EAFC',
    'border-opacity': 1,
  },
}

export const darkTheme: ThemeDefinition = {
  dark: true,
  colors: {
    primary: '#536DFE',
    secondary: '#FF5C93',
    error: '#FF4081',
    warning: '#FFC260',
    success: '#3CD4A0',
    info: '#9013FE',
    background: '#121212',
    surface: '#1E1E1E',
  },
}

export const vuetify = createVuetify({
  theme: {
    defaultTheme: 'light',
    themes: { light: lightTheme, dark: darkTheme },
  },
  icons: {
    defaultSet: 'mdi',
    aliases,
    sets: { mdi },
  },
  defaults: {
    VCard: { rounded: 'lg' },
    VBtn: { rounded: 'lg' },
    VTextField: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
    VSelect: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
    VAutocomplete: { variant: 'outlined', density: 'comfortable', hideDetails: 'auto' },
    VDataTable: { density: 'comfortable' },
    VNavigationDrawer: { color: 'surface' },
  },
  display: {
    mobileBreakpoint: 'sm',
    thresholds: { xs: 0, sm: 600, md: 960, lg: 1280, xl: 1920 },
  },
})
