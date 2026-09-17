/** System information endpoints (`app/api/v1/system.py`). */
import { api } from '@/api/client'
import type { SystemInfo } from '@/types/system'

/**
 * Runtime state and the configuration health check.
 *
 * Global-administrator only. There is no write counterpart: configuration comes
 * from the environment and is read once at startup, so an editing UI would
 * either show a stale value or claim a change the running process has not made.
 */
export function getSystemInfo(): Promise<SystemInfo> {
  return api.get<SystemInfo>('/system/info')
}
