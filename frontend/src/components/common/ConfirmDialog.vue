<template>
  <v-dialog
    :model-value="modelValue"
    :max-width="maxWidth"
    :persistent="persistent"
    @update:model-value="onUpdate"
  >
    <v-card>
      <v-card-title class="wl-confirm__title">{{ title }}</v-card-title>

      <v-card-text class="wl-confirm__text">
        <slot>{{ text }}</slot>
      </v-card-text>

      <v-card-actions class="wl-confirm__actions">
        <v-spacer />
        <v-btn variant="text" :disabled="loading" @click="onCancel">
          {{ cancelText }}
        </v-btn>
        <v-btn :color="confirmColor" variant="flat" :loading="loading" @click="onConfirm">
          {{ confirmText }}
        </v-btn>
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    modelValue: boolean
    title: string
    text?: string
    confirmText?: string
    cancelText?: string
    /** Destructive actions use the material-vue error colour. */
    confirmColor?: string
    persistent?: boolean
    loading?: boolean
    maxWidth?: number | string
  }>(),
  {
    text: '',
    confirmText: '确定',
    cancelText: '取消',
    confirmColor: 'primary',
    persistent: true,
    loading: false,
    maxWidth: 440,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  confirm: []
  cancel: []
}>()

function onUpdate(value: boolean): void {
  emit('update:modelValue', value)
}

function onConfirm(): void {
  emit('confirm')
}

function onCancel(): void {
  emit('cancel')
  emit('update:modelValue', false)
}
</script>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-confirm__title {
  font-size: 1.25rem;
  font-weight: 500;
  color: $title-grey;
}

.wl-confirm__text {
  color: $text-color;
  line-height: 1.6;
}

.wl-confirm__actions {
  padding: 8px 16px 16px;
}
</style>
