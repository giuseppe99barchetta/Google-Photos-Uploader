import { useEffect, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  FolderSync,
  HardDriveUpload,
  KeyRound,
  RefreshCw,
} from 'lucide-react';
import { api } from '../lib/api';
import { formatBytes, formatDuration, formatRelativeTime } from '../lib/utils';
import type { LiveSnapshot, NavPage, OverviewResponse } from '../types';
import { LiveUploadMonitor } from '../components/LiveUploadMonitor';
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  EmptyState,
  Skeleton,
} from '../components/ui';

export function OverviewPage({
  liveOverride,
  refreshSignal,
  onNavigate,
}: {
  liveOverride: LiveSnapshot | null;
  refreshSignal: number;
  onNavigate: (page: NavPage) => void;
}) {
  const [data, setData] = useState<OverviewResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    try {
      setError(null);
      const res = await api.overview();
      setData(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load overview');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [refreshSignal]);

  if (loading && !data) {
    return (
      <div className="space-y-6">
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[1, 2, 3, 4].map((i) => (
            <Skeleton key={i} className="h-28" />
          ))}
        </div>
        <Skeleton className="h-56" />
      </div>
    );
  }

  if (error && !data) {
    return (
      <Card className="p-6 text-center">
        <p className="text-sm text-destructive">{error}</p>
        <Button className="mt-3" variant="outline" onClick={() => void load()}>
          <RefreshCw className="h-4 w-4" />
          Retry
        </Button>
      </Card>
    );
  }

  if (!data) return null;

  const live = liveOverride || data.live;
  const authVariant =
    data.auth.status === 'valid'
      ? 'success'
      : data.auth.status === 'not_configured'
        ? 'warning'
        : 'destructive';

  return (
    <div className="space-y-6">
      {/* Status Banner when Auth needs attention */}
      {data.auth.status !== 'valid' && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4 text-sm">
          <div className="flex items-center gap-2.5">
            <KeyRound className="h-5 w-5 text-amber-600 dark:text-amber-400" />
            <div>
              <p className="font-semibold">Google Photos Authentication: {data.auth.status}</p>
              <p className="text-xs text-muted-foreground">
                {data.auth.last_error || 'Verify or update Android AUTH_DATA to keep uploads running.'}
              </p>
            </div>
          </div>
          <Button size="sm" onClick={() => onNavigate('auth')}>
            Open Authentication
          </Button>
        </div>
      )}

      {/* KPI Row */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Active Uploads</CardTitle>
            <HardDriveUpload className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {live.active_count} <span className="text-sm font-normal text-muted-foreground">/ {data.upload_threads}</span>
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              Batch size: {data.upload_batch_size} &middot; {data.queue_paused ? 'Paused' : 'Running'}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Pending Queue</CardTitle>
            <Clock className="h-4 w-4 text-sky-500" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{data.stats.pending.toLocaleString()}</div>
            <p className="mt-1 text-xs text-muted-foreground">
              {formatBytes(data.stats.pending_bytes)} waiting
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Uploaded Media</CardTitle>
            <CheckCircle2 className="h-4 w-4 text-emerald-500" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">{data.stats.done.toLocaleString()}</div>
            <p className="mt-1 text-xs text-muted-foreground">
              {formatBytes(data.stats.uploaded_bytes)} &middot; {data.stats.skipped.toLocaleString()} duplicates skipped
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-sm font-medium text-muted-foreground">Failed / Retrying</CardTitle>
            <AlertTriangle className="h-4 w-4 text-amber-500" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {data.stats.failed.toLocaleString()}
              {data.stats.retrying > 0 && (
                <span className="ml-1.5 text-sm font-normal text-amber-600 dark:text-amber-400">
                  (+{data.stats.retrying} retrying)
                </span>
              )}
            </div>
            <div className="mt-1 flex flex-wrap items-center gap-1.5">
              <Badge variant={authVariant}>
                Accounts: {data.auth.valid_accounts_count ?? (data.auth.status === 'valid' ? 1 : 0)}/
                {data.auth.accounts_count ?? (data.auth.configured ? 1 : 0)} valid
              </Badge>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Live Upload Monitor */}
      <LiveUploadMonitor
        live={live}
        stats={data.stats}
        queuePaused={data.queue_paused}
        maxThreads={data.upload_threads}
      />

      {/* Folders + Recent Activity */}
      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle>Monitored Folders</CardTitle>
              <CardDescription>
                {data.watcher.active_watchers} of {data.watcher.configured_watchers} watchers active
              </CardDescription>
            </div>
            <Button variant="outline" size="sm" onClick={() => onNavigate('folders')}>
              Manage
            </Button>
          </CardHeader>
          <CardContent>
            {data.folders.length === 0 ? (
              <EmptyState
                icon={<FolderSync className="h-5 w-5" />}
                title="No folders configured"
                description="Add an approved mounted folder to begin automatic detection and upload."
                action={
                  <Button size="sm" onClick={() => onNavigate('folders')}>
                    Add Folder
                  </Button>
                }
              />
            ) : (
              <div className="space-y-2.5">
                {data.folders.map((folder) => (
                  <div
                    key={folder.id}
                    className="flex items-center justify-between gap-3 rounded-lg border p-3"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{folder.name}</p>
                      <p className="truncate font-mono text-xs text-muted-foreground">
                        {folder.path}
                        {folder.account_name ? ` → ${folder.account_name}` : ''}
                      </p>
                    </div>
                    <div className="flex shrink-0 items-center gap-2 text-xs">
                      <span className="text-muted-foreground">
                        {folder.uploaded_count} done / {folder.pending_count} pending
                      </span>
                      <Badge
                        variant={
                          folder.health === 'healthy'
                            ? 'success'
                            : folder.health === 'unavailable'
                              ? 'destructive'
                              : 'warning'
                        }
                      >
                        {folder.health}
                      </Badge>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <div>
              <CardTitle>Recent Uploads</CardTitle>
              <CardDescription>Latest confirmed uploads, duplicates, and attempts</CardDescription>
            </div>
            <Button variant="outline" size="sm" onClick={() => onNavigate('history')}>
              View All
            </Button>
          </CardHeader>
          <CardContent>
            {data.recent_history.length === 0 ? (
              <EmptyState
                icon={<CheckCircle2 className="h-5 w-5" />}
                title="No upload history yet"
                description="Completed uploads and skipped remote duplicates will appear here."
              />
            ) : (
              <div className="divide-y">
                {data.recent_history.map((item) => (
                  <div key={item.id} className="flex items-center justify-between gap-3 py-2.5 text-sm">
                    <div className="min-w-0">
                      <p className="truncate font-medium">{item.filename}</p>
                      <p className="text-xs text-muted-foreground">
                        {formatBytes(item.file_size)} &middot; {formatDuration(item.duration)} &middot;{' '}
                        {formatRelativeTime(item.created_at)}
                      </p>
                    </div>
                    <Badge
                      variant={
                        item.outcome === 'uploaded'
                          ? 'success'
                          : item.outcome === 'skipped'
                            ? 'secondary'
                            : 'destructive'
                      }
                    >
                      {item.outcome}
                    </Badge>
                  </div>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
