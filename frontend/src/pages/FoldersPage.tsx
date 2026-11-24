import React, { useEffect, useState } from 'react';
import {
  FolderOpen,
  FolderPlus,
  FolderSync,
  Pause,
  Pencil,
  Play,
  RefreshCw,
  Sparkles,
  Trash2,
  UserCheck,
} from 'lucide-react';
import { api } from '../lib/api';
import { formatBytes, formatRelativeTime } from '../lib/utils';
import type { GoogleAccount, MonitoredFolder } from '../types';
import { DirectoryBrowserModal } from '../components/DirectoryBrowserModal';
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  ConfirmDialog,
  EmptyState,
  Input,
  Modal,
  Select,
  Skeleton,
  Switch,
  useToast,
} from '../components/ui';

const ALL_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.heic', '.mp4', '.mov', '.mkv'];
const IMMICH_DIRS_TEXT = 'thumbs, encoded-video, profile, backups, @eaDir';
const IMMICH_PATTERNS_TEXT = '.*, ._*, *.tmp, *.part';

interface FolderFormState {
  name: string;
  path: string;
  accountId: string;
  createSubfolderName: string;
  enabled: boolean;
  recursive: boolean;
  extensions: string[];
  excludeDirsText: string;
  excludePatternsText: string;
  minFileSizeKb: string;
  maxFileSizeMb: string;
}

const DEFAULT_FORM: FolderFormState = {
  name: '',
  path: '',
  accountId: '',
  createSubfolderName: '',
  enabled: true,
  recursive: true,
  extensions: [...ALL_EXTENSIONS],
  excludeDirsText: '',
  excludePatternsText: '',
  minFileSizeKb: '',
  maxFileSizeMb: '',
};

export function FoldersPage({ refreshSignal }: { refreshSignal: number }) {
  const { toast } = useToast();
  const [folders, setFolders] = useState<MonitoredFolder[]>([]);
  const [accounts, setAccounts] = useState<GoogleAccount[]>([]);
  const [approvedRoots, setApprovedRoots] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<number | null>(null);

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<MonitoredFolder | null>(null);
  const [browserOpen, setBrowserOpen] = useState(false);
  const [form, setForm] = useState<FolderFormState>(DEFAULT_FORM);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [removing, setRemoving] = useState<MonitoredFolder | null>(null);

  const load = async () => {
    try {
      const res = await api.listFolders();
      setFolders(res.folders);
      setApprovedRoots(res.approved_roots);
      setAccounts(res.accounts || []);
    } catch (err) {
      toast({
        variant: 'error',
        title: 'Failed to load folders',
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [refreshSignal]);

  const openAdd = () => {
    setEditing(null);
    setForm({
      ...DEFAULT_FORM,
      accountId: accounts[0] ? String(accounts[0].id) : '',
    });
    setFormError(null);
    setModalOpen(true);
  };

  const openEdit = (folder: MonitoredFolder) => {
    setEditing(folder);
    setForm({
      name: folder.name,
      path: folder.path,
      accountId: folder.account_id != null ? String(folder.account_id) : '',
      createSubfolderName: '',
      enabled: folder.enabled,
      recursive: folder.recursive,
      extensions: [...folder.extensions],
      excludeDirsText: folder.exclude_dirs.join(', '),
      excludePatternsText: folder.exclude_patterns.join(', '),
      minFileSizeKb: folder.min_file_size > 1 ? String(Math.round(folder.min_file_size / 1024)) : '',
      maxFileSizeMb:
        folder.max_file_size != null ? String(Math.round(folder.max_file_size / (1024 * 1024))) : '',
    });
    setFormError(null);
    setModalOpen(true);
  };

  const toggleExt = (ext: string) => {
    setForm((prev) => {
      const exists = prev.extensions.includes(ext);
      const next = exists ? prev.extensions.filter((e) => e !== ext) : [...prev.extensions, ext];
      return { ...prev, extensions: next };
    });
  };

  const applyImmichPreset = () => {
    setForm((prev) => ({
      ...prev,
      excludeDirsText: IMMICH_DIRS_TEXT,
      excludePatternsText: IMMICH_PATTERNS_TEXT,
      recursive: true,
    }));
    toast({
      variant: 'info',
      title: 'Applied Immich preset',
      description: 'Excluded thumbs, encoded-video, profile, backups, @eaDir, and hidden/temp files.',
    });
  };

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    setFormError(null);
    if (!form.name.trim()) {
      setFormError('Folder name is required.');
      return;
    }
    if (!editing && !form.path.trim()) {
      setFormError('Select a mounted directory from an approved root.');
      return;
    }
    if (form.extensions.length === 0) {
      setFormError('Select at least one supported media extension.');
      return;
    }

    const exclude_dirs = form.excludeDirsText
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    const exclude_patterns = form.excludePatternsText
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    const min_file_size = form.minFileSizeKb.trim()
      ? Math.max(1, Math.round(Number(form.minFileSizeKb) * 1024))
      : 1;
    const max_file_size = form.maxFileSizeMb.trim()
      ? Math.round(Number(form.maxFileSizeMb) * 1024 * 1024)
      : null;

    if (
      Number.isNaN(min_file_size) ||
      (max_file_size != null && (Number.isNaN(max_file_size) || max_file_size < min_file_size))
    ) {
      setFormError('Check minimum and maximum file size bounds.');
      return;
    }

    const account_id = form.accountId ? Number(form.accountId) : null;

    setSaving(true);
    try {
      if (editing) {
        await api.updateFolder(editing.id, {
          name: form.name.trim(),
          account_id,
          enabled: form.enabled,
          recursive: form.recursive,
          extensions: form.extensions,
          exclude_dirs,
          exclude_patterns,
          min_file_size,
          max_file_size,
        });
        toast({ variant: 'success', title: 'Folder updated', description: form.name.trim() });
      } else {
        await api.addFolder({
          name: form.name.trim(),
          path: form.path.trim(),
          account_id,
          create_subfolder_name: form.createSubfolderName.trim() || null,
          enabled: form.enabled,
          recursive: form.recursive,
          extensions: form.extensions,
          exclude_dirs,
          exclude_patterns,
          min_file_size,
          max_file_size,
        });
        toast({ variant: 'success', title: 'Folder added', description: 'Monitoring and initial scan started.' });
      }
      setModalOpen(false);
      await load();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Unable to save folder');
    } finally {
      setSaving(false);
    }
  };

  const handlePauseResume = async (folder: MonitoredFolder) => {
    setBusyId(folder.id);
    try {
      if (folder.paused) {
        await api.resumeFolder(folder.id);
        toast({ variant: 'success', title: `Resumed ${folder.name}` });
      } else {
        await api.pauseFolder(folder.id);
        toast({
          variant: 'info',
          title: `Paused ${folder.name}`,
          description: 'Active in-flight uploads finish safely.',
        });
      }
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Action failed', description: err instanceof Error ? err.message : undefined });
    } finally {
      setBusyId(null);
    }
  };

  const handleScan = async (folder: MonitoredFolder) => {
    setBusyId(folder.id);
    try {
      const res = await api.scanFolder(folder.id);
      toast({
        variant: 'success',
        title: `Scan complete: ${folder.name}`,
        description: `${res.discovered_count} media files found, ${res.newly_queued} newly queued.`,
      });
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Scan failed', description: err instanceof Error ? err.message : undefined });
    } finally {
      setBusyId(null);
    }
  };

  const handleRemove = async () => {
    if (!removing) return;
    setBusyId(removing.id);
    try {
      await api.removeFolder(removing.id);
      toast({
        variant: 'success',
        title: `Removed ${removing.name} from monitoring`,
        description: 'Local files and queue history were preserved untouched.',
      });
      setRemoving(null);
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Remove failed', description: err instanceof Error ? err.message : undefined });
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-sm text-muted-foreground">
            Approved mounted roots:{' '}
            <span className="font-mono text-foreground">{approvedRoots.join(', ') || '/data'}</span>
          </p>
        </div>
        <Button onClick={openAdd}>
          <FolderPlus className="h-4 w-4" />
          Add Monitored Folder
        </Button>
      </div>

      {loading ? (
        <div className="grid gap-4 md:grid-cols-2">
          <Skeleton className="h-52" />
          <Skeleton className="h-52" />
        </div>
      ) : folders.length === 0 ? (
        <EmptyState
          icon={<FolderSync className="h-6 w-6" />}
          title="No monitored folders"
          description="Select an approved Docker bind-mounted folder (such as an Immich user folder) or create a dedicated subfolder per account."
          action={
            <Button onClick={openAdd}>
              <FolderPlus className="h-4 w-4" />
              Add Folder
            </Button>
          }
        />
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {folders.map((folder) => {
            const busy = busyId === folder.id;
            return (
              <Card key={folder.id}>
                <CardHeader className="flex flex-row items-start justify-between gap-3">
                  <div className="min-w-0">
                    <CardTitle className="flex flex-wrap items-center gap-2">
                      <span className="truncate">{folder.name}</span>
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
                    </CardTitle>
                    <CardDescription className="mt-1 truncate font-mono text-xs" title={folder.path}>
                      {folder.path}
                    </CardDescription>
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      {folder.account_name ? (
                        <Badge
                          variant={folder.account_status === 'valid' ? 'default' : 'destructive'}
                        >
                          <UserCheck className="h-3 w-3" />
                          {folder.account_name}
                          {folder.account_hint ? ` (${folder.account_hint})` : ''}
                        </Badge>
                      ) : (
                        <Badge variant="warning">Unassigned — pick a Google account</Badge>
                      )}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button variant="ghost" size="icon" onClick={() => openEdit(folder)} aria-label="Edit folder">
                      <Pencil className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => setRemoving(folder)}
                      aria-label="Remove folder"
                    >
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </div>
                </CardHeader>

                <CardContent className="space-y-4">
                  <div className="grid grid-cols-3 gap-2 rounded-lg border bg-muted/30 p-3 text-center text-xs">
                    <div>
                      <p className="text-base font-bold">{folder.pending_count}</p>
                      <p className="text-muted-foreground">Pending</p>
                    </div>
                    <div>
                      <p className="text-base font-bold text-emerald-600 dark:text-emerald-400">
                        {folder.uploaded_count}
                      </p>
                      <p className="text-muted-foreground">Uploaded</p>
                    </div>
                    <div>
                      <p className="text-base font-bold text-red-600 dark:text-red-400">
                        {folder.failed_count}
                      </p>
                      <p className="text-muted-foreground">Failed</p>
                    </div>
                  </div>

                  <div className="grid grid-cols-2 gap-y-1.5 text-xs text-muted-foreground">
                    <span>Discovered files: <strong className="text-foreground">{folder.discovered_count}</strong></span>
                    <span>Recursive: <strong className="text-foreground">{folder.recursive ? 'Yes' : 'No'}</strong></span>
                    <span>Last scan: <strong className="text-foreground">{formatRelativeTime(folder.last_scan_at)}</strong></span>
                    <span>Last detected: <strong className="text-foreground">{formatRelativeTime(folder.last_detected_at)}</strong></span>
                    <span>
                      Size filter:{' '}
                      <strong className="text-foreground">
                        {formatBytes(folder.min_file_size)} – {folder.max_file_size ? formatBytes(folder.max_file_size) : 'No max'}
                      </strong>
                    </span>
                    <span className="truncate">
                      Types: <strong className="text-foreground">{folder.extensions.join(' ')}</strong>
                    </span>
                  </div>

                  {folder.last_error && (
                    <p className="rounded-md border border-destructive/30 bg-destructive/10 px-2.5 py-1.5 text-xs text-destructive">
                      {folder.last_error}
                    </p>
                  )}

                  <div className="flex flex-wrap gap-2 border-t pt-3">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy || !folder.enabled}
                      onClick={() => void handlePauseResume(folder)}
                    >
                      {folder.paused ? <Play className="h-3.5 w-3.5" /> : <Pause className="h-3.5 w-3.5" />}
                      {folder.paused ? 'Resume' : 'Pause'}
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={busy}
                      onClick={() => void handleScan(folder)}
                    >
                      <RefreshCw className={`h-3.5 w-3.5 ${busy ? 'animate-spin' : ''}`} />
                      Scan Now
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* Add / Edit Folder Modal */}
      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title={editing ? `Edit Folder: ${editing.name}` : 'Add Monitored Folder'}
        description="Assign this folder to a connected Google Photos account and configure monitoring filters."
        maxWidth="max-w-xl"
      >
        <form onSubmit={handleSave} className="space-y-4">
          {formError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
              {formError}
            </div>
          )}

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Folder Name</label>
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="e.g. Immich User A"
                required
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium">Target Google Account</label>
              <Select
                className="w-full"
                value={form.accountId}
                onChange={(e) => setForm({ ...form, accountId: e.target.value })}
              >
                <option value="">Unassigned (paused)</option>
                {accounts.map((acc) => (
                  <option key={acc.id} value={acc.id}>
                    {acc.name} ({acc.account_hint || acc.status})
                  </option>
                ))}
              </Select>
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium">Mounted Directory</label>
            <div className="flex gap-2">
              <Input
                value={form.path}
                readOnly
                placeholder="Choose from approved mounted roots..."
                className="font-mono text-xs"
              />
              {!editing && (
                <Button variant="outline" onClick={() => setBrowserOpen(true)}>
                  <FolderOpen className="h-4 w-4" />
                  Browse
                </Button>
              )}
            </div>
            {!editing && (
              <div className="pt-1">
                <Input
                  value={form.createSubfolderName}
                  onChange={(e) => setForm({ ...form, createSubfolderName: e.target.value })}
                  placeholder="Optional: auto-create dedicated subfolder inside selected writable root (e.g. account-b)"
                />
              </div>
            )}
          </div>

          <div className="grid grid-cols-2 gap-4 rounded-lg border p-3">
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">Monitoring Enabled</span>
              <Switch checked={form.enabled} onCheckedChange={(v) => setForm({ ...form, enabled: v })} />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm font-medium">Recursive Scan</span>
              <Switch checked={form.recursive} onCheckedChange={(v) => setForm({ ...form, recursive: v })} />
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium">Supported Extensions</label>
            <div className="flex flex-wrap gap-2">
              {ALL_EXTENSIONS.map((ext) => {
                const selected = form.extensions.includes(ext);
                return (
                  <button
                    key={ext}
                    type="button"
                    onClick={() => toggleExt(ext)}
                    className={`rounded-md border px-2.5 py-1 font-mono text-xs font-medium transition-colors ${
                      selected
                        ? 'border-primary bg-primary/15 text-primary'
                        : 'border-input text-muted-foreground hover:bg-accent'
                    }`}
                  >
                    {ext}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <label className="text-sm font-medium">Exclusion Filters</label>
              <Button variant="ghost" size="sm" onClick={applyImmichPreset}>
                <Sparkles className="h-3.5 w-3.5 text-primary" />
                Apply Immich Preset
              </Button>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1">
                <label className="text-xs text-muted-foreground">Excluded Subdirectories</label>
                <Input
                  value={form.excludeDirsText}
                  onChange={(e) => setForm({ ...form, excludeDirsText: e.target.value })}
                  placeholder="thumbs, encoded-video, @eaDir"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs text-muted-foreground">Excluded Filename Patterns</label>
                <Input
                  value={form.excludePatternsText}
                  onChange={(e) => setForm({ ...form, excludePatternsText: e.target.value })}
                  placeholder="._*, *.tmp, *.part"
                />
              </div>
            </div>
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Min File Size (KB, optional)</label>
              <Input
                type="number"
                min="0"
                value={form.minFileSizeKb}
                onChange={(e) => setForm({ ...form, minFileSizeKb: e.target.value })}
                placeholder="Default: 1 byte"
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Max File Size (MB, optional)</label>
              <Input
                type="number"
                min="1"
                value={form.maxFileSizeMb}
                onChange={(e) => setForm({ ...form, maxFileSizeMb: e.target.value })}
                placeholder="No limit"
              />
            </div>
          </div>

          <div className="flex justify-end gap-2 border-t pt-3">
            <Button variant="outline" onClick={() => setModalOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={saving}>
              {saving ? 'Saving...' : editing ? 'Save Changes' : 'Add Folder'}
            </Button>
          </div>
        </form>
      </Modal>

      <DirectoryBrowserModal
        open={browserOpen}
        onClose={() => setBrowserOpen(false)}
        onSelect={(selectedPath, suggestedName) => {
          setForm((prev) => ({
            ...prev,
            path: selectedPath,
            name: prev.name || suggestedName,
          }));
        }}
      />

      <ConfirmDialog
        open={Boolean(removing)}
        onClose={() => setRemoving(null)}
        onConfirm={() => void handleRemove()}
        title="Remove Folder from Monitoring?"
        description={`Stop monitoring "${removing?.name}" (${removing?.path})? Local photos and videos will NOT be deleted, and existing queue/history records will be preserved.`}
        confirmLabel="Remove Monitoring"
        destructive
        busy={removing ? busyId === removing.id : false}
      />
    </div>
  );
}
