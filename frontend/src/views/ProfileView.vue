<script setup lang="ts">
/**
 * Personal settings.
 *
 * Today this is SSH key management, which is what a user needs before an
 * instance is usable: the keys are mounted into every instance they own as
 * `authorized_keys`. There is no `ssh_key:*` permission code, because there is
 * nothing to authorise — a user may only ever touch their own keys, and the
 * backend enforces that by ownership rather than by a grant an administrator
 * could widen.
 */
import { onMounted, reactive, ref } from 'vue'

import { ApiError } from '@/api/client'
import { addSshKey, deleteSshKey, listSshKeys } from '@/api/instances'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import PageHeader from '@/components/common/PageHeader.vue'
import { useAuthStore } from '@/stores/auth'
import { useNotificationStore } from '@/stores/notifications'
import type { SshKey } from '@/types/instance'

const auth = useAuthStore()
const notifications = useNotificationStore()

const keys = ref<SshKey[]>([])
const loading = ref(false)

const form = reactive({ name: '', publicKey: '' })
const addOpen = ref(false)
const saving = ref(false)

const deleteOpen = ref(false)
const deleteTarget = ref<SshKey | null>(null)

async function load(): Promise<void> {
  loading.value = true
  try {
    keys.value = await listSshKeys()
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
  } finally {
    loading.value = false
  }
}

/**
 * A public key is `ssh-ed25519 AAAA... comment`. Only the first two fields
 * matter; the comment is preserved but not required.
 */
const keyLooksValid = (value: string): boolean => {
  const parts = value.trim().split(/\s+/)
  return parts.length >= 2 && /^(ssh-|ecdsa-|sk-)/.test(parts[0]) && parts[1].length > 20
}

function openAdd(): void {
  form.name = ''
  form.publicKey = ''
  addOpen.value = true
}

async function submitAdd(): Promise<void> {
  if (!keyLooksValid(form.publicKey)) {
    notifications.error('公钥格式不正确，应形如 ssh-ed25519 AAAA… user@host')
    return
  }
  saving.value = true
  try {
    await addSshKey({
      name: form.name.trim() || 'default',
      publicKey: form.publicKey.trim(),
    })
    notifications.success('公钥已登记；正在运行的实例将在下次重新下发后生效')
    addOpen.value = false
    await load()
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    // A duplicate fingerprint is the common case here, and the backend says so.
    notifications.error(error.message)
  } finally {
    saving.value = false
  }
}

function confirmDelete(key: SshKey): void {
  deleteTarget.value = key
  deleteOpen.value = true
}

async function submitDelete(): Promise<void> {
  const target = deleteTarget.value
  deleteOpen.value = false
  if (!target) return
  try {
    await deleteSshKey(target.id)
    notifications.success('公钥已删除')
    await load()
  } catch (error) {
    if (!(error instanceof ApiError)) throw error
    notifications.error(error.message)
  } finally {
    deleteTarget.value = null
  }
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

onMounted(() => void load())
</script>

<template>
  <div>
    <PageHeader title="个人设置" subtitle="账户信息与 SSH 公钥">
      <template #actions>
        <v-btn color="primary" prepend-icon="mdi-plus" @click="openAdd">添加公钥</v-btn>
      </template>
    </PageHeader>

    <v-card class="mb-6">
      <v-card-title>账户</v-card-title>
      <v-card-text>
        <v-table density="comfortable">
          <tbody>
            <tr>
              <th class="wl-key">用户名</th>
              <td>{{ auth.user?.username ?? '—' }}</td>
            </tr>
            <tr>
              <th class="wl-key">姓名</th>
              <td>{{ auth.user?.displayName || auth.user?.fullName || '—' }}</td>
            </tr>
            <tr>
              <th class="wl-key">邮箱</th>
              <td>{{ auth.user?.email ?? '—' }}</td>
            </tr>
            <tr>
              <th class="wl-key">角色</th>
              <td>{{ auth.roles.join('、') || '—' }}</td>
            </tr>
          </tbody>
        </v-table>
      </v-card-text>
    </v-card>

    <v-card>
      <v-card-title>SSH 公钥</v-card-title>
      <v-card-subtitle>
        这些公钥会被挂载进你名下的每个实例，用于 SSH 登录。只上传公钥，私钥永远不要离开你的电脑。
      </v-card-subtitle>
      <v-card-text>
        <v-progress-linear v-if="loading" indeterminate class="mb-3" />

        <v-table v-if="keys.length" density="comfortable">
          <thead>
            <tr>
              <th>名称</th>
              <th>指纹</th>
              <th>添加时间</th>
              <th class="text-end">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="key in keys" :key="key.id">
              <td>{{ key.name }}</td>
              <td class="wl-mono text-caption">{{ key.fingerprint }}</td>
              <td class="text-caption">{{ formatTime(key.createdAt) }}</td>
              <td class="text-end">
                <v-btn
                  size="small"
                  variant="text"
                  color="error"
                  icon="mdi-delete-outline"
                  @click="confirmDelete(key)"
                />
              </td>
            </tr>
          </tbody>
        </v-table>

        <EmptyState
          v-else-if="!loading"
          icon="mdi-key-outline"
          title="还没有登记公钥"
          description="没有公钥时实例仍可创建，但 SSH 无法登录。"
        />
      </v-card-text>
    </v-card>

    <v-dialog v-model="addOpen" max-width="640">
      <v-card>
        <v-card-title>添加 SSH 公钥</v-card-title>
        <v-card-text>
          <v-text-field
            v-model="form.name"
            label="名称"
            density="comfortable"
            variant="outlined"
            placeholder="工作笔记本"
            hint="仅用于你自己辨认"
            persistent-hint
          />
          <v-textarea
            v-model="form.publicKey"
            label="公钥内容 *"
            rows="4"
            density="comfortable"
            variant="outlined"
            class="wl-mono mt-2"
            placeholder="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... user@host"
          />
          <v-alert type="info" variant="tonal" density="compact" class="mt-2">
            在本地执行 <code>cat ~/.ssh/id_ed25519.pub</code> 并把整行内容粘贴到这里。
            相同指纹的公钥不能重复登记。
          </v-alert>
        </v-card-text>
        <v-card-actions>
          <v-spacer />
          <v-btn variant="text" @click="addOpen = false">取消</v-btn>
          <v-btn color="primary" :loading="saving" @click="submitAdd">添加</v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>

    <ConfirmDialog
      v-model="deleteOpen"
      title="删除公钥"
      :text="
        deleteTarget
          ? `删除后，使用 ${deleteTarget.name} 对应私钥的登录将失效。正在运行的实例需要重新下发才会更新。`
          : ''
      "
      confirm-text="删除"
      confirm-color="error"
      @confirm="submitDelete"
    />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/tokens' as *;

.wl-mono {
  font-family: 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace;
}

.wl-key {
  width: 100px;
  font-weight: 500;
  color: $title-grey;
}
</style>
