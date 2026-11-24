import {
  Activity,
  CheckCircle2,
  Clock,
  FileSearch,
  Gauge,
  Hash,
  SkipForward,
  Sparkles,
  Upload,
  XCircle,
} from 'lucide-react';
import type { LiveSnapshot, LiveUploadItem, QueueStats } from '../types';
import { formatBytes, formatDuration, formatSpeed } from '../lib/utils';
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Progress } from './ui';

function phaseMeta(phase: LiveUploadItem['phase']) {
  switch (phase) {
    case 'hashing':
      return { label: 'Hashing', variant: 'info' as const, icon: <Hash className="h-3 w-3" /> };
    case 'checking':
      return { label: 'Checking', variant: 'info' as const, icon: <FileSearch className="h-3 w-3" /> };
    case 'uploading':
      return { label: 'Uploading', variant: 'default' as const, icon: <Upload className="h-3 w-3" /> };
    case 'finalizing':
      return { label: 'Finalizing', variant: 'info' as const, icon: <Sparkles className="h-3 w-3" /> };
    case 'complete':
      return { label: 'Completed', variant: 'success' as const, icon: <CheckCircle2 className="h-3 w-3" /> };
    case 'skipped':
      return { label: 'Skipped (Duplicate)', variant: 'secondary' as const, icon: <SkipForward className="h-3 w-3" /> };
    case 'error':
      return { label: 'Failed', variant: 'destructive' as const, icon: <XCircle className="h-3 w-3" /> };
    default:
      return { label: 'Queued in Batch', variant: 'secondary' as const, icon: <Clock className="h-3 w-3" /> };
  }
}

export function LiveUploadMonitor({
  live,
  stats,
  queuePaused,
  maxThreads,
}: {
  live: LiveSnapshot;
  stats: QueueStats | null;
  queuePaused: boolean;
  maxThreads: number;
}) {
  const displayItems = live.batch_items.length > 0 ? live.batch_items : live.active_uploads;
  const totalTracked = stats ? stats.done + stats.pending + stats.failed : 0;
  const overallPct = totalTracked > 0 && stats ? Math.round((stats.done / totalTracked) * 100) : null;

  return (
    <Card>
      <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <CardTitle className="flex items-center gap-2">
            <Activity className="h-4 w-4 text-primary" />
            Live Upload Monitor
            {live.active_count > 0 && (
              <Badge variant="default">
                {live.active_count} / {maxThreads} active
              </Badge>
            )}
            {queuePaused && <Badge variant="warning">Queue Paused</Badge>}
          </CardTitle>
          <CardDescription>
            Real-time progress from the GPMC concurrent batch engine (authoritative completion uses returned media keys).
          </CardDescription>
        </div>
        {live.aggregate_speed_bps != null && (
          <div className="flex items-center gap-1.5 rounded-md border bg-muted/50 px-3 py-1.5 text-xs font-medium">
            <Gauge className="h-3.5 w-3.5 text-primary" />
            {formatSpeed(live.aggregate_speed_bps)}
          </div>
        )}
      </CardHeader>

      <CardContent className="space-y-4">
        {stats && totalTracked > 0 && (
          <div className="rounded-lg border bg-muted/30 p-3">
            <div className="mb-1.5 flex flex-wrap items-center justify-between gap-2 text-xs">
              <span className="font-medium">Overall Queue Progress</span>
              <span className="text-muted-foreground">
                {stats.done.toLocaleString()} completed &middot; {stats.pending.toLocaleString()} pending &middot;{' '}
                {stats.failed.toLocaleString()} failed
                {overallPct != null ? ` (${overallPct}%)` : ''}
              </span>
            </div>
            <Progress value={overallPct} />
          </div>
        )}

        {displayItems.length === 0 ? (
          <div className="rounded-lg border border-dashed py-8 text-center text-sm text-muted-foreground">
            {queuePaused
              ? 'Queue is paused. Resume the queue to process pending uploads.'
              : 'No active uploads right now. Newly detected media will appear here automatically.'}
          </div>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {displayItems.map((item) => {
              const meta = phaseMeta(item.phase);
              return (
                <div
                  key={item.posix_path}
                  className="rounded-lg border bg-background p-3.5 shadow-xs transition-colors"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium" title={item.path}>
                        {item.filename}
                      </p>
                      <p className="truncate text-xs text-muted-foreground">
                        {item.folder_name || 'Monitored folder'}
                        {item.account_name ? ` → ${item.account_name}` : ''}
                      </p>
                    </div>
                    <Badge variant={meta.variant} className="shrink-0">
                      {meta.icon}
                      {meta.label}
                    </Badge>
                  </div>

                  <div className="mt-3">
                    <Progress value={item.percent} />
                    <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
                      <span>
                        {formatBytes(item.bytes_completed)} / {formatBytes(item.bytes_total)}
                        {item.percent != null ? ` (${item.percent.toFixed(0)}%)` : ''}
                      </span>
                      <span className="flex items-center gap-2">
                        {item.speed_bps != null && <span>{formatSpeed(item.speed_bps)}</span>}
                        {item.eta_seconds != null && item.phase === 'uploading' && (
                          <span>ETA {formatDuration(item.eta_seconds)}</span>
                        )}
                      </span>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
