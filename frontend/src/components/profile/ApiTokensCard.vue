<script setup lang="ts">
/**
 * Personal access tokens.
 *
 * The whole component is shaped by one property of the API: **the secret is
 * returned exactly once**. So the create flow does not close on success — it
 * replaces the form with the token, blocks dismissal until it has been copied,
 * and never offers to show it again. A dialog that closed itself would leave the
 * user with a token they have already lost and no way to know.
 *
 * The listing deliberately shows a *prefix* rather than a masked secret. Masking
 * implies there is something behind the mask to reveal.
 */
import { computed, onMounted, reactive, ref } from 'vue'

import { ApiError } from '@/api/client'
import { createApiToken, listApiTokens, revokeApiToken } from '@/api/apiTokens'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import { useNotificationStore } from '@/stores/notifications'
import type { ApiToken, ApiTokenIssued } from '@/types/apiToken'
import { SCOPE_OPTIONS, isExpired, scopeSummary } from '@/types/apiToken'

const notifications = useNotificationStore()

const tokens = ref<ApiToken[]>([])
const loading = ref(false)

const createOpen = ref(false)
const revokeOpen = ref(false)
const revokeTarget = ref<ApiToken | null>(null)

/** Set once a token has been minted; while set, the dialog shows the secret. */
const issued = ref<ApiTokenIssued | null>(null)
const copied = ref(false)

const form = reactive<{ name: string; scopes: string[]; expiresInDays: number | null }>({
  name: '',
  scopes: [],
  // A year, matching the server's ceiling. "Never expires" is available but not
  // the default: a credential nobody ever rotates is the one that leaks.
  expiresInDays: 365,
})

const expiryOptions = [
  { title: '30 天', value: 30 },
  { title: '90 天', value: 90 },
  { title: '1 年', value: 365 },
  { title: '永不过期', value: null },
]

const scopeItems = computed(() => SCOPE_OPTIONS.map((option) => ({ ...option })))

async function load(): Promise<void> {
  loading.value = true
  try {
    tokens.value = await listApiTokens()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    loading.value = false
  }
}

onMounted(load)

function openCreate(): void {
  issued.value = null
  copied.value = false
  form.name = ''
  form.scopes = []
  form.expiresInDays = 365
  createOpen.value = true
}

async function submit(): Promise<void> {
  if (!form.name.trim()) {
    notifications.error('请填写令牌名称')
    return
  }
  try {
    issued.value = await createApiToken({
      name: form.name.trim(),
      scopes: form.scopes,
      expiresInDays: form.expiresInDays,
    })
    copied.value = false
    await load()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  }
}

async function copySecret(): Promise<void> {
  const secret = issued.value?.token
  if (!secret) return
  try {
    await navigator.clipboard.writeText(secret)
    copied.value = true
  } catch {
    // Clipboard access is denied on insecure origins and in some embedded
    // webviews. The secret is on screen and selectable, so the honest move is
    // to say the copy failed rather than to claim it worked.
    notifications.error('无法访问剪贴板，请手动选中并复制')
  }
}

function askRevoke(token: ApiToken): void {
  revokeTarget.value = token
  revokeOpen.value = true
}

async function confirmRevoke(): Promise<void> {
  const target = revokeTarget.value
  if (!target) return
  try {
    await revokeApiToken(target.id)
    notifications.success(`已吊销令牌「${target.name}」`)
    await load()
  } catch (error) {
    if (error instanceof ApiError) notifications.error(error.message)
  } finally {
    revokeOpen.value = false
    revokeTarget.value = null
  }
}

function closeCreate(): void {
  createOpen.value = false
  issued.value = null
  copied.value = false
}

function formatDate(value: string | null): string {
  if (!value) return '永不过期'
  return new Date(value).toLocaleString('zh-CN')
}
</script>

<template>
  <v-card class="mb-6">
    <v-card-title>API 令牌</v-card-title>
    <v-card-subtitle>
      用于脚本与 CI 的长期凭证。令牌只能继承你的权限，不能超过你本人。
    </v-card-subtitle>
    <v-card-text>
      <div class="d-flex justify-end mb-3">
        <v-btn color="primary" prepend-icon="mdi-plus" size="small" @click="openCreate">
          新建令牌
        </v-btn>
      </div>

      <v-table v-if="tokens.length" density="comfortable">
        <thead>
          <tr>
            <th>名称</th>
            <th>前缀</th>
            <th>权限范围</th>
            <th>有效期至</th>
            <th>最近使用</th>
            <th class="text-end">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="token in tokens" :key="token.id">
            <td>{{ token.name }}</td>
            <td>
              <code class="wl-mono">{{ token.prefix }}…</code>
            </td>
            <td class="text-caption">{{ scopeSummary(token) }}</td>
            <td>
              <v-chip
                v-if="isExpired(token)"
                size="small"
                color="error"
                variant="tonal"
                label
              >
                已过期
              </v-chip>
              <span v-else class="text-caption">{{ formatDate(token.expiresAt) }}</span>
            </td>
            <td class="text-caption">
              {{ token.lastUsedAt ? formatDate(token.lastUsedAt) : '从未使用' }}
            </td>
            <td class="text-end">
              <v-btn
                size="small"
                variant="text"
                color="error"
                @click="askRevoke(token)"
              >
                吊销
              </v-btn>
            </td>
          </tr>
        </tbody>
      </v-table>

      <EmptyState
        v-else-if="!loading"
        icon="mdi-key-chain"
        title="还没有 API 令牌"
        description="需要脚本或 CI 访问平台时，在这里创建一枚。"
      />
    </v-card-text>
  </v-card>

  <!--
    One dialog, two states. The second state exists because the secret is shown
    exactly once, and closing on success would lose it.
  -->
  <v-dialog v-model="createOpen" max-width="640" :persistent="issued !== null">
    <v-card>
      <template v-if="!issued">
        <v-card-title>新建 API 令牌</v-card-title>
        <v-card-text>
          <v-text-field
            v-model="form.name"
            label="名称"
            density="comfortable"
            variant="outlined"
            hint="用于在列表中区分用途，例如 ci-deploy"
            persistent-hint
            class="mb-4"
          />
          <v-select
            v-model="form.expiresInDays"
            :items="expiryOptions"
            label="有效期"
            density="comfortable"
            variant="outlined"
            class="mb-4"
          />
          <v-select
            v-model="form.scopes"
            :items="scopeItems"
            item-title="title"
            item-value="value"
            label="权限范围"
            multiple
            chips
            closable-chips
            density="comfortable"
            variant="outlined"
            hint="留空表示跟随你的角色：你被降权时，令牌立即同步收窄"
            persistent-hint
          />
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="closeCreate">取消</v-btn>
          <v-btn color="primary" @click="submit">创建</v-btn>
        </v-card-actions>
      </template>

      <template v-else>
        <v-card-title>令牌已创建</v-card-title>
        <v-card-text>
          <v-alert type="warning" variant="tonal" density="comfortable" class="mb-4">
            这是唯一一次显示该令牌。离开本对话框后将无法再次查看，只能吊销后重建。
          </v-alert>
          <v-textarea
            :model-value="issued.token"
            readonly
            auto-grow
            variant="outlined"
            density="comfortable"
            class="wl-mono"
            @focus="($event.target as HTMLTextAreaElement).select()"
          />
        </v-card-text>
        <v-card-actions>
          <v-btn
            :prepend-icon="copied ? 'mdi-check' : 'mdi-content-copy'"
            :color="copied ? 'success' : 'primary'"
            @click="copySecret"
          >
            {{ copied ? '已复制' : '复制令牌' }}
          </v-btn>
          <v-spacer />
          <!--
            Dismissal is only offered once the secret has been copied. It is not
            a lock — the text is selectable and the user may leave by other
            means — but it is the last place to stop somebody losing a credential
            they cannot recover.
          -->
          <v-btn variant="text" :disabled="!copied" @click="closeCreate">
            我已保存
          </v-btn>
        </v-card-actions>
      </template>
    </v-card>
  </v-dialog>

  <ConfirmDialog
    v-model="revokeOpen"
    title="吊销令牌"
    :text="`吊销后「${revokeTarget?.name ?? ''}」立即失效，正在使用它的脚本会开始收到 401。`"
    confirm-text="吊销"
    confirm-color="error"
    @confirm="confirmRevoke"
  />
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace;
  font-variant-numeric: tabular-nums;
}
</style>
