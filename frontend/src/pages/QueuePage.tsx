import { useEffect, useState } from 'react';
import {
  Ban,
  Eye,
  ListOrdered,
  Pause,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
} from 'lucide-react';
import { api } from '../lib/api';
import { formatBytes, formatRelativeTime, formatSpeed, formatTimestamp } from '../lib/utils';
import type { GoogleAccount, LiveSnapshot, MonitoredFolder, QueueJob, QueueStats } from '../types';
import { LiveUploadMonitor } from '../components/LiveUploadMonitor';
import {
  Badge,
  Button,
  Card,
  CardContent,
  ConfirmDialog,
  EmptyState,
  Input,
  Modal,
  Progress,
  Select,
  Skeleton,
  useToast,
} from '../components/ui';

export function QueuePage({
  liveOverride,
  refreshSignal,
  maxThreads,
}: {
  liveOverride: LiveSnapshot | null;
  refreshSignal: number;
  maxThreads: number;
}) {
  const { toast } = useToast();
  const [jobs, setJobs] = useState<QueueJob[]>([]);
  const [folders, setFolders] = useState<MonitoredFolder[]>([]);
  const [accounts, setAccounts] = useState<GoogleAccount[]>([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState<QueueStats | null>(null);
  const [live, setLive] = useState<LiveSnapshot | null>(null);
  const [paused, setPaused] = useState(false);
  const [loading, setLoading] = useState(true);

  const [search, setSearch] = useState('');
  const [folderId, setFolderId] = useState('');
  const [accountId, setAccountId] = useState('');
  const [fileType, setFileType] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [sortBy, setSortBy] = useState('created_at');
  const [sortDir, setSortDir] = useState('desc');
  const [offset, setOffset] = useState(0);
  const limit = 30;

  const [inspecting, setInspecting] = useState<QueueJob | null>(null);
  const [cancelling, setCancelling] = useState<QueueJob | null>(null);

  const load = async () => {
    try {
      const [queueRes, folderRes] = await Promise.all([
        api.listQueue({
          search,
          folder_id: folderId ? Number(folderId) : undefined,
          account_id: accountId ? Number(accountId) : undefined,
          file_type: fileType,
          status: statusFilter,
          sort_by: sortBy,
          sort_dir: sortDir,
          limit,
          offset,
        }),
        api.listFolders(),
      ]);
      setJobs(queueRes.items);
      setTotal(queueRes.total);
      setStats(queueRes.stats);
      setLive(queueRes.live);
      setPaused(queueRes.queue_paused);
      setFolders(folderRes.folders);
      setAccounts(folderRes.accounts || []);
    } catch (err) {
      toast({ variant: 'error', title: 'Failed to load queue', description: err instanceof Error ? err.message : undefined });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [search, folderId, accountId, fileType, statusFilter, sortBy, sortDir, offset, refreshSignal]);

  const effectiveLive = liveOverride || live;

  const handleTogglePause = async () => {
    try {
      if (paused) {
        await api.resumeQueue();
        toast({ variant: 'success', title: 'Upload queue resumed' });
      } else {
        await api.pauseQueue();
        toast({ variant: 'info', title: 'Upload queue paused', description: 'In-flight batches finish safely.' });
      }
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Action failed', description: err instanceof Error ? err.message : undefined });
    }
  };

  const handleRetryAll = async () => {
    try {
      const res = await api.retryAll(folderId ? Number(folderId) : undefined);
      toast({ variant: 'success', title: `Requeued ${res.retried_count} job(s)` });
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Retry failed', description: err instanceof Error ? err.message : undefined });
    }
  };

  const handleRetryOne = async (job: QueueJob) => {
    try {
      await api.retryJob(job.path);
      toast({ variant: 'success', title: `Requeued ${job.filename}` });
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Retry failed', description: err instanceof Error ? err.message : undefined });
    }
  };

  const handleCancel = async () => {
    if (!cancelling) return;
    try {
      await api.cancelJob(cancelling.path);
      toast({
        variant: 'info',
        title: `Cancelled ${cancelling.filename}`,
        description: 'Local file on disk remains untouched.',
      });
      setCancelling(null);
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Cancel failed', description: err instanceof Error ? err.message : undefined });
    }
  };

  const statusBadge = (job: QueueJob) => {
    if (job.live) return <Badge variant="default">{job.live.phase}</Badge>;
    if (job.status === 'done') return <Badge variant="success">done</Badge>;
    if (job.status === 'failed') return <Badge variant="destructive">failed</Badge>;
    if (job.status === 'cancelled') return <Badge variant="secondary">cancelled</Badge>;
    if (job.attempts > 0) return <Badge variant="warning">retrying</Badge>;
    return <Badge variant="info">pending</Badge>;
  };

  return (
    <div className="space-y-6">
      {effectiveLive && (
        <LiveUploadMonitor
          live={effectiveLive}
          stats={stats}
          queuePaused={paused}
          maxThreads={maxThreads}
        />
      )}

      {/* Controls & Filters */}
      <Card>
        <CardContent className="space-y-4 pt-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap gap-2">
              <Button variant={paused ? 'default' : 'outline'} size="sm" onClick={() => void handleTogglePause()}>
                {paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
                {paused ? 'Resume Queue' : 'Pause Queue'}
              </Button>
              <Button variant="secondary" size="sm" onClick={() => void handleRetryAll()}>
                <RotateCcw className="h-3.5 w-3.5" />
                Retry All Failed
              </Button>
              <Button variant="ghost" size="sm" onClick={() => void load()}>
                <RefreshCw className="h-3.5 w-3.5" />
                Refresh
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">{total.toLocaleString()} total jobs matching filter</p>
          </div>

          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-6">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                value={search}
                onChange={(e) => {
                  setOffset(0);
                  setSearch(e.target.value);
                }}
                placeholder="Search filename..."
                className="pl-8"
              />
            </div>

            <Select
              value={accountId}
              onChange={(e) => {
                setOffset(0);
                setAccountId(e.target.value);
              }}
            >
              <option value="">All accounts</option>
              {accounts.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name}
                </option>
              ))}
            </Select>

            <Select
              value={folderId}
              onChange={(e) => {
                setOffset(0);
                setFolderId(e.target.value);
              }}
            >
              <option value="">All folders</option>
              {folders.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.name}
                </option>
              ))}
            </Select>

            <Select
              value={fileType}
              onChange={(e) => {
                setOffset(0);
                setFileType(e.target.value);
              }}
            >
              <option value="">All file types</option>
              {['.jpg', '.jpeg', '.png', '.heic', '.mp4', '.mov', '.mkv'].map((ext) => (
                <option key={ext} value={ext}>
                  {ext.toUpperCase()}
                </option>
              ))}
            </Select>

            <Select
              value={statusFilter}
              onChange={(e) => {
                setOffset(0);
                setStatusFilter(e.target.value);
              }}
            >
              <option value="">All statuses</option>
              <option value="pending">Pending</option>
              <option value="retrying">Retrying</option>
              <option value="failed">Failed</option>
              <option value="done">Done</option>
              <option value="cancelled">Cancelled</option>
            </Select>

            <Select
              value={`${sortBy}:${sortDir}`}
              onChange={(e) => {
                const [col, dir] = e.target.value.split(':');
                setSortBy(col);
                setSortDir(dir);
              }}
            >
              <option value="created_at:desc">Newest first</option>
              <option value="created_at:asc">Oldest first</option>
              <option value="file_size:desc">Largest first</option>
              <option value="file_size:asc">Smallest first</option>
            </Select>
          </div>
        </CardContent>
      </Card>

      {/* Queue Table */}
      <Card>
        <CardContent className="p-0">
          {loading ? (
            <div className="space-y-2 p-5">
              {[1, 2, 3, 4].map((i) => (
                <Skeleton key={i} className="h-12 w-full" />
              ))}
            </div>
          ) : jobs.length === 0 ? (
            <div className="p-6">
              <EmptyState
                icon={<ListOrdered className="h-6 w-6" />}
                title="No jobs match your filters"
                description="Drop supported media into a monitored folder or adjust your search criteria."
              />
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b bg-muted/40 text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3 font-medium">File</th>
                    <th className="px-4 py-3 font-medium">Folder</th>
                    <th className="px-4 py-3 font-medium">Size</th>
                    <th className="px-4 py-3 font-medium">Status</th>
                    <th className="px-4 py-3 font-medium">Progress / Speed</th>
                    <th className="px-4 py-3 font-medium">Retries</th>
                    <th className="px-4 py-3 font-medium">Added / Attempt</th>
                    <th className="px-4 py-3 font-medium text-right">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {jobs.map((job) => {
                    const liveItem = effectiveLive?.active_uploads.find((a) => a.path === job.path) || job.live;
                    return (
                      <tr key={job.path} className="hover:bg-muted/30">
                        <td className="max-w-xs px-4 py-3">
                          <p className="truncate font-medium" title={job.path}>
                            {job.filename}
                          </p>
                          <p className="text-xs text-muted-foreground uppercase">
                            {job.file_ext.replace('.', '') || 'media'}
                          </p>
                        </td>
                        <td className="px-4 py-3 text-xs text-muted-foreground">
                          <div>{job.folder_name || '—'}</div>
                          {job.account_name && (
                            <div className="text-[11px] text-primary">{job.account_name}</div>
                          )}
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 text-xs">
                          {formatBytes(job.file_size)}
                        </td>
                        <td className="px-4 py-3">
                          <div className="space-y-1">
                            {statusBadge({ ...job, live: liveItem })}
                            {job.last_error && (
                              <p className="max-w-[180px] truncate text-xs text-destructive" title={job.last_error}>
                                {job.last_error}
                              </p>
                            )}
                          </div>
                        </td>
                        <td className="min-w-[140px] px-4 py-3 text-xs">
                          {liveItem ? (
                            <div className="space-y-1">
                              <Progress value={liveItem.percent} />
                              <span className="text-muted-foreground">
                                {liveItem.percent != null ? `${liveItem.percent.toFixed(0)}%` : liveItem.phase}{' '}
                                {liveItem.speed_bps != null ? `· ${formatSpeed(liveItem.speed_bps)}` : ''}
                              </span>
                            </div>
                          ) : job.status === 'done' ? (
                            <span className="text-emerald-600 dark:text-emerald-400">100%</span>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                        <td className="px-4 py-3 text-xs">{job.attempts}</td>
                        <td className="whitespace-nowrap px-4 py-3 text-xs text-muted-foreground">
                          <div>Added {formatRelativeTime(job.created_at)}</div>
                          {job.last_attempt_at && <div>Last {formatRelativeTime(job.last_attempt_at)}</div>}
                        </td>
                        <td className="whitespace-nowrap px-4 py-3 text-right">
                          <div className="flex justify-end gap-1">
                            <Button
                              variant="ghost"
                              size="icon"
                              onClick={() => setInspecting(job)}
                              aria-label="Inspect job"
                            >
                              <Eye className="h-4 w-4" />
                            </Button>
                            {(job.status === 'failed' || job.status === 'cancelled' || job.attempts > 0) && (
                              <Button
                                variant="ghost"
                                size="icon"
                                onClick={() => void handleRetryOne(job)}
                                aria-label="Retry job"
                              >
                                <RotateCcw className="h-4 w-4" />
                              </Button>
                            )}
                            {job.status === 'pending' && !job.media_key && (
                              <Button
                                variant="ghost"
                                size="icon"
                                onClick={() => setCancelling(job)}
                                aria-label="Cancel job"
                              >
                                <Ban className="h-4 w-4 text-destructive" />
                              </Button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {total > limit && (
            <div className="flex items-center justify-between border-t px-4 py-3 text-xs">
              <span className="text-muted-foreground">
                Showing {offset + 1}–{Math.min(offset + limit, total)} of {total}
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - limit))}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={offset + limit >= total}
                  onClick={() => setOffset(offset + limit)}
                >
                  Next
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Inspect Job Modal */}
      <Modal
        open={Boolean(inspecting)}
        onClose={() => setInspecting(null)}
        title={inspecting?.filename || 'Job Details'}
      >
        {inspecting && (
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <div className="col-span-2">
              <dt className="text-xs text-muted-foreground">Container Path</dt>
              <dd className="break-all font-mono text-xs">{inspecting.path}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Status</dt>
              <dd className="font-medium">{inspecting.status}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">File Size</dt>
              <dd>{formatBytes(inspecting.file_size)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Retry Attempts</dt>
              <dd>{inspecting.attempts}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Next Attempt</dt>
              <dd>{inspecting.next_attempt ? formatTimestamp(inspecting.next_attempt) : 'Immediate'}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Added</dt>
              <dd>{formatTimestamp(inspecting.created_at)}</dd>
            </div>
            <div>
              <dt className="text-xs text-muted-foreground">Completed</dt>
              <dd>{formatTimestamp(inspecting.completed_at)}</dd>
            </div>
            {inspecting.media_key && (
              <div className="col-span-2">
                <dt className="text-xs text-muted-foreground">Google Photos Media Key</dt>
                <dd className="break-all font-mono text-xs">{inspecting.media_key}</dd>
              </div>
            )}
            {inspecting.last_error && (
              <div className="col-span-2">
                <dt className="text-xs text-muted-foreground">Last Error</dt>
                <dd className="rounded-md bg-destructive/10 p-2 font-mono text-xs text-destructive">
                  {inspecting.last_error}
                </dd>
              </div>
            )}
          </dl>
        )}
      </Modal>

      <ConfirmDialog
        open={Boolean(cancelling)}
        onClose={() => setCancelling(null)}
        onConfirm={() => void handleCancel()}
        title="Cancel Queued Upload?"
        description={`Cancel "${cancelling?.filename}" from the upload queue? Your local file on disk will NOT be deleted, and you can retry it later.`}
        confirmLabel="Cancel Job"
        destructive
      />
    </div>
  );
}
