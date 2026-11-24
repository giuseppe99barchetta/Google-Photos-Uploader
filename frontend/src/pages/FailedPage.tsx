import { useEffect, useState } from 'react';
import { AlertOctagon, CheckCircle2, RotateCcw, Search } from 'lucide-react';
import { api } from '../lib/api';
import { formatBytes, formatRelativeTime, formatTimestamp } from '../lib/utils';
import type { QueueJob } from '../types';
import {
  Badge,
  Button,
  Card,
  CardContent,
  EmptyState,
  Input,
  Skeleton,
  useToast,
} from '../components/ui';

export function FailedPage({ refreshSignal }: { refreshSignal: number }) {
  const { toast } = useToast();
  const [jobs, setJobs] = useState<QueueJob[]>([]);
  const [total, setTotal] = useState(0);
  const [search, setSearch] = useState('');
  const [loading, setLoading] = useState(true);
  const [retrying, setRetrying] = useState(false);

  const load = async () => {
    try {
      const res = await api.listQueue({
        search,
        status: 'failed_or_retrying',
        sort_by: 'updated_at',
        sort_dir: 'desc',
        limit: 100,
      });
      setJobs(res.items);
      setTotal(res.total);
    } catch (err) {
      toast({ variant: 'error', title: 'Failed to load errors', description: err instanceof Error ? err.message : undefined });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [search, refreshSignal]);

  const handleRetryAll = async () => {
    setRetrying(true);
    try {
      const res = await api.retryAll();
      toast({ variant: 'success', title: `Requeued ${res.retried_count} failed job(s)` });
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Retry failed', description: err instanceof Error ? err.message : undefined });
    } finally {
      setRetrying(false);
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

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="relative w-full max-w-sm">
          <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search failed files..."
            className="pl-8"
          />
        </div>
        <Button onClick={() => void handleRetryAll()} disabled={retrying || total === 0}>
          <RotateCcw className="h-4 w-4" />
          Retry All Eligible ({total})
        </Button>
      </div>

      <Card>
        <CardContent className="p-0">
          {loading ? (
            <div className="space-y-2 p-5">
              {[1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-14 w-full" />
              ))}
            </div>
          ) : jobs.length === 0 ? (
            <div className="p-6">
              <EmptyState
                icon={<CheckCircle2 className="h-6 w-6 text-emerald-500" />}
                title="No failed uploads"
                description="All queued media items are uploading cleanly without errors."
              />
            </div>
          ) : (
            <div className="divide-y">
              {jobs.map((job) => (
                <div
                  key={job.path}
                  className="flex flex-col justify-between gap-3 p-4 sm:flex-row sm:items-center"
                >
                  <div className="min-w-0 space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <AlertOctagon className="h-4 w-4 shrink-0 text-destructive" />
                      <p className="truncate text-sm font-semibold">{job.filename}</p>
                      <Badge variant={job.status === 'failed' ? 'destructive' : 'warning'}>
                        {job.status === 'failed' ? 'Exhausted / Cooldown' : 'Retrying'}
                      </Badge>
                      <span className="text-xs text-muted-foreground">{formatBytes(job.file_size)}</span>
                    </div>
                    <p className="truncate font-mono text-xs text-muted-foreground">{job.path}</p>
                    <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
                      <span>Error: <strong className="text-destructive">{job.last_error || 'Unknown'}</strong></span>
                      <span>Attempts: <strong className="text-foreground">{job.attempts}</strong></span>
                      <span>Next retry: <strong className="text-foreground">{formatRelativeTime(job.next_attempt)}</strong></span>
                      <span>Last attempt: {formatTimestamp(job.last_attempt_at)}</span>
                    </div>
                  </div>
                  <Button variant="outline" size="sm" onClick={() => void handleRetryOne(job)}>
                    <RotateCcw className="h-3.5 w-3.5" />
                    Retry Now
                  </Button>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
