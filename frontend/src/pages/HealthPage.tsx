import { useEffect, useState } from 'react';
import {
  CheckCircle2,
  Cpu,
  Database,
  FolderSync,
  HardDrive,
  HeartPulse,
  KeyRound,
  RefreshCw,
  Server,
} from 'lucide-react';
import { api } from '../lib/api';
import { formatBytes, formatDuration, formatTimestamp } from '../lib/utils';
import type { SystemHealth } from '../types';
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  Progress,
  Skeleton,
  useToast,
} from '../components/ui';

export function HealthPage({ refreshSignal }: { refreshSignal: number }) {
  const { toast } = useToast();
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [loading, setLoading] = useState(true);

  const load = async () => {
    try {
      const res = await api.systemHealth();
      setHealth(res);
    } catch (err) {
      toast({ variant: 'error', title: 'Failed to load system health', description: err instanceof Error ? err.message : undefined });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [refreshSignal]);

  if (loading && !health) {
    return (
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {[1, 2, 3, 4, 5, 6].map((i) => (
          <Skeleton key={i} className="h-36" />
        ))}
      </div>
    );
  }

  if (!health) return null;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <HeartPulse className="h-5 w-5 text-primary" />
          <Badge variant={health.status === 'healthy' ? 'success' : 'warning'}>
            System: {health.status}
          </Badge>
          <span className="text-xs text-muted-foreground">
            v{health.app_version} &middot; Uptime {formatDuration(health.uptime_seconds)}
          </span>
        </div>
        <Button variant="outline" size="sm" onClick={() => void load()}>
          <RefreshCw className="h-3.5 w-3.5" />
          Refresh Diagnostics
        </Button>
      </div>

      {/* Component Health Cards */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm">Backend & API</CardTitle>
            <Server className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent className="space-y-1 text-xs">
            <Badge variant="success">{health.backend.status}</Badge>
            <p className="text-muted-foreground">Application version: v{health.app_version}</p>
            <p className="text-muted-foreground">Last successful upload: {formatTimestamp(health.last_successful_upload_at)}</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm">Upload Worker & Slots</CardTitle>
            <Cpu className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent className="space-y-1 text-xs">
            <div className="flex gap-2">
              <Badge variant={health.uploader.worker_alive ? 'success' : 'destructive'}>
                {health.uploader.status}
              </Badge>
              {health.uploader.queue_paused && <Badge variant="warning">Paused</Badge>}
            </div>
            <p className="text-muted-foreground">
              Active uploads: <strong className="text-foreground">{health.upload_slots.active}</strong> &middot; Available slots:{' '}
              <strong className="text-foreground">{health.upload_slots.available}</strong> / {health.upload_slots.max_threads}
            </p>
            <p className="text-muted-foreground">Batch size: {health.upload_slots.batch_size} files</p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm">Google Photos Accounts</CardTitle>
            <KeyRound className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent className="space-y-1.5 text-xs">
            {health.accounts && health.accounts.length > 0 ? (
              health.accounts.map((acc) => (
                <div key={acc.id} className="flex items-center justify-between gap-2">
                  <span className="truncate font-medium">
                    {acc.name}{' '}
                    <span className="font-mono text-muted-foreground">({acc.account_hint})</span>
                  </span>
                  <Badge
                    variant={
                      acc.status === 'valid'
                        ? 'success'
                        : acc.status === 'not_configured'
                          ? 'warning'
                          : 'destructive'
                    }
                  >
                    {acc.status}
                  </Badge>
                </div>
              ))
            ) : (
              <>
                <Badge
                  variant={
                    health.auth.status === 'valid'
                      ? 'success'
                      : health.auth.status === 'not_configured'
                        ? 'warning'
                        : 'destructive'
                  }
                >
                  {health.auth.status}
                </Badge>
                <p className="text-muted-foreground">Account: {health.auth.account_hint || 'Not configured'}</p>
              </>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm">SQLite Queue Database</CardTitle>
            <Database className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent className="space-y-1 text-xs">
            <Badge variant={health.database.ok ? 'success' : 'destructive'}>
              {health.database.ok ? 'Healthy (WAL)' : 'Error'}
            </Badge>
            <p className="text-muted-foreground">Schema version: v{health.database.schema_version}</p>
            <p className="text-muted-foreground">Database size: {formatBytes(health.database.size_bytes)}</p>
          </CardContent>
        </Card>

        <Card className="sm:col-span-2">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm">Folder Watchers</CardTitle>
            <FolderSync className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent className="space-y-2 text-xs">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={health.watcher.healthy ? 'success' : 'warning'}>
                {health.watcher.active_watchers} / {health.watcher.configured_watchers} watchers active
              </Badge>
              <span className="text-muted-foreground">
                Last successful scan: {formatTimestamp(health.last_successful_scan_at)}
              </span>
            </div>
            {health.watcher.folders.length > 0 && (
              <div className="grid gap-1.5 pt-1 sm:grid-cols-2">
                {health.watcher.folders.map((f) => (
                  <div key={f.id} className="flex items-center justify-between rounded-md border px-2.5 py-1.5">
                    <span className="truncate font-medium">
                      {f.name}
                      {f.account_name ? ` → ${f.account_name}` : ''}
                    </span>
                    <Badge
                      variant={
                        f.health === 'healthy'
                          ? 'success'
                          : f.health === 'unavailable'
                            ? 'destructive'
                            : 'secondary'
                      }
                    >
                      {f.health}
                    </Badge>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Disk Space */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <HardDrive className="h-4 w-4 text-primary" />
            Storage & Disk Space
          </CardTitle>
          <CardDescription>Free capacity on persistent state storage and approved media mount points.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 md:grid-cols-2">
          {health.disks.map((disk) => {
            const usedPct = disk.total_bytes > 0 ? Math.round((disk.used_bytes / disk.total_bytes) * 100) : 0;
            return (
              <div key={disk.path} className="space-y-2 rounded-lg border p-3.5">
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{disk.label}</p>
                    <p className="truncate font-mono text-xs text-muted-foreground">{disk.path}</p>
                  </div>
                  {!disk.available ? (
                    <Badge variant="destructive">Unavailable</Badge>
                  ) : disk.low_disk ? (
                    <Badge variant="warning">Low Space</Badge>
                  ) : (
                    <Badge variant="success">
                      <CheckCircle2 className="h-3 w-3" />
                      OK
                    </Badge>
                  )}
                </div>
                {disk.available && (
                  <>
                    <Progress value={usedPct} />
                    <div className="flex justify-between text-xs text-muted-foreground">
                      <span>Free: {formatBytes(disk.free_bytes)} ({disk.free_percent}%)</span>
                      <span>Total: {formatBytes(disk.total_bytes)}</span>
                    </div>
                  </>
                )}
              </div>
            );
          })}
        </CardContent>
      </Card>
    </div>
  );
}
