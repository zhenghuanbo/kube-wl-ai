<template>
  <div class="wl-terminal">
    <div class="wl-terminal__bar">
      <v-icon size="small" :icon="stateIcon" :color="stateColor" />
      <span class="wl-terminal__state">{{ stateText }}</span>
      <v-spacer />
      <v-btn
        v-if="status !== 'open'"
        size="small"
        variant="text"
        prepend-icon="mdi-refresh"
        :loading="status === 'connecting'"
        @click="connect"
      >
        重新连接
      </v-btn>
      <v-btn
        v-else
        size="small"
        variant="text"
        prepend-icon="mdi-close"
        @click="disconnect"
      >
        断开
      </v-btn>
    </div>
    <div ref="host" class="wl-terminal__screen" />
  </div>
</template>

<script setup lang="ts">
import { FitAddon } from '@xterm/addon-fit'
import { Terminal } from '@xterm/xterm'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { terminalSocketUrl } from '@/api/instances'

import '@xterm/xterm/css/xterm.css'

/**
 * An interactive shell inside a running instance.
 *
 * The frame protocol is split by *type*, not by content: binary frames are
 * keystrokes and text frames are control messages. Sending everything as text
 * and sniffing for JSON would break the moment a user pastes JSON into their
 * shell, which is exactly when a terminal must not misbehave.
 */
const props = defineProps<{
  instanceId: number
  /** Reconnect from scratch whenever this changes (e.g. after a restart). */
  sessionKey?: string | number
  active?: boolean
}>()

type Status = 'idle' | 'connecting' | 'open' | 'closed' | 'error'

const host = ref<HTMLElement | null>(null)
const status = ref<Status>('idle')
const message = ref('')

let terminal: Terminal | null = null
let fit: FitAddon | null = null
let socket: WebSocket | null = null
let observer: ResizeObserver | null = null
/** Guards against the "close" handler reopening after an explicit teardown. */
let disposed = false

const stateText = computed(() => {
  switch (status.value) {
    case 'connecting':
      return '连接中…'
    case 'open':
      return '已连接'
    case 'closed':
      return '连接已结束'
    case 'error':
      return message.value || '连接失败'
    default:
      return '未连接'
  }
})

const stateIcon = computed(() => {
  switch (status.value) {
    case 'open':
      return 'mdi-lan-connect'
    case 'connecting':
      return 'mdi-lan-pending'
    case 'error':
      return 'mdi-lan-disconnect'
    default:
      return 'mdi-lan'
  }
})

const stateColor = computed(() => {
  switch (status.value) {
    case 'open':
      return 'success'
    case 'connecting':
      return 'info'
    case 'error':
      return 'error'
    default:
      return 'medium-emphasis'
  }
})

function ensureTerminal(): Terminal {
  if (terminal) return terminal

  terminal = new Terminal({
    cursorBlink: true,
    fontFamily: '"JetBrains Mono", "Fira Code", Menlo, Consolas, monospace',
    fontSize: 13,
    // The material-vue dark surface; xterm needs concrete colours, not tokens.
    theme: { background: '#1e1e2d', foreground: '#e1e1ef', cursor: '#536dfe' },
    scrollback: 5000,
    convertEol: true,
  })
  fit = new FitAddon()
  terminal.loadAddon(fit)

  terminal.onData((data) => {
    // Binary, never text: this is a keystroke, not a control message.
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(new TextEncoder().encode(data))
    }
  })

  return terminal
}

function fitAndReport(): void {
  if (!fit || !terminal) return
  try {
    fit.fit()
  } catch {
    // fit() throws when the element has no layout yet (e.g. a hidden tab).
    return
  }
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(
      JSON.stringify({ type: 'resize', rows: terminal.rows, cols: terminal.cols }),
    )
  }
}

function disconnect(): void {
  socket?.close()
  socket = null
  status.value = 'closed'
}

function connect(): void {
  const term = ensureTerminal()
  socket?.close()
  status.value = 'connecting'
  message.value = ''

  let url: string
  try {
    url = terminalSocketUrl(props.instanceId, term.rows || 24, term.cols || 80)
  } catch {
    status.value = 'error'
    message.value = '无法构造终端地址'
    return
  }

  const ws = new WebSocket(url)
  ws.binaryType = 'arraybuffer'
  socket = ws

  ws.onopen = () => {
    if (disposed || socket !== ws) return
    status.value = 'open'
    term.clear()
    fitAndReport()
    term.focus()
  }

  ws.onmessage = (event: MessageEvent) => {
    if (socket !== ws) return
    if (typeof event.data === 'string') {
      // A control frame from the server (today only errors).
      try {
        const parsed = JSON.parse(event.data) as { message?: string }
        if (parsed.message) message.value = parsed.message
      } catch {
        term.write(event.data)
      }
      return
    }
    term.write(new Uint8Array(event.data as ArrayBuffer))
  }

  ws.onerror = () => {
    if (socket !== ws) return
    // The close handler carries the useful detail; keep this from clobbering it.
    if (status.value === 'connecting') status.value = 'error'
  }

  ws.onclose = (event: CloseEvent) => {
    if (socket !== ws) return
    socket = null
    if (status.value === 'open' || status.value === 'connecting') {
      status.value = event.code === 1008 ? 'error' : 'closed'
    }
    if (!message.value) {
      message.value =
        event.code === 1008 ? '没有权限或实例未就绪' : '终端会话已结束'
    }
  }
}

function teardown(): void {
  disposed = true
  observer?.disconnect()
  observer = null
  socket?.close()
  socket = null
  terminal?.dispose()
  terminal = null
  fit = null
}

onMounted(() => {
  const term = ensureTerminal()
  if (host.value) term.open(host.value)

  // A hidden tab reports zero size, so refit only once layout is real.
  observer = new ResizeObserver(() => fitAndReport())
  if (host.value) observer.observe(host.value)

  if (props.active !== false) connect()
})

onBeforeUnmount(teardown)

watch(
  () => props.sessionKey,
  () => {
    if (props.active === false) return
    connect()
  },
)

watch(
  () => props.active,
  (now) => {
    if (now === false) {
      disconnect()
      return
    }
    // Defer one tick: the container is still being revealed.
    window.setTimeout(() => {
      if (!disposed) connect()
    }, 0)
  },
)

defineExpose({ connect, disconnect })
</script>

<style scoped lang="scss">
.wl-terminal {
  display: flex;
  flex-direction: column;
  border-radius: 8px;
  overflow: hidden;
  background: #1e1e2d;
}

.wl-terminal__bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 8px 4px 12px;
  background: #26263a;
  color: #e1e1ef;
}

.wl-terminal__state {
  font-size: 0.8125rem;
  opacity: 0.85;
}

.wl-terminal__screen {
  // A fixed height keeps fit() meaningful: with `height: auto` the addon has
  // nothing to divide by and the terminal collapses to one row.
  height: 460px;
  padding: 8px;
}
</style>
