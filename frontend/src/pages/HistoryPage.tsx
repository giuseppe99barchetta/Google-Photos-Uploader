import { useEffect, useState } from 'react';
import { Download, History as HistoryIcon, Search } from 'lucide-react';
import { api } from '../lib/api';
import { formatBytes, formatDuration, formatTimestamp } from '../lib/utils';
import type { GoogleAccount, HistoryItem, MonitoredFolder } from '../types';
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

export function HistoryPage({ refreshSignal }: { refreshSignal: number }) {
  const { toast } = useToast();
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [folders, setFolders] = useState<MonitoredFolder[]>([]);
  const [accounts, setAccounts] = useState<GoogleAccount[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);

  const [search, setSearch] = useState('');
  const [outcome, setOutcome] = useState('');
  const [folderId, setFolderId] = useState('');
  const [accountId, setAccountId] = useState('');
  const [offset, setOffset] = useState(0);
  const limit = 40;

  const load = async () => {
    try {
      const [histRes, folderRes] = await Promise.all([
        api.listHistory({
          search,
          outcome,
          folder_id: folderId ? Number(folderId) : undefined,
          account_id: accountId ? Number(accountId) : undefined,
          limit,
          offset,
        }),
        api.listFolders(),
      ]);
      setItems(histRes.items);
      setTotal(histRes.total);
      setFolders(folderRes.folders);
      setAccounts(folderRes.accounts || []);
    } catch (err) {
      toast({ variant: 'error', title: 'Failed to load history', description: err instanceof Error ? err.message : undefined });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [search, outcome, folderId, accountId, offset, refreshSignal]);

  const exportUrl = (fmt: 'csv' | 'json') => {
    const qs = new URLSearchParams({ format: fmt });
    if (outcome) qs.set('outcome', outcome);
    if (folderId) qs.set('folder_id', folderId);
    if (accountId) qs.set('account_id', accountId);
    return `/api/history/export?${qs.toString()}`;
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardContent className="space-y-4 pt-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-sm text-muted-foreground">
              {total.toLocaleString()} recorded upload events (secrets are never stored or exported)
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

          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
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
              value={outcome}
              onChange={(e) => {
                setOffset(0);
                setOutcome(e.target.value);
              }}
            >
              <option value="">All outcomes</option>
              <option value="uploaded">Uploaded</option>
              <option value="skipped">Skipped (Duplicate)</option>
              <option value="failed">Failed attempt</option>
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
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          {loading ? (
            <div className="space-y-2 p-5">
              {[1, 2, 3, 4].map((i) => (
                <Skeleton key={i} className="h-11 w-full" />
              ))}
            </div>
          ) : items.length === 0 ? (
            <div className="p-6">
              <EmptyState
                icon={<HistoryIcon className="h-6 w-6" />}
                title="No history records"
                description="Completed uploads, remote duplicates, and failed attempts will appear here."
              />
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="border-b bg-muted/40 text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-3 font-medium">File</th>
                    <th className="px-4 py-3 font-medium">Folder</th>
                    <th className="px-4 py-3 font-medium">Outcome</th>
                    <th className="px-4 py-3 font-medium">Size</th>
                    <th className="px-4 py-3 font-medium">Duration</th>
                    <th className="px-4 py-3 font-medium">Result / Error</th>
                    <th className="px-4 py-3 font-medium">Timestamp</th>
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {items.map((item) => (
                    <tr key={item.id} className="hover:bg-muted/30">
                      <td className="max-w-xs px-4 py-3 font-medium">
                        <p className="truncate" title={item.path}>
                          {item.filename}
                        </p>
                      </td>
                      <td className="px-4 py-3 text-xs text-muted-foreground">
                        <div>{item.folder_name || '—'}</div>
                        {item.account_name && (
                          <div className="text-[11px] text-primary">{item.account_name}</div>
                        )}
                      </td>
                      <td className="px-4 py-3">
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
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-xs">{formatBytes(item.file_size)}</td>
                      <td className="whitespace-nowrap px-4 py-3 text-xs">{formatDuration(item.duration)}</td>
                      <td className="max-w-[200px] truncate px-4 py-3 font-mono text-xs text-muted-foreground">
                        {item.media_key || item.error || '—'}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-xs text-muted-foreground">
                        {formatTimestamp(item.created_at)}
                      </td>
                    </tr>
                  ))}
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
