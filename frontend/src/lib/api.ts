import type {
  ActivityLogItem,
  AppSettings,
  AuthStatus,
  BrowseResponse,
  DirectoryEntry,
  GoogleAccount,
  HistoryItem,
  ImmichStatus,
  ImmichUsersResponse,
  LiveSnapshot,
  MonitoredFolder,
  NotificationItem,
  OverviewResponse,
  QueueJob,
  QueueStats,
  SessionStatus,
  SystemHealth,
} from '../types';

// Stored strictly in memory; never persisted in localStorage or sessionStorage.
let csrfToken: string | null = null;
let onUnauthorizedCallback: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorizedCallback = handler;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || 'GET').toUpperCase();
  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string> | undefined),
  };
  if (options.body && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method) && csrfToken) {
    headers['X-CSRF-Token'] = csrfToken;
  }

  const res = await fetch(path, {
    ...options,
    method,
    headers,
    credentials: 'same-origin',
  });

  if (res.status === 401 && !path.startsWith('/api/session/')) {
    csrfToken = null;
    onUnauthorizedCallback?.();
  }

  const contentType = res.headers.get('content-type') || '';
  const data = contentType.includes('application/json') ? await res.json() : await res.text();

  if (!res.ok) {
    const detail =
      typeof data === 'object' && data !== null && 'detail' in data
        ? String((data as { detail: unknown }).detail)
        : typeof data === 'string' && data
          ? data
          : `Request failed (${res.status})`;
    throw new ApiError(res.status, detail);
  }

  return data as T;
}

export const api = {
  // Session
  sessionStatus: () => request<SessionStatus>('/api/session/status'),
  sessionSetup: (username: string, password: string) =>
    request<{ authenticated: boolean; username: string; csrf_token: string }>('/api/session/setup', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  sessionLogin: (username: string, password: string) =>
    request<{ authenticated: boolean; username: string; csrf_token: string }>('/api/session/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  sessionLogout: () => request<{ authenticated: boolean }>('/api/session/logout', { method: 'POST' }),
  changePassword: (current_password: string, new_password: string) =>
    request<{ updated: boolean }>('/api/session/password', {
      method: 'POST',
      body: JSON.stringify({ current_password, new_password }),
    }),

  // Overview
  overview: () => request<OverviewResponse>('/api/overview'),

  // Folders
  listFolders: () =>
    request<{ folders: MonitoredFolder[]; approved_roots: string[]; accounts: GoogleAccount[] }>('/api/folders'),
  browseFolders: (path?: string) =>
    request<BrowseResponse>(path ? `/api/folders/browse?path=${encodeURIComponent(path)}` : '/api/folders/browse'),
  addFolder: (payload: {
    name: string;
    path: string;
    enabled: boolean;
    recursive: boolean;
    extensions: string[];
    exclude_dirs: string[];
    exclude_patterns: string[];
    min_file_size: number;
    max_file_size: number | null;
    account_id?: number | null;
    create_subfolder_name?: string | null;
    immich_preset?: boolean;
  }) => request<{ folder: MonitoredFolder }>('/api/folders', { method: 'POST', body: JSON.stringify(payload) }),
  updateFolder: (id: number, payload: Partial<MonitoredFolder>) =>
    request<{ folder: MonitoredFolder }>(`/api/folders/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  removeFolder: (id: number) => request<{ removed: boolean }>(`/api/folders/${id}`, { method: 'DELETE' }),
  pauseFolder: (id: number) => request<{ folder: MonitoredFolder }>(`/api/folders/${id}/pause`, { method: 'POST' }),
  resumeFolder: (id: number) => request<{ folder: MonitoredFolder }>(`/api/folders/${id}/resume`, { method: 'POST' }),
  scanFolder: (id: number) =>
    request<{ discovered_count: number; newly_queued: number; folder: MonitoredFolder }>(`/api/folders/${id}/scan`, {
      method: 'POST',
    }),

  // Queue
  listQueue: (params: Record<string, string | number | undefined>) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== '') qs.set(k, String(v));
    });
    return request<{
      items: QueueJob[];
      total: number;
      limit: number;
      offset: number;
      queue_paused: boolean;
      stats: QueueStats;
      live: LiveSnapshot;
    }>(`/api/queue?${qs.toString()}`);
  },
  inspectJob: (path: string) => request<{ job: QueueJob }>(`/api/queue/job?path=${encodeURIComponent(path)}`),
  pauseQueue: () => request<{ queue_paused: boolean }>('/api/queue/pause', { method: 'POST' }),
  resumeQueue: () => request<{ queue_paused: boolean }>('/api/queue/resume', { method: 'POST' }),
  retryJob: (path: string) =>
    request<{ retried: boolean }>('/api/queue/retry', { method: 'POST', body: JSON.stringify({ path }) }),
  retryAll: (folder_id?: number) =>
    request<{ retried_count: number }>('/api/queue/retry-all', {
      method: 'POST',
      body: JSON.stringify({ folder_id: folder_id ?? null }),
    }),
  cancelJob: (path: string) =>
    request<{ cancelled: boolean }>('/api/queue/cancel', { method: 'POST', body: JSON.stringify({ path }) }),

  // History
  listHistory: (params: Record<string, string | number | undefined>) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== '') qs.set(k, String(v));
    });
    return request<{ items: HistoryItem[]; total: number; limit: number; offset: number }>(`/api/history?${qs.toString()}`);
  },

  // Activity logs
  listLogs: (params: Record<string, string | number | undefined>) => {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v !== undefined && v !== '') qs.set(k, String(v));
    });
    return request<{ items: ActivityLogItem[]; total: number; limit: number; offset: number }>(`/api/logs?${qs.toString()}`);
  },

  // Multi-Account Google Photos Management
  listAccounts: () =>
    request<{ accounts: GoogleAccount[]; approved_roots: DirectoryEntry[] }>('/api/accounts'),
  addAccount: (payload: {
    name: string;
    auth_data: string;
    create_dedicated_folder?: boolean;
    parent_root?: string | null;
    bind_folder_path?: string | null;
    folder_name?: string | null;
    immich_preset?: boolean;
  }) =>
    request<{
      valid: boolean;
      saved: boolean;
      status: string;
      account_hint: string;
      message: string;
      account: GoogleAccount;
      folder?: MonitoredFolder | null;
      folder_warning?: string | null;
    }>('/api/accounts', { method: 'POST', body: JSON.stringify(payload) }),
  renameAccount: (id: number, name: string) =>
    request<{ account: GoogleAccount }>(`/api/accounts/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ name }),
    }),
  rotateAccountCredentials: (id: number, auth_data: string) =>
    request<{
      valid: boolean;
      saved: boolean;
      status: string;
      account_hint: string;
      message: string;
      account: GoogleAccount;
    }>(`/api/accounts/${id}/credentials`, {
      method: 'POST',
      body: JSON.stringify({ auth_data }),
    }),
  revalidateAccount: (id: number) =>
    request<{ valid: boolean; status: string; message: string; account: GoogleAccount }>(
      `/api/accounts/${id}/revalidate`,
      { method: 'POST' }
    ),
  removeAccount: (id: number, force = false) =>
    request<{ removed: boolean; unbound_folders: number }>(
      `/api/accounts/${id}?force=${force ? 'true' : 'false'}`,
      { method: 'DELETE' }
    ),

  // Android Authentication (Primary shortcuts)
  authStatus: () => request<AuthStatus>('/api/auth/status'),
  validateAuth: (auth_data: string) =>
    request<{ valid: boolean; status: string; account_hint: string; message: string }>('/api/auth/validate', {
      method: 'POST',
      body: JSON.stringify({ auth_data }),
    }),
  saveAuth: (auth_data: string) =>
    request<{ valid: boolean; saved: boolean; status: string; account_hint: string; message: string; auth_status: AuthStatus }>(
      '/api/auth/save',
      { method: 'POST', body: JSON.stringify({ auth_data }) }
    ),
  revalidateAuth: () =>
    request<{ valid: boolean; status: string; message: string; auth_status: AuthStatus }>('/api/auth/revalidate', {
      method: 'POST',
    }),

  // Settings
  getSettings: () =>
    request<{ settings: AppSettings; watcher: { active_watchers: number; configured_watchers: number; healthy: boolean } }>(
      '/api/settings'
    ),
  updateSettings: (patch: Partial<AppSettings>) =>
    request<{ settings: AppSettings }>('/api/settings', { method: 'PUT', body: JSON.stringify(patch) }),

  // Notifications
  listNotifications: (includeResolved = false) =>
    request<{ items: NotificationItem[]; unread_count: number }>(
      `/api/notifications?include_resolved=${includeResolved}`
    ),
  markNotificationRead: (id: number) => request<{ ok: boolean }>(`/api/notifications/${id}/read`, { method: 'POST' }),
  markAllNotificationsRead: () => request<{ updated: number }>('/api/notifications/read-all', { method: 'POST' }),
  dismissNotification: (id: number) => request<{ ok: boolean }>(`/api/notifications/${id}/dismiss`, { method: 'POST' }),

  // Immich Integration
  immichStatus: () => request<ImmichStatus>('/api/immich/status'),
  updateImmichConfig: (payload: {
    url?: string;
    public_url?: string;
    media_root?: string;
    api_key?: string;
    clear_api_key?: boolean;
  }) => request<ImmichStatus>('/api/immich/config', { method: 'PUT', body: JSON.stringify(payload) }),
  immichUsers: () => request<ImmichUsersResponse>('/api/immich/users'),
  createImmichUser: (payload: {
    name: string;
    email: string;
    password: string;
    storage_label?: string | null;
    is_admin?: boolean;
    account_id?: number | null;
    auto_bind?: boolean;
  }) =>
    request<{ user: ImmichUsersResponse['users'][number]; folder?: MonitoredFolder | null; bind_warning?: string | null }>(
      '/api/immich/users',
      { method: 'POST', body: JSON.stringify(payload) }
    ),
  bindImmichFolder: (payload: { path: string; account_id: number; folder_name: string }) =>
    request<{ folder: MonitoredFolder; updated_existing: boolean }>('/api/immich/bind', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // Health
  systemHealth: () => request<SystemHealth>('/api/health'),
};
