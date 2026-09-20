import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'
import vuetify from 'vite-plugin-vuetify'

/** Backend origin used by the dev server proxy. */
const devProxyTarget = process.env.VITE_DEV_PROXY_TARGET ?? 'http://localhost:8000'

/**
 * Dev-server exposure, off by default.
 *
 * `npm run dev` binds loopback unless `VITE_DEV_HOST` says otherwise. That
 * default is load-bearing: this repository is developed on machines that are
 * themselves cluster nodes, and a dev server that silently listens on every
 * interface is a dev server somebody else finds first. Reaching it from another
 * host is a deliberate act:
 *
 *     VITE_DEV_HOST=0.0.0.0 npm run dev
 *
 * Then browse to `http://<this-host>:5173`. Only this one port needs to be
 * reachable — `/api` and `/ws` are proxied server-side (below), so the backend
 * stays on loopback and no CORS entry is involved.
 */
const devHost = process.env.VITE_DEV_HOST ?? '127.0.0.1'

/**
 * Hostnames Vite will answer to, for access through something that is *not* an
 * IP address — a tunnel, an ingress, a cloud dev proxy.
 *
 * Vite rejects a request whose `Host` header it does not recognise, as
 * DNS-rebinding protection. `localhost` and bare IP addresses are allowed
 * already, so `http://10.0.0.5:5173` works with no configuration. A hostname
 * gets a 403 that looks like a broken app rather than a dev-server policy, which
 * is why this is worth wiring up explicitly:
 *
 *     VITE_ALLOWED_HOSTS=dev.example.edu,mybox.internal npm run dev
 *
 * Deliberately not `allowedHosts: true`. Disabling the check outright is the
 * usual advice and it is the wrong default here: it would let any page that
 * resolves to this machine reach the dev server, which is exactly what the check
 * exists to prevent.
 */
const allowedHosts = (process.env.VITE_ALLOWED_HOSTS ?? '')
  .split(',')
  .map((host) => host.trim())
  .filter(Boolean)

/**
 * HMR over a terminating proxy.
 *
 * When the browser reaches the dev server through an HTTPS tunnel, Vite's client
 * still tries to open `ws://<host>:5173` — the wrong scheme and usually the wrong
 * port — and the page loads but never hot-reloads, with only a console error to
 * explain it. These three let the socket be pointed at the public endpoint.
 *
 * Left undefined, Vite's defaults apply and HMR works directly (including over
 * an SSH tunnel, where the socket is same-origin and nothing needs overriding).
 */
const hmrHost = process.env.VITE_HMR_HOST
const hmrClientPort = process.env.VITE_HMR_CLIENT_PORT
const hmrProtocol = process.env.VITE_HMR_PROTOCOL

const hmr =
  hmrHost || hmrClientPort || hmrProtocol
    ? {
        ...(hmrHost ? { host: hmrHost } : {}),
        ...(hmrClientPort ? { clientPort: Number(hmrClientPort) } : {}),
        ...(hmrProtocol ? { protocol: hmrProtocol as 'ws' | 'wss' } : {}),
      }
    : undefined

export default defineConfig({
  plugins: [
    vue(),
    // Tree-shakes Vuetify components and directives.
    vuetify({ autoImport: true }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    host: devHost,
    allowedHosts,
    hmr,
    proxy: {
      // Same-origin in production (nginx); proxied here for local development.
      // The proxy runs in this process, so the backend never needs to be
      // reachable from the browser.
      '/api': { target: devProxyTarget, changeOrigin: true },
      '/ws': { target: devProxyTarget, changeOrigin: true, ws: true },
    },
  },
  build: {
    target: 'es2022',
    sourcemap: true,
    chunkSizeWarningLimit: 1200,
  },
})
