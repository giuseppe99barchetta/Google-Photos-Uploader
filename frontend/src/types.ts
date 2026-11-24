export type NavPage =
  | 'overview'
  | 'immich'
  | 'folders'
  | 'queue'
  | 'history'
  | 'failed'
  | 'auth'
  | 'settings'
  | 'logs'
  | 'health';

export interface SessionStatus {
  authenticated: boolean;
  setup_required: boolean;
  username: string | null;
  csrf_token: string | null;
}

export interface QueueStats {
  pending: number;
  done: number;
  failed: number;
  cancelled: number;
  retrying: number;
  total: number;
  uploaded_bytes: number;
  pending_bytes: number;
  last_completed_at: number | null;
  skipped: number;
}

export interface LiveUploadItem {
  path: string;
  posix_path: string;
  filename: string;
  folder_id: number | null;
  folder_name: string | null;
  account_id?: number | null;
  account_name?: string | null;
  phase: 'queued' | 'hashing' | 'checking' | 'uploading' | 'finalizing' | 'complete' | 'skipped' | 'error';
  bytes_completed: number;
  bytes_total: number;
  percent: number | null;
  speed_bps: number | null;
  eta_seconds: number | null;
  started_at: number;
  updated_at: number;
}

export interface LiveSnapshot {
  active_count: number;
  active_uploads: LiveUploadItem[];
  batch_items: LiveUploadItem[];
  batch: {
    in_progress: boolean;
    size: number;
    threads: number;
    account_id?: number | null;
    account_name?: string | null;
    started_at: number | null;
  };
  aggregate_speed_bps: number | null;
}

export interface GoogleAccount {
  id: number;
  name: string;
  slug: string;
  account_hint: string;
  source: string;
  status: 'not_configured' | 'validating' | 'valid' | 'invalid' | 'expired' | 'error';
  is_primary: boolean;
  configured: boolean;
  has_managed_override?: boolean;
  secret_file_mounted?: boolean;
  secret_file_readonly?: boolean;
  last_validated_at: number | null;
  last_error: string | null;
  folder_count: number;
  pending_count: number;
  uploaded_count: number;
  failed_count: number;
  created_at: number;
  updated_at: number;
}

export interface MonitoredFolder {
  id: number;
  name: string;
  path: string;
  account_id: number | null;
  account_name?: string | null;
  account_hint?: string | null;
  account_status?: string | null;
  enabled: boolean;
  paused: boolean;
  recursive: boolean;
  extensions: string[];
  exclude_dirs: string[];
  exclude_patterns: string[];
  min_file_size: number;
  max_file_size: number | null;
  last_scan_at: number | null;
  last_detected_at: number | null;
  discovered_count: number;
  health: 'healthy' | 'unavailable' | 'paused' | 'disabled' | 'unknown';
  last_error: string | null;
  pending_count: number;
  uploaded_count: number;
  failed_count: number;
  created_at: number;
  updated_at: number;
}

export interface DirectoryEntry {
  path: string;
  name: string;
  exists: boolean;
  readable: boolean;
  writable?: boolean;
  is_root: boolean;
}

export interface BrowseResponse {
  current_path: string | null;
  parent_path: string | null;
  approved_roots: DirectoryEntry[];
  directories: DirectoryEntry[];
}

export interface QueueJob {
  path: string;
  filename: string;
  signature: string;
  status: 'pending' | 'done' | 'failed' | 'cancelled';
  media_key: string | null;
  attempts: number;
  next_attempt: number;
  last_error: string | null;
  folder_id: number | null;
  folder_name: string | null;
  account_id?: number | null;
  account_name?: string | null;
  account_hint?: string | null;
  file_size: number;
  file_ext: string;
  created_at: number;
  updated_at: number;
  last_attempt_at: number | null;
  completed_at: number | null;
  upload_duration: number | null;
  result_status: string | null;
  live?: LiveUploadItem | null;
}

export interface HistoryItem {
  id: number;
  path: string;
  filename: string;
  folder_id: number | null;
  folder_name: string | null;
  account_id?: number | null;
  account_name?: string | null;
  file_size: number;
  file_ext: string;
  outcome: 'uploaded' | 'skipped' | 'failed';
  media_key: string | null;
  duration: number | null;
  error: string | null;
  created_at: number;
}

export interface ActivityLogItem {
  id: number;
  level: 'info' | 'warning' | 'error';
  category: string;
  event_type: string;
  message: string;
  details: Record<string, unknown> | null;
  created_at: number;
}

export interface NotificationItem {
  id: number;
  issue_key: string;
  severity: 'info' | 'success' | 'warning' | 'error';
  category: string;
  title: string;
  message: string;
  read: boolean;
  resolved: boolean;
  created_at: number;
  updated_at: number;
}

export interface AuthStatus {
  account_id?: number;
  name?: string;
  status: 'not_configured' | 'validating' | 'valid' | 'invalid' | 'expired' | 'error';
  configured: boolean;
  account_hint: string;
  source: string;
  has_managed_override: boolean;
  secret_file_mounted: boolean;
  secret_file_readonly: boolean;
  last_validated_at: number | null;
  last_error: string | null;
  accounts_count?: number;
  valid_accounts_count?: number;
}

export interface AppSettings {
  upload_threads: number;
  upload_batch_size: number;
  scan_interval: number;
  stable_seconds: number;
  stability_timeout: number;
  default_recursive: boolean;
  upload_quality: 'original' | 'saver';
  use_quota: boolean;
  duplicate_detection: boolean;
  filename_conflict_policy: 'hash_dedup' | 'skip_existing_filename';
  delete_after_upload: boolean;
  max_attempts: number;
  retry_base: number;
  retry_max: number;
  auto_retry_failed: boolean;
  notify_upload_completed: boolean;
  notify_upload_failed: boolean;
  notify_auth_issues: boolean;
  notify_folder_unavailable: boolean;
  notify_low_disk: boolean;
  notify_watcher_stopped: boolean;
  low_disk_threshold_gb: number;
}

export interface OverviewResponse {
  stats: QueueStats;
  queue_paused: boolean;
  live: LiveSnapshot;
  upload_threads: number;
  upload_batch_size: number;
  folders: MonitoredFolder[];
  accounts?: GoogleAccount[];
  auth: AuthStatus;
  watcher: { active_watchers: number; configured_watchers: number; healthy: boolean };
  recent_history: HistoryItem[];
  recent_activity: ActivityLogItem[];
  notifications: NotificationItem[];
  unread_notifications: number;
}

export interface SystemHealth {
  status: 'healthy' | 'degraded';
  app_version: string;
  uptime_seconds: number;
  backend: { status: string; running: boolean };
  uploader: { status: string; worker_alive: boolean; queue_paused: boolean };
  auth: AuthStatus;
  accounts?: GoogleAccount[];
  watcher: {
    active_watchers: number;
    configured_watchers: number;
    healthy: boolean;
    supervisor_alive: boolean;
    folders: Array<{
      id: number;
      name: string;
      path: string;
      account_id?: number | null;
      account_name?: string | null;
      health: string;
      enabled: boolean;
      paused: boolean;
      last_scan_at: number | null;
      last_error: string | null;
    }>;
  };
  database: { ok: boolean; schema_version: number; size_bytes: number };
  disks: Array<{
    label: string;
    path: string;
    available: boolean;
    total_bytes: number;
    used_bytes: number;
    free_bytes: number;
    free_percent: number;
    low_disk: boolean;
  }>;
  upload_slots: {
    active: number;
    max_threads: number;
    available: number;
    batch_size: number;
  };
  last_successful_upload_at: number | null;
  last_successful_scan_at: number | null;
}

export interface ImmichStatus {
  configured: boolean;
  url: string;
  public_url: string;
  reachable: boolean;
  version: string | null;
  has_api_key: boolean;
  api_key_valid: boolean | null;
  admin_access: boolean;
  media_root: string;
  media_root_exists: boolean;
  media_root_readable: boolean;
  has_upload_dir: boolean;
  has_library_dir: boolean;
  error: string | null;
  approved_roots: string[];
}

export interface ImmichUser {
  id: string;
  name: string;
  email: string;
  is_admin: boolean;
  status: string;
  storage_label: string | null;
  quota_usage_bytes: number | null;
  recommended_path: string | null;
  folder_exists: boolean;
  storage_mode: 'library' | 'upload' | null;
  candidate_paths: Array<{ path: string; mode: string; exists: boolean }>;
  bound_folder: MonitoredFolder | null;
}

export interface ImmichDiskFolder {
  label: string;
  path: string;
  storage_mode: 'library' | 'upload';
  bound_folder: MonitoredFolder | null;
}

export interface ImmichUsersResponse {
  users: ImmichUser[];
  disk_folders: ImmichDiskFolder[];
  admin_access: boolean;
  api_connected: boolean;
  message: string | null;
  accounts: GoogleAccount[];
}
