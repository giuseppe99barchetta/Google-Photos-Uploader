import { useEffect, useState } from 'react';
import { ArrowUp, Check, Folder, FolderOpen, HardDrive, ShieldCheck } from 'lucide-react';
import { api } from '../lib/api';
import type { BrowseResponse } from '../types';
import { Badge, Button, Modal, Skeleton } from './ui';

export function DirectoryBrowserModal({
  open,
  onClose,
  onSelect,
}: {
  open: boolean;
  onClose: () => void;
  onSelect: (path: string, suggestedName: string) => void;
}) {
  const [data, setData] = useState<BrowseResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async (targetPath?: string) => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.browseFolders(targetPath);
      setData(res);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to browse approved directories');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (open) {
      void load();
    }
  }, [open]);

  const currentName = data?.current_path
    ? data.current_path.split('/').filter(Boolean).pop() || data.current_path
    : '';

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Select Mounted Directory"
      description="Only approved Docker bind-mounted roots and their subdirectories are accessible."
      maxWidth="max-w-xl"
    >
      <div className="space-y-4">
        <div className="flex items-center justify-between gap-2 rounded-lg border bg-muted/40 px-3 py-2 text-xs">
          <div className="flex min-w-0 items-center gap-2">
            <ShieldCheck className="h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
            <span className="truncate font-mono">
              {data?.current_path || 'Approved Mounted Roots'}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {data?.current_path && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => void load(data.parent_path || undefined)}
              >
                <ArrowUp className="h-3.5 w-3.5" />
                Up
              </Button>
            )}
          </div>
        </div>

        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
            {error}
          </div>
        )}

        {loading ? (
          <div className="space-y-2">
            <Skeleton className="h-11 w-full" />
            <Skeleton className="h-11 w-full" />
            <Skeleton className="h-11 w-full" />
          </div>
        ) : (
          <div className="max-h-72 space-y-1.5 overflow-y-auto rounded-lg border p-2">
            {data && data.directories.length === 0 ? (
              <div className="py-8 text-center text-sm text-muted-foreground">
                No subdirectories inside this folder. You can select this directory directly below.
              </div>
            ) : (
              data?.directories.map((dir) => (
                <div
                  key={dir.path}
                  className="flex items-center justify-between gap-2 rounded-md border border-transparent px-3 py-2 hover:border-border hover:bg-accent/60"
                >
                  <button
                    type="button"
                    disabled={!dir.exists || !dir.readable}
                    onClick={() => void load(dir.path)}
                    className="flex min-w-0 flex-1 items-center gap-2.5 text-left disabled:opacity-50"
                  >
                    {dir.is_root ? (
                      <HardDrive className="h-4 w-4 shrink-0 text-primary" />
                    ) : (
                      <Folder className="h-4 w-4 shrink-0 text-amber-500" />
                    )}
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{dir.name}</p>
                      <p className="truncate font-mono text-xs text-muted-foreground">{dir.path}</p>
                    </div>
                  </button>
                  <div className="flex shrink-0 items-center gap-2">
                    {!dir.exists ? (
                      <Badge variant="destructive">Unavailable</Badge>
                    ) : !dir.readable ? (
                      <Badge variant="warning">Unreadable</Badge>
                    ) : (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => {
                          onSelect(dir.path, dir.name);
                          onClose();
                        }}
                      >
                        <Check className="h-3.5 w-3.5" />
                        Select
                      </Button>
                    )}
                  </div>
                </div>
              ))
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2 border-t pt-3">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          {data?.current_path && (
            <Button
              onClick={() => {
                onSelect(data.current_path!, currentName);
                onClose();
              }}
            >
              <FolderOpen className="h-4 w-4" />
              Use Current Folder ({currentName})
            </Button>
          )}
        </div>
      </div>
    </Modal>
  );
}
