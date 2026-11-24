import React, { useEffect, useState } from 'react';
import {
  CheckCircle2,
  Circle,
  ExternalLink,
  FolderSync,
  Image,
  KeyRound,
  Link2,
  RefreshCw,
  Server,
  UserPlus,
} from 'lucide-react';
import { api } from '../lib/api';
import { formatBytes } from '../lib/utils';
import type { ImmichStatus, ImmichUsersResponse, NavPage } from '../types';
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  EmptyState,
  Input,
  Modal,
  Select,
  Skeleton,
  Switch,
  useToast,
} from '../components/ui';

export function ImmichPage({
  refreshSignal,
  onNavigate,
}: {
  refreshSignal: number;
  onNavigate: (page: NavPage) => void;
}) {
  const { toast } = useToast();
  const [status, setStatus] = useState<ImmichStatus | null>(null);
  const [usersData, setUsersData] = useState<ImmichUsersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [savingConfig, setSavingConfig] = useState(false);

  const [url, setUrl] = useState('http://immich-server:2283');
  const [publicUrl, setPublicUrl] = useState('http://localhost:2283');
  const [mediaRoot, setMediaRoot] = useState('');
  const [apiKey, setApiKey] = useState('');

  // Per-row selected target Google account IDs
  const [selectedAccounts, setSelectedAccounts] = useState<Record<string, string>>({});
  const [bindingPath, setBindingPath] = useState<string | null>(null);

  // Create Immich user modal
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [newEmail, setNewEmail] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [newStorageLabel, setNewStorageLabel] = useState('');
  const [newAccountId, setNewAccountId] = useState('');
  const [autoBind, setAutoBind] = useState(true);
  const [creating, setCreating] = useState(false);

  const load = async () => {
    try {
      const st = await api.immichStatus();
      setStatus(st);
      setUrl(st.url || 'http://immich-server:2283');
      setPublicUrl(st.public_url || 'http://localhost:2283');
      setMediaRoot(st.media_root || st.approved_roots.find((r) => r.includes('immich')) || st.approved_roots[0] || '');
      try {
        const u = await api.immichUsers();
        setUsersData(u);
        const defaults: Record<string, string> = {};
        const fallbackAcc = u.accounts[0] ? String(u.accounts[0].id) : '';
        u.users.forEach((usr) => {
          defaults[usr.id] = usr.bound_folder?.account_id
            ? String(usr.bound_folder.account_id)
            : fallbackAcc;
        });
        u.disk_folders.forEach((df) => {
          defaults[df.path] = df.bound_folder?.account_id
            ? String(df.bound_folder.account_id)
            : fallbackAcc;
        });
        setSelectedAccounts((prev) => ({ ...defaults, ...prev }));
        if (!newAccountId && fallbackAcc) setNewAccountId(fallbackAcc);
      } catch (err) {
        setUsersData(null);
        if (st.has_api_key && st.reachable) {
          toast({
            variant: 'warning',
            title: 'Could not list Immich users',
            description: err instanceof Error ? err.message : undefined,
          });
        }
      }
    } catch (err) {
      toast({
        variant: 'error',
        title: 'Failed to load Immich status',
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [refreshSignal]);

  const handleSaveConfig = async (e: React.FormEvent) => {
    e.preventDefault();
    setSavingConfig(true);
    try {
      const updated = await api.updateImmichConfig({
        url: url.trim(),
        public_url: publicUrl.trim(),
        media_root: mediaRoot.trim(),
        api_key: apiKey.trim() || undefined,
      });
      setApiKey('');
      setStatus(updated);
      toast({
        variant: updated.reachable ? 'success' : 'warning',
        title: updated.reachable ? 'Immich connection saved' : 'Settings saved (server not reachable yet)',
        description: updated.error || (updated.version ? `Connected to Immich ${updated.version}` : undefined),
      });
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Failed to save Immich config', description: err instanceof Error ? err.message : undefined });
    } finally {
      setSavingConfig(false);
    }
  };

  const handleClearKey = async () => {
    setSavingConfig(true);
    try {
      await api.updateImmichConfig({ clear_api_key: true });
      toast({ variant: 'info', title: 'Cleared stored Immich API key' });
      await load();
    } finally {
      setSavingConfig(false);
    }
  };

  const handleBind = async (path: string, accountIdStr: string, label: string) => {
    if (!accountIdStr) {
      toast({ variant: 'error', title: 'Select a target Google Photos account first' });
      return;
    }
    setBindingPath(path);
    try {
      const res = await api.bindImmichFolder({
        path,
        account_id: Number(accountIdStr),
        folder_name: `Immich - ${label}`,
      });
      toast({
        variant: 'success',
        title: res.updated_existing ? 'Updated Immich sync link' : 'Immich folder auto-sync enabled',
        description: `${res.folder.path} → ${res.folder.account_name || 'Google Account'}`,
      });
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Failed to bind Immich folder', description: err instanceof Error ? err.message : undefined });
    } finally {
      setBindingPath(null);
    }
  };

  const handleCreateUser = async (e: React.FormEvent) => {
    e.preventDefault();
    setCreating(true);
    try {
      const res = await api.createImmichUser({
        name: newName.trim(),
        email: newEmail.trim(),
        password: newPassword,
        storage_label: newStorageLabel.trim() || null,
        account_id: autoBind && newAccountId ? Number(newAccountId) : null,
        auto_bind: autoBind,
      });
      setCreateOpen(false);
      setNewName('');
      setNewEmail('');
      setNewPassword('');
      setNewStorageLabel('');
      toast({
        variant: res.bind_warning ? 'warning' : 'success',
        title: `Created Immich user "${res.user.name}"`,
        description: res.folder
          ? `Auto-linked ${res.folder.path} to ${res.folder.account_name || 'Google Account'}.`
          : res.bind_warning || 'User created in Immich.',
      });
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Failed to create Immich user', description: err instanceof Error ? err.message : undefined });
    } finally {
      setCreating(false);
    }
  };

  if (loading && !status) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-36 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  const accounts = usersData?.accounts || [];
  const step1Done = Boolean(status?.reachable && status?.media_root_exists);
  const step2Done = Boolean(status?.api_key_valid);
  const step3Done = accounts.length > 0;
  const step4Done = Boolean(
    usersData?.users.some((u) => u.bound_folder) || usersData?.disk_folders.some((d) => d.bound_folder)
  );

  return (
    <div className="space-y-6">
      {/* Status Banner */}
      <Card>
        <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <CardTitle className="flex flex-wrap items-center gap-2">
              <Image className="h-4 w-4 text-primary" />
              Immich Server & Multi-User Auto-Sync
              {status?.reachable ? (
                <Badge variant="success">Online {status.version || ''}</Badge>
              ) : (
                <Badge variant="warning">Not Connected</Badge>
              )}
              {status?.media_root_exists ? (
                <Badge variant="info">Media Mounted ({status.media_root})</Badge>
              ) : (
                <Badge variant="secondary">Media root not mounted yet</Badge>
              )}
            </CardTitle>
            <CardDescription>
              Connect your Immich users to separate Google Photos accounts. Original photos stay untouched in Immich (read-only mount).
            </CardDescription>
          </div>
          <div className="flex flex-wrap gap-2">
            {status?.public_url && (
              <a href={status.public_url} target="_blank" rel="noreferrer">
                <Button variant="outline" size="sm">
                  <ExternalLink className="h-3.5 w-3.5" />
                  Open Immich
                </Button>
              </a>
            )}
            <Button variant="secondary" size="sm" onClick={() => void load()}>
              <RefreshCw className="h-3.5 w-3.5" />
              Refresh
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {/* Guided 4-Step Checklist */}
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {[
              {
                done: step1Done,
                title: '1. Start Immich Stack',
                desc: 'Run compose with docker-compose.immich.yml',
              },
              {
                done: step2Done,
                title: '2. Paste Immich API Key',
                desc: 'Immich → Account Settings → API Keys',
              },
              {
                done: step3Done,
                title: '3. Connect Google Accounts',
                desc: `${accounts.length} account(s) ready`,
                action: () => onNavigate('auth'),
              },
              {
                done: step4Done,
                title: '4. Link Users → Accounts',
                desc: '1-click binding below',
              },
            ].map((step) => (
              <div
                key={step.title}
                onClick={step.action}
                className={`flex items-start gap-2.5 rounded-lg border p-3 text-xs ${
                  step.action ? 'cursor-pointer hover:bg-accent/50' : ''
                }`}
              >
                {step.done ? (
                  <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
                ) : (
                  <Circle className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                )}
                <div>
                  <p className="font-semibold text-foreground">{step.title}</p>
                  <p className="mt-0.5 text-muted-foreground">{step.desc}</p>
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Immich Connection Form */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Server className="h-4 w-4 text-primary" />
            Immich Connection Settings
          </CardTitle>
          <CardDescription>
            When using <code className="font-mono">docker-compose.immich.yml</code>, URL is{' '}
            <code className="font-mono">http://immich-server:2283</code> and Media Root is{' '}
            <code className="font-mono">/media/immich</code>. Your API key is stored with 0600 permissions outside SQLite.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSaveConfig} className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="space-y-1">
                <label className="text-xs font-medium">Internal Immich API URL</label>
                <Input
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                  placeholder="http://immich-server:2283"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs font-medium">Browser Link URL</label>
                <Input
                  value={publicUrl}
                  onChange={(e) => setPublicUrl(e.target.value)}
                  placeholder="http://localhost:2283"
                />
              </div>
              <div className="space-y-1">
                <label className="text-xs font-medium">Mounted Immich Media Root</label>
                <Select
                  className="w-full"
                  value={mediaRoot}
                  onChange={(e) => setMediaRoot(e.target.value)}
                >
                  <option value="">Select mounted root...</option>
                  {(status?.approved_roots || []).map((root) => (
                    <option key={root} value={root}>
                      {root}
                    </option>
                  ))}
                </Select>
              </div>
            </div>

            <div className="space-y-1">
              <label className="text-xs font-medium">
                Immich Admin API Key {status?.has_api_key ? '(saved — paste new key to replace)' : ''}
              </label>
              <div className="flex flex-wrap gap-2">
                <Input
                  type="password"
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  placeholder={status?.has_api_key ? '•••••••••••••••• (stored securely)' : 'Paste Immich Admin API Key'}
                  className="max-w-lg font-mono text-xs"
                />
                <Button type="submit" disabled={savingConfig}>
                  <KeyRound className="h-4 w-4" />
                  {savingConfig ? 'Saving...' : 'Save & Test Connection'}
                </Button>
                {status?.has_api_key && (
                  <Button variant="outline" onClick={() => void handleClearKey()} disabled={savingConfig}>
                    Clear Key
                  </Button>
                )}
              </div>
            </div>

            {status?.error && (
              <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-xs text-amber-700 dark:text-amber-400">
                {status.error}
              </div>
            )}
          </form>
        </CardContent>
      </Card>

      {/* Immich Users -> Google Accounts Mapping */}
      <Card>
        <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <CardTitle className="flex items-center gap-2">
              <Link2 className="h-4 w-4 text-primary" />
              Immich Users → Google Photos Accounts
            </CardTitle>
            <CardDescription>
              Pick which Google Photos account receives each Immich user&apos;s automatic mobile backups.
            </CardDescription>
          </div>
          <div className="flex gap-2">
            {accounts.length === 0 && (
              <Button variant="outline" size="sm" onClick={() => onNavigate('auth')}>
                Connect Google Account
              </Button>
            )}
            {status?.admin_access && (
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                <UserPlus className="h-4 w-4" />
                Create Immich User
              </Button>
            )}
          </div>
        </CardHeader>

        <CardContent className="space-y-4">
          {!usersData?.api_connected ? (
            <EmptyState
              icon={<Image className="h-6 w-6" />}
              title={
                status?.has_api_key
                  ? 'Immich API key needs attention'
                  : 'Connect your Immich Admin API key to discover users'
              }
              description={
                usersData?.message ||
                'Open Immich (http://localhost:2283), finish creating your first Admin user, then go to Account Settings → API Keys → New API Key and paste it above.'
              }
            />
          ) : usersData.users.length === 0 ? (
            <EmptyState
              icon={<UserPlus className="h-6 w-6" />}
              title="No Immich users returned"
              description="Create your first Immich users in Immich or click Create Immich User above."
            />
          ) : (
            <div className="divide-y rounded-lg border">
              {usersData.users.map((user) => {
                const chosenAcc = selectedAccounts[user.id] || '';
                const busy = bindingPath === user.recommended_path;
                return (
                  <div
                    key={user.id}
                    className="flex flex-col justify-between gap-3 p-4 lg:flex-row lg:items-center"
                  >
                    <div className="min-w-0 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <p className="font-semibold">{user.name}</p>
                        {user.is_admin && <Badge variant="secondary">Admin</Badge>}
                        {user.storage_label && (
                          <Badge variant="info">Label: {user.storage_label}</Badge>
                        )}
                        {user.bound_folder ? (
                          <Badge variant="success">
                            Linked → {user.bound_folder.account_name || 'Google Account'}
                          </Badge>
                        ) : (
                          <Badge variant="warning">Not linked yet</Badge>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">
                        {user.email}
                        {user.quota_usage_bytes != null
                          ? ` · Used ${formatBytes(user.quota_usage_bytes)}`
                          : ''}
                      </p>
                      {user.recommended_path ? (
                        <p className="truncate font-mono text-xs text-muted-foreground">
                          Folder: {user.recommended_path}{' '}
                          {user.folder_exists
                            ? '(ready on disk)'
                            : '(will auto-activate on first photo upload)'}
                        </p>
                      ) : (
                        <p className="text-xs text-amber-600 dark:text-amber-400">
                          Mount Immich storage at /media/immich to enable automatic folder syncing.
                        </p>
                      )}
                    </div>

                    {user.recommended_path && (
                      <div className="flex flex-wrap items-center gap-2">
                        <Select
                          value={chosenAcc}
                          onChange={(e) =>
                            setSelectedAccounts((prev) => ({ ...prev, [user.id]: e.target.value }))
                          }
                        >
                          <option value="">Choose Google Account...</option>
                          {accounts.map((acc) => (
                            <option key={acc.id} value={acc.id}>
                              {acc.name} ({acc.account_hint})
                            </option>
                          ))}
                        </Select>
                        <Button
                          size="sm"
                          disabled={busy || !chosenAcc}
                          onClick={() =>
                            void handleBind(user.recommended_path!, chosenAcc, user.name)
                          }
                        >
                          <FolderSync className="h-3.5 w-3.5" />
                          {busy
                            ? 'Linking...'
                            : user.bound_folder
                              ? 'Update Link'
                              : 'Link & Auto-Sync'}
                        </Button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {/* Disk-discovered folders fallback */}
          {usersData && usersData.disk_folders.length > 0 && !usersData.api_connected && (
            <div className="space-y-2 pt-2">
              <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                Detected Immich Folders on Disk ({status?.media_root})
              </p>
              <div className="divide-y rounded-lg border">
                {usersData.disk_folders.map((df) => {
                  const chosenAcc = selectedAccounts[df.path] || '';
                  return (
                    <div key={df.path} className="flex flex-wrap items-center justify-between gap-3 p-3.5 text-sm">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span className="font-medium">{df.label}</span>
                          <Badge variant="secondary">{df.storage_mode}</Badge>
                          {df.bound_folder && (
                            <Badge variant="success">
                              Linked → {df.bound_folder.account_name}
                            </Badge>
                          )}
                        </div>
                        <p className="truncate font-mono text-xs text-muted-foreground">{df.path}</p>
                      </div>
                      <div className="flex items-center gap-2">
                        <Select
                          value={chosenAcc}
                          onChange={(e) =>
                            setSelectedAccounts((prev) => ({ ...prev, [df.path]: e.target.value }))
                          }
                        >
                          <option value="">Choose Google Account...</option>
                          {accounts.map((acc) => (
                            <option key={acc.id} value={acc.id}>
                              {acc.name}
                            </option>
                          ))}
                        </Select>
                        <Button
                          size="sm"
                          disabled={!chosenAcc || bindingPath === df.path}
                          onClick={() => void handleBind(df.path, chosenAcc, df.label)}
                        >
                          Link & Sync
                        </Button>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Create Immich User Modal */}
      <Modal
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        title="Create Immich User"
        description="Creates a new Immich user account and optionally binds their photo folder directly to a Google Photos account."
      >
        <form onSubmit={handleCreateUser} className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="text-xs font-medium">Full Name</label>
              <Input value={newName} onChange={(e) => setNewName(e.target.value)} placeholder="User B" required />
            </div>
            <div className="space-y-1">
              <label className="text-xs font-medium">Storage Label (optional)</label>
              <Input
                value={newStorageLabel}
                onChange={(e) => setNewStorageLabel(e.target.value)}
                placeholder="e.g. userB"
              />
            </div>
          </div>
          <div className="space-y-1">
            <label className="text-xs font-medium">Immich Login Email</label>
            <Input
              type="email"
              value={newEmail}
              onChange={(e) => setNewEmail(e.target.value)}
              placeholder="userb@home.local"
              required
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs font-medium">Immich Password (min 6 chars)</label>
            <Input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              minLength={6}
              required
            />
          </div>

          <div className="space-y-3 rounded-lg border bg-muted/20 p-3">
            <div className="flex items-center justify-between text-sm">
              <span className="font-medium">Auto-link to Google Photos Account</span>
              <Switch checked={autoBind} onCheckedChange={setAutoBind} />
            </div>
            {autoBind && (
              <Select
                className="w-full"
                value={newAccountId}
                onChange={(e) => setNewAccountId(e.target.value)}
              >
                <option value="">Select Google Account...</option>
                {accounts.map((acc) => (
                  <option key={acc.id} value={acc.id}>
                    {acc.name} ({acc.account_hint})
                  </option>
                ))}
              </Select>
            )}
          </div>

          <div className="flex justify-end gap-2 border-t pt-3">
            <Button variant="outline" onClick={() => setCreateOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={creating}>
              {creating ? 'Creating...' : 'Create User'}
            </Button>
          </div>
        </form>
      </Modal>
    </div>
  );
}
