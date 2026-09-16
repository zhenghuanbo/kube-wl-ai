/**
 * Instance wire types.
 *
 * Mirrors `backend/app/schemas/instance.py`. Keys are exactly the camelCase the
 * API emits; nothing is renamed on the way in.
 */
export type InstanceStatus =
  | 'pending'
  | 'creating'
  | 'running'
  | 'stopping'
  | 'stopped'
  | 'failed'
  | 'deleting'
  | 'error'

export type IdeType = 'none' | 'jupyter' | 'vscode' | 'webssh'

/** Label and icon for the IDE picker; the values must match the backend enum. */
export const IDE_OPTIONS: ReadonlyArray<{ value: IdeType; title: string; icon: string }> = [
  { value: 'none', title: '无（仅 SSH）', icon: 'mdi-close-octagon-outline' },
  { value: 'jupyter', title: 'JupyterLab', icon: 'mdi-notebook-outline' },
  { value: 'vscode', title: 'VS Code Server', icon: 'mdi-microsoft-visual-studio-code' },
  { value: 'webssh', title: 'Web 终端', icon: 'mdi-console-line' },
]

export interface InstanceSummary {
  id: number
  name: string
  displayName: string
  description?: string | null
  workspaceId: number
  workspaceName?: string | null
  ownerUserId: number
  ownerUsername?: string | null
  ownerDisplayName?: string | null
  resourceGroupId?: number | null
  resourceGroupName?: string | null
  image: string
  ideType: IdeType
  status: InstanceStatus
  statusMessage?: string | null
  cpuMilli: number
  memoryMb: number
  gpuType: string
  gpuCount: number
  storageGb: number
  sshEnabled: boolean
  sshNodePort?: number | null
  nodeName?: string | null
  startedAt?: string | null
  expiresAt?: string | null
  isExpired: boolean
  isActive: boolean
  createdAt: string
  updatedAt: string
}

export interface InstanceDetail extends InstanceSummary {
  k8sDeployment?: string | null
  k8sSshService?: string | null
  k8sIdeService?: string | null
  k8sIngress?: string | null
  k8sPvc?: string | null
  podName?: string | null
  /**
   * The exact command to paste. `null` when SSH is not available — either it
   * was not enabled or the cluster has not allocated a port yet.
   */
  sshCommand?: string | null
  /** Path the IDE is served under, relative to the platform origin. */
  idePath?: string | null
  maxRuntimeSeconds?: number | null
}

export interface InstanceCreatePayload {
  name: string
  displayName?: string
  description?: string | null
  workspaceId?: number | null
  ownerUserId?: number | null
  resourceGroupId: number
  resourceSpecId: number
  image: string
  ideType?: IdeType
  storageGb?: number | null
  sshEnabled?: boolean
  maxRuntimeSeconds?: number | null
}

export interface InstanceUpdatePayload {
  displayName?: string
  description?: string | null
}

export interface InstanceLogs {
  logs: string
}

/** A short-lived, signed URL for an instance's web IDE. */
export interface IdeAccess {
  url: string
  expiresIn: number
}

export interface SshKey {
  id: number
  name: string
  publicKey: string
  fingerprint: string
  createdAt: string
}

export interface SshKeyCreatePayload {
  name: string
  publicKey: string
}
