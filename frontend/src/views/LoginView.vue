<script setup lang="ts">
/**
 * Login screen.
 *
 * Deliberately minimal: the platform authenticates against the university
 * directory, so there is no sign-up, no password reset and no "remember me".
 * When LDAP is the configured provider the copy says so, which stops people
 * from hunting for a password form that does not exist.
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { fetchLoginOptions, type LoginOptions } from '@/api/auth'
import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const showPassword = ref(false)
const errorMessage = ref('')
const options = ref<LoginOptions | null>(null)

const directoryManaged = computed(() => options.value?.ldapEnabled === true)

const canSubmit = computed(
  () => username.value.trim().length > 0 && password.value.length > 0 && !auth.busy,
)

onMounted(async () => {
  try {
    options.value = await fetchLoginOptions()
  } catch {
    // The login form still works without this; it only drives the hint text.
  }
})

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  errorMessage.value = ''

  try {
    await auth.loginWithPassword(username.value.trim(), password.value)
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/dashboard'
    await router.replace(redirect)
  } catch (error) {
    errorMessage.value =
      error instanceof ApiError ? error.message : '登录失败，请稍后重试'
    password.value = ''
  }
}
</script>

<template>
  <div class="wl-login">
    <div class="wl-login__brand">
      <div class="wl-login__mark">
        <v-icon icon="mdi-brain" size="34" />
      </div>
      <div>
        <div class="wl-login__title">西湖 AI 平台</div>
        <div class="wl-login__subtitle">GPU 资源调度与管理系统</div>
      </div>
    </div>

    <v-card class="wl-login__card" rounded="xl" elevation="8">
      <v-card-text class="pa-8 pa-sm-10">
        <h1 class="wl-login__heading">登录</h1>
        <p class="wl-login__hint">
          {{
            directoryManaged
              ? '请使用学校统一身份认证（FreeIPA）账号登录。'
              : '请使用平台管理员分配的账号登录。'
          }}
        </p>

        <v-alert
          v-if="errorMessage"
          type="error"
          variant="tonal"
          density="comfortable"
          class="mb-5"
          rounded="lg"
        >
          {{ errorMessage }}
        </v-alert>

        <v-form @submit.prevent="submit">
          <v-text-field
            v-model="username"
            label="用户名"
            prepend-inner-icon="mdi-account-outline"
            autocomplete="username"
            variant="outlined"
            density="comfortable"
            hide-details="auto"
            class="mb-4"
            autofocus
          />

          <v-text-field
            v-model="password"
            label="密码"
            prepend-inner-icon="mdi-lock-outline"
            :type="showPassword ? 'text' : 'password'"
            :append-inner-icon="showPassword ? 'mdi-eye-off-outline' : 'mdi-eye-outline'"
            autocomplete="current-password"
            variant="outlined"
            density="comfortable"
            hide-details="auto"
            class="mb-6"
            @click:append-inner="showPassword = !showPassword"
          />

          <v-btn
            type="submit"
            color="primary"
            size="large"
            block
            rounded="lg"
            :loading="auth.busy"
            :disabled="!canSubmit"
          >
            登 录
          </v-btn>
        </v-form>
      </v-card-text>
    </v-card>

    <p class="wl-login__footer">
      账号问题请联系实验室管理员 · 研究生院 AI 计算平台
    </p>
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-login {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 28px;
  padding: 32px 20px;
  background:
    radial-gradient(1200px 600px at 12% -10%, rgba($primary, 0.16), transparent 60%),
    radial-gradient(900px 500px at 92% 8%, rgba($info, 0.12), transparent 55%),
    $body-bg;

  &__brand {
    display: flex;
    align-items: center;
    gap: 14px;
  }

  &__mark {
    width: 58px;
    height: 58px;
    border-radius: 18px;
    display: grid;
    place-items: center;
    color: #fff;
    background: linear-gradient(135deg, $primary, $info);
    box-shadow: $card-shadow;
  }

  &__title {
    font-size: 1.6rem;
    font-weight: 700;
    color: $title-grey;
    line-height: 1.2;
  }

  &__subtitle {
    font-size: 0.86rem;
    color: $text-grey;
    letter-spacing: 0.04em;
  }

  &__card {
    width: 100%;
    max-width: 440px;
  }

  &__heading {
    font-size: 1.7rem;
    font-weight: 600;
    color: $title-grey;
    margin-bottom: 6px;
  }

  &__hint {
    font-size: 0.9rem;
    color: $text-grey;
    margin-bottom: 26px;
  }

  &__footer {
    font-size: 0.8rem;
    color: $text-grey;
  }
}
</style>
