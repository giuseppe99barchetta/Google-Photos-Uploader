import { useEffect, useState } from 'react';
import { Download, ScrollText, Search } from 'lucide-react';
import { api } from '../lib/api';
import { formatTimestamp } from '../lib/utils';
import type { ActivityLogItem } from '../types';
import {
  Badge,
  Button,
  Card,
  CardContent,
  EmptyState,
  Input,
  Select,
  Skeleton,
  useToast,
} from '../components/ui';

export function LogsPage({ refreshSignal }: { refreshSignal: number }) {
  const { toast } = useToast();
  const [items, setItems] = useState<ActivityLogItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState('');
  const [level, setLevel] = useState('');
  const [category, setCategory] = useState('');
  const [offset, setOffset] = useState(0);
  const limit = 60;

  const load = async () => {
    try {
      const res = await api.listLogs({ search, level, category, limit, offset });
      setItems(res.items);
      setTotal(res.total);
    } catch (err) {
      toast({ variant: 'error', title: 'Failed to load activity logs', description: err instanceof Error ? err.message : undefined });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [search, level, category, offset, refreshSignal]);

  const exportUrl = (fmt: 'json' | 'csv') => {
    const qs = new URLSearchParams({ format: fmt });
    if (level) qs.set('level', level);
    if (category) qs.set('category', category);
    return `/api/logs/export?${qs.toString()}`;
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="space-y-4 pt-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-muted-foreground">
              {total.toLocaleString()} audit & activity log entries (all tokens/secrets automatically redacted)
            </p>
            <div className="flex gap-2">
              <a href={exportUrl('csv')} download>
                <Button variant="outline" size="sm">
                  <Download className="h-3.5 w-3.5" />
                  Export CSV
                </Button>
              </a>
              <a href={exportUrl('json')} download>
                <Button variant="outline" size="sm">
                  <Download className="h-3.5 w-3.5" />
                  Export JSON
                </Button>
              </a>
            </div>
          </div>

          <div className="grid gap-2 sm:grid-cols-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                value={search}
                onChange={(e) => {
                  setOffset(0);
                  setSearch(e.target.value);
                }}
                placeholder="Search log messages..."
                className="pl-8"
              />
            </div>

            <Select
              value={level}
              onChange={(e) => {
                setOffset(0);
                setLevel(e.target.value);
              }}
            >
              <option value="">All levels</option>
              <option value="info">Info</option>
              <option value="warning">Warning</option>
              <option value="error">Error</option>
            </Select>

            <Select
              value={category}
              onChange={(e) => {
                setOffset(0);
                setCategory(e.target.value);
              }}
            >
              <option value="">All categories</option>
              <option value="folder">Folder</option>
              <option value="monitor">Monitor</option>
              <option value="upload">Upload</option>
              <option value="retry">Retry</option>
              <option value="auth">Authentication</option>
              <option value="settings">Settings</option>
              <option value="system">System</option>
            </Select>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          {loading ? (
            <div className="space-y-2 p-5">
              {[1, 2, 3, 4].map((i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : items.length === 0 ? (
            <div className="p-6">
              <EmptyState
                icon={<ScrollText className="h-6 w-6" />}
                title="No activity logs yet"
                description="Folder changes, uploads, retries, auth changes, and system events will be logged here."
              />
            </div>
          ) : (
            <div className="divide-y">
              {items.map((entry) => (
                <div key={entry.id} className="flex flex-col justify-between gap-2 px-4 py-3 text-sm sm:flex-row sm:items-center">
                  <div className="flex min-w-0 flex-wrap items-center gap-2">
                    <Badge
                      variant={
                        entry.level === 'error'
                          ? 'destructive'
                          : entry.level === 'warning'
                            ? 'warning'
                            : 'info'
                      }
                    >
                      {entry.level}
                    </Badge>
                    <Badge variant="secondary">{entry.category}</Badge>
                    <span className="break-words">{entry.message}</span>
                  </div>
                  <span className="shrink-0 whitespace-nowrap font-mono text-xs text-muted-foreground">
                    {formatTimestamp(entry.created_at)}
                  </span>
                </div>
              ))}
            </div>
          )}

          {total > limit && (
            <div className="flex items-center justify-between border-t px-4 py-3 text-xs">
              <span className="text-muted-foreground">
                Showing {offset + 1}–{Math.min(offset + limit, total)} of {total}
              </span>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
                  Previous
                </Button>
                <Button variant="outline" size="sm" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>
                  Next
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
