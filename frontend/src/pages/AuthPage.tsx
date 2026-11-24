import React, { useEffect, useState } from 'react';
import {
  CheckCircle2,
  FolderOpen,
  KeyRound,
  Lock,
  Pencil,
  Plus,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  Users,
} from 'lucide-react';
import { api } from '../lib/api';
import { formatTimestamp } from '../lib/utils';
import type { DirectoryEntry, GoogleAccount } from '../types';
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

type FolderSetupMode = 'dedicated' | 'existing' | 'later';

export function AuthPage({ refreshSignal }: { refreshSignal: number }) {
  const { toast } = useToast();
  const [accounts, setAccounts] = useState<GoogleAccount[]>([]);
  const [roots, setRoots] = useState<DirectoryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyAccountId, setBusyAccountId] = useState<number | null>(null);

  // Add account modal state
  const [addOpen, setAddOpen] = useState(false);
  const [addName, setAddName] = useState('');
  const [addAuth, setAddAuth] = useState('');
  const [folderMode, setFolderMode] = useState<FolderSetupMode>('dedicated');
  const [parentRoot, setParentRoot] = useState('');
  const [bindPath, setBindPath] = useState('');
  const [folderName, setFolderName] = useState('');
  const [immichPreset, setImmichPreset] = useState(true);
  const [browserOpen, setBrowserOpen] = useState(false);
  const [validating, setValidating] = useState(false);
  const [saving, setSaving] = useState(false);
  const [validationResult, setValidationResult] = useState<{
    valid: boolean;
    status: string;
    account_hint: string;
    message: string;
  } | null>(null);

  // Rotate credentials modal state
  const [rotatingAccount, setRotatingAccount] = useState<GoogleAccount | null>(null);
  const [rotateAuth, setRotateAuth] = useState('');
  const [rotateValidation, setRotateValidation] = useState<{
    valid: boolean;
    status: string;
    account_hint: string;
    message: string;
  } | null>(null);
  const [rotating, setRotating] = useState(false);

  // Rename & remove states
  const [renamingAccount, setRenamingAccount] = useState<GoogleAccount | null>(null);
  const [renameText, setRenameText] = useState('');
  const [removingAccount, setRemovingAccount] = useState<GoogleAccount | null>(null);

  // Dashboard password change
  const [curPass, setCurPass] = useState('');
  const [newPass, setNewPass] = useState('');
  const [changingPass, setChangingPass] = useState(false);

  const load = async () => {
    try {
      const res = await api.listAccounts();
      setAccounts(res.accounts);
      setRoots(res.approved_roots);
      const writableRoot = res.approved_roots.find((r) => r.exists && r.writable);
      if (writableRoot && !parentRoot) {
        setParentRoot(writableRoot.path);
      } else if (res.approved_roots[0] && !parentRoot) {
        setParentRoot(res.approved_roots[0].path);
      }
    } catch (err) {
      toast({
        variant: 'error',
        title: 'Failed to load connected accounts',
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, [refreshSignal]);

  const statusBadge = (s: GoogleAccount['status']) => {
    switch (s) {
      case 'valid':
        return <Badge variant="success">Valid</Badge>;
      case 'validating':
        return <Badge variant="info">Validating</Badge>;
      case 'not_configured':
        return <Badge variant="warning">Not Configured</Badge>;
      case 'expired':
        return <Badge variant="destructive">Expired</Badge>;
      case 'invalid':
        return <Badge variant="destructive">Invalid</Badge>;
      default:
        return <Badge variant="warning">Error</Badge>;
    }
  };

  const openAddModal = () => {
    setAddName('');
    setAddAuth('');
    setBindPath('');
    setFolderName('');
    setValidationResult(null);
    const hasWritable = roots.some((r) => r.exists && r.writable);
    setFolderMode(hasWritable ? 'dedicated' : 'existing');
    setImmichPreset(!hasWritable);
    setAddOpen(true);
  };

  const handleValidateNew = async () => {
    if (!addAuth.trim()) return;
    setValidating(true);
    setValidationResult(null);
    try {
      const res = await api.validateAuth(addAuth);
      setValidationResult(res);
      toast({
        variant: res.valid ? 'success' : 'error',
        title: res.valid ? 'Credentials valid' : `Validation failed (${res.status})`,
        description: res.message,
      });
    } catch (err) {
      toast({ variant: 'error', title: 'Validation failed', description: err instanceof Error ? err.message : undefined });
    } finally {
      setValidating(false);
    }
  };

  const handleCreateAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!addName.trim() || !addAuth.trim()) return;
    if (folderMode === 'existing' && !bindPath.trim()) {
      toast({ variant: 'error', title: 'Select an existing mounted folder or choose another folder mode' });
      return;
    }
    setSaving(true);
    try {
      const res = await api.addAccount({
        name: addName.trim(),
        auth_data: addAuth,
        create_dedicated_folder: folderMode === 'dedicated',
        parent_root: folderMode === 'dedicated' ? parentRoot : null,
        bind_folder_path: folderMode === 'existing' ? bindPath : null,
        folder_name: folderName.trim() || null,
        immich_preset: immichPreset,
      });
      setAddOpen(false);
      setAddAuth('');
      toast({
        variant: res.folder_warning ? 'warning' : 'success',
        title: `Connected ${res.account.name}`,
        description: res.folder_warning
          ? `Account connected, but folder setup noted: ${res.folder_warning}`
          : res.folder
            ? `Bound dedicated folder: ${res.folder.path}`
            : 'Account connected safely with isolated 0600 credentials.',
      });
      await load();
    } catch (err) {
      toast({
        variant: 'error',
        title: 'Could not connect Google account',
        description: err instanceof Error ? err.message : undefined,
      });
    } finally {
      setSaving(false);
    }
  };

  const handleRevalidate = async (account: GoogleAccount) => {
    setBusyAccountId(account.id);
    try {
      const res = await api.revalidateAccount(account.id);
      toast({
        variant: res.valid ? 'success' : 'warning',
        title: `${account.name}: ${res.status}`,
        description: res.message,
      });
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Revalidation failed', description: err instanceof Error ? err.message : undefined });
    } finally {
      setBusyAccountId(null);
    }
  };

  const handleValidateRotate = async () => {
    if (!rotateAuth.trim()) return;
    setValidating(true);
    setRotateValidation(null);
    try {
      const res = await api.validateAuth(rotateAuth);
      setRotateValidation(res);
    } catch (err) {
      toast({ variant: 'error', title: 'Validation failed', description: err instanceof Error ? err.message : undefined });
    } finally {
      setValidating(false);
    }
  };

  const handleSaveRotate = async () => {
    if (!rotatingAccount || !rotateAuth.trim()) return;
    setRotating(true);
    try {
      await api.rotateAccountCredentials(rotatingAccount.id, rotateAuth);
      toast({
        variant: 'success',
        title: `Rotated credentials for ${rotatingAccount.name}`,
        description: 'New credentials validated, saved atomically (0600), and reloaded.',
      });
      setRotatingAccount(null);
      setRotateAuth('');
      setRotateValidation(null);
      await load();
    } catch (err) {
      toast({
        variant: 'error',
        title: 'Credential update rejected',
        description: err instanceof Error ? err.message : 'Existing working credentials preserved.',
      });
    } finally {
      setRotating(false);
    }
  };

  const handleRename = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!renamingAccount || !renameText.trim()) return;
    setBusyAccountId(renamingAccount.id);
    try {
      await api.renameAccount(renamingAccount.id, renameText.trim());
      toast({ variant: 'success', title: 'Account renamed' });
      setRenamingAccount(null);
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Rename failed', description: err instanceof Error ? err.message : undefined });
    } finally {
      setBusyAccountId(null);
    }
  };

  const handleRemove = async () => {
    if (!removingAccount) return;
    setBusyAccountId(removingAccount.id);
    try {
      const res = await api.removeAccount(removingAccount.id, true);
      toast({
        variant: 'success',
        title: `Disconnected ${removingAccount.name}`,
        description:
          res.unbound_folders > 0
            ? `${res.unbound_folders} bound folder(s) were paused safely. Local media files were untouched.`
            : 'Credentials removed. Local media files and history were preserved.',
      });
      setRemovingAccount(null);
      await load();
    } catch (err) {
      toast({ variant: 'error', title: 'Disconnect failed', description: err instanceof Error ? err.message : undefined });
    } finally {
      setBusyAccountId(null);
    }
  };

  const handlePasswordChange = async (e: React.FormEvent) => {
    e.preventDefault();
    if (newPass.length < 8) {
      toast({ variant: 'error', title: 'Password must be at least 8 characters' });
      return;
    }
    setChangingPass(true);
    try {
      await api.changePassword(curPass, newPass);
      setCurPass('');
      setNewPass('');
      toast({ variant: 'success', title: 'Dashboard password updated' });
    } catch (err) {
      toast({ variant: 'error', title: 'Password update failed', description: err instanceof Error ? err.message : undefined });
    } finally {
      setChangingPass(false);
    }
  };

  if (loading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-44 w-full" />
        <Skeleton className="h-44 w-full" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header + Connect Button */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold">Connected Google Photos Accounts</h2>
          <p className="text-sm text-muted-foreground">
            Connect multiple Google accounts and bind each Immich user folder or dedicated drop folder to its own account.
          </p>
        </div>
        <Button onClick={openAddModal}>
          <Plus className="h-4 w-4" />
          Connect Google Account
        </Button>
      </div>

      {/* Connected Accounts Grid */}
      {accounts.length === 0 ? (
        <EmptyState
          icon={<Users className="h-6 w-6" />}
          title="No Google accounts connected"
          description="Connect a Google account using Android AUTH_DATA and assign a dedicated folder or Immich user folder."
          action={
            <Button onClick={openAddModal}>
              <Plus className="h-4 w-4" />
              Connect Google Account
            </Button>
          }
        />
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {accounts.map((acc) => {
            const busy = busyAccountId === acc.id;
            return (
              <Card key={acc.id}>
                <CardHeader className="flex flex-row items-start justify-between gap-3">
                  <div className="min-w-0">
                    <CardTitle className="flex flex-wrap items-center gap-2">
                      <span className="truncate">{acc.name}</span>
                      {acc.is_primary && <Badge variant="secondary">Primary</Badge>}
                      {statusBadge(acc.status)}
                    </CardTitle>
                    <CardDescription className="mt-1 font-mono text-xs">
                      {acc.account_hint || 'Not configured'} &middot; slug: {acc.slug}
                    </CardDescription>
                  </div>
                  <div className="flex shrink-0 items-center gap-1">
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => {
                        setRenamingAccount(acc);
                        setRenameText(acc.name);
                      }}
                      aria-label="Rename account"
                    >
                      <Pencil className="h-4 w-4" />
                    </Button>
                    <Button
                      variant="ghost"
                      size="icon"
                      onClick={() => setRemovingAccount(acc)}
                      aria-label="Disconnect account"
                    >
                      <Trash2 className="h-4 w-4 text-destructive" />
                    </Button>
                  </div>
                </CardHeader>

                <CardContent className="space-y-4">
                  <div className="grid grid-cols-4 gap-2 rounded-lg border bg-muted/30 p-3 text-center text-xs">
                    <div>
                      <p className="text-base font-bold">{acc.folder_count}</p>
                      <p className="text-muted-foreground">Folders</p>
                    </div>
                    <div>
                      <p className="text-base font-bold">{acc.pending_count}</p>
                      <p className="text-muted-foreground">Pending</p>
                    </div>
                    <div>
                      <p className="text-base font-bold text-emerald-600 dark:text-emerald-400">
                        {acc.uploaded_count}
                      </p>
                      <p className="text-muted-foreground">Uploaded</p>
                    </div>
                    <div>
                      <p className="text-base font-bold text-red-600 dark:text-red-400">
                        {acc.failed_count}
                      </p>
                      <p className="text-muted-foreground">Failed</p>
                    </div>
                  </div>

                  <div className="space-y-1 text-xs text-muted-foreground">
                    <p>
                      Source:{' '}
                      <strong className="text-foreground">
                        {acc.source === 'secret_file'
                          ? 'Mounted Secret (/run/secrets/gphotos_auth)'
                          : acc.source === 'managed_override'
                            ? 'Managed 0600 Override (/state/auth_override.secret)'
                            : acc.source === 'account_secret'
                              ? `Isolated 0600 Secret (/state/accounts/account_${acc.id}.secret)`
                              : acc.source}
                      </strong>
                    </p>
                    <p>
                      Last validated:{' '}
                      <strong className="text-foreground">{formatTimestamp(acc.last_validated_at)}</strong>
                    </p>
                  </div>

                  {acc.last_error && (
                    <div className="rounded-md border border-destructive/30 bg-destructive/10 p-2.5 text-xs text-destructive">
                      {acc.last_error}
                    </div>
                  )}

                  <div className="flex flex-wrap gap-2 border-t pt-3">
                    <Button
                      variant="outline"
                      size="sm"
                      disabled={busy}
                      onClick={() => void handleRevalidate(acc)}
                    >
                      <RefreshCw className={`h-3.5 w-3.5 ${busy ? 'animate-spin' : ''}`} />
                      Revalidate
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={busy}
                      onClick={() => {
                        setRotatingAccount(acc);
                        setRotateAuth('');
                        setRotateValidation(null);
                      }}
                    >
                      <KeyRound className="h-3.5 w-3.5" />
                      Rotate Credentials
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {/* Security note */}
      <div className="flex items-start gap-2.5 rounded-lg border bg-muted/20 p-3.5 text-xs text-muted-foreground">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-400" />
        <div>
          <strong className="text-foreground">Credential isolation & safety:</strong> Every account&apos;s
          Android <code className="font-mono">AUTH_DATA</code> is validated against Google before saving,
          stored atomically with <code className="font-mono">0600</code> permissions outside SQLite (never
          modifying read-only <code className="font-mono">/run/secrets/gphotos_auth</code>), automatically
          redacted from all logs, and runs with its own isolated GPMC client and hash cache. Multiple accounts
          can share the same <code className="font-mono">androidId</code> if captured from the same device,
          as long as each account has its own full captured <code className="font-mono">AUTH_DATA</code> string.
        </div>
      </div>

      {/* Dashboard Admin Password */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Lock className="h-4 w-4 text-primary" />
            Dashboard Access Password
          </CardTitle>
          <CardDescription>Update the local password used to sign into this web dashboard.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handlePasswordChange} className="grid max-w-xl gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <label className="text-xs font-medium">Current Password</label>
              <Input type="password" value={curPass} onChange={(e) => setCurPass(e.target.value)} required />
            </div>
            <div className="space-y-1">
              <label className="text-xs font-medium">New Password (min 8 chars)</label>
              <Input
                type="password"
                value={newPass}
                onChange={(e) => setNewPass(e.target.value)}
                minLength={8}
                required
              />
            </div>
            <div className="sm:col-span-2">
              <Button type="submit" variant="secondary" size="sm" disabled={changingPass}>
                {changingPass ? 'Updating...' : 'Update Dashboard Password'}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>

      {/* Connect New Google Account Modal */}
      <Modal
        open={addOpen}
        onClose={() => setAddOpen(false)}
        title="Connect Google Photos Account"
        description="Validate Android AUTH_DATA and optionally bind a dedicated folder or Immich user folder."
        maxWidth="max-w-xl"
      >
        <form onSubmit={handleCreateAccount} className="space-y-4">
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Account Label</label>
            <Input
              value={addName}
              onChange={(e) => setAddName(e.target.value)}
              placeholder="e.g. Account A (Immich User A)"
              required
            />
          </div>

          <div className="space-y-1.5">
            <label className="text-sm font-medium">Android AUTH_DATA</label>
            <textarea
              value={addAuth}
              onChange={(e) => {
                setAddAuth(e.target.value);
                setValidationResult(null);
              }}
              rows={4}
              spellCheck={false}
              autoComplete="off"
              placeholder="androidId=...&client_sig=...&callerSig=...&Email=...&Token=..."
              className="w-full rounded-md border border-input bg-background p-3 font-mono text-xs shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              required
            />
            <div className="flex items-center justify-between gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={!addAuth.trim() || validating || saving}
                onClick={() => void handleValidateNew()}
              >
                <ShieldCheck className="h-3.5 w-3.5" />
                {validating ? 'Validating...' : 'Validate First'}
              </Button>
              {validationResult && (
                <span
                  className={`flex items-center gap-1 text-xs font-medium ${
                    validationResult.valid
                      ? 'text-emerald-600 dark:text-emerald-400'
                      : 'text-destructive'
                  }`}
                >
                  {validationResult.valid ? (
                    <CheckCircle2 className="h-3.5 w-3.5" />
                  ) : (
                    <ShieldAlert className="h-3.5 w-3.5" />
                  )}
                  {validationResult.valid
                    ? `Verified (${validationResult.account_hint})`
                    : validationResult.message}
                </span>
              )}
            </div>
          </div>

          {/* Dedicated Folder / Immich Folder Setup */}
          <div className="space-y-3 rounded-lg border bg-muted/20 p-3.5">
            <p className="text-sm font-medium">Dedicated Folder Setup</p>
            <div className="grid gap-2 sm:grid-cols-3">
              {(
                [
                  ['dedicated', 'Auto-create folder'],
                  ['existing', 'Pick Immich / existing'],
                  ['later', 'Configure later'],
                ] as const
              ).map(([mode, label]) => (
                <button
                  key={mode}
                  type="button"
                  onClick={() => setFolderMode(mode)}
                  className={`rounded-md border px-2.5 py-2 text-xs font-medium transition-colors ${
                    folderMode === mode
                      ? 'border-primary bg-primary/15 text-primary'
                      : 'border-input bg-background text-muted-foreground hover:bg-accent'
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>

            {folderMode === 'dedicated' && (
              <div className="space-y-2 pt-1">
                <label className="text-xs font-medium">Writable Parent Root</label>
                <Select
                  className="w-full"
                  value={parentRoot}
                  onChange={(e) => setParentRoot(e.target.value)}
                >
                  {roots.map((r) => (
                    <option key={r.path} value={r.path} disabled={!r.writable}>
                      {r.path} {!r.writable ? '(read-only)' : '(writable)'}
                    </option>
                  ))}
                </Select>
                <p className="text-xs text-muted-foreground">
                  Creates a dedicated subfolder inside the selected root using the account slug and starts monitoring it automatically.
                </p>
              </div>
            )}

            {folderMode === 'existing' && (
              <div className="space-y-2 pt-1">
                <label className="text-xs font-medium">Existing Mounted Directory (e.g. Immich user folder)</label>
                <div className="flex gap-2">
                  <Input
                    value={bindPath}
                    readOnly
                    placeholder="Browse approved mounted roots..."
                    className="font-mono text-xs"
                  />
                  <Button variant="outline" size="sm" onClick={() => setBrowserOpen(true)}>
                    <FolderOpen className="h-4 w-4" />
                    Browse
                  </Button>
                </div>
              </div>
            )}

            {folderMode !== 'later' && (
              <div className="space-y-2 pt-1">
                <Input
                  value={folderName}
                  onChange={(e) => setFolderName(e.target.value)}
                  placeholder="Optional folder display name (defaults to '<Account> Folder')"
                />
                <div className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">
                    Apply Immich exclusions preset (skip thumbs, encoded-video, profile, backups)
                  </span>
                  <Switch checked={immichPreset} onCheckedChange={setImmichPreset} />
                </div>
              </div>
            )}
          </div>

          <div className="flex justify-end gap-2 border-t pt-3">
            <Button variant="outline" onClick={() => setAddOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={saving || validating || !addName.trim() || !addAuth.trim()}>
              <KeyRound className="h-4 w-4" />
              {saving ? 'Connecting...' : 'Validate & Connect'}
            </Button>
          </div>
        </form>
      </Modal>

      {/* Rotate Credentials Modal */}
      <Modal
        open={Boolean(rotatingAccount)}
        onClose={() => setRotatingAccount(null)}
        title={`Rotate Credentials: ${rotatingAccount?.name || ''}`}
        description="Validated against Google first. If validation fails, working credentials stay untouched."
      >
        <div className="space-y-4">
          <textarea
            value={rotateAuth}
            onChange={(e) => {
              setRotateAuth(e.target.value);
              setRotateValidation(null);
            }}
            rows={5}
            spellCheck={false}
            autoComplete="off"
            placeholder="Paste replacement Android AUTH_DATA..."
            className="w-full rounded-md border border-input bg-background p-3 font-mono text-xs shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          {rotateValidation && (
            <div
              className={`rounded-md border p-2.5 text-xs ${
                rotateValidation.valid
                  ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                  : 'border-destructive/40 bg-destructive/10 text-destructive'
              }`}
            >
              {rotateValidation.message}
            </div>
          )}
          <div className="flex justify-between gap-2 border-t pt-3">
            <Button
              variant="outline"
              size="sm"
              disabled={!rotateAuth.trim() || validating || rotating}
              onClick={() => void handleValidateRotate()}
            >
              {validating ? 'Validating...' : 'Validate Only'}
            </Button>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setRotatingAccount(null)}>
                Cancel
              </Button>
              <Button
                disabled={!rotateAuth.trim() || validating || rotating}
                onClick={() => void handleSaveRotate()}
              >
                {rotating ? 'Saving...' : 'Validate & Save'}
              </Button>
            </div>
          </div>
        </div>
      </Modal>

      {/* Rename Modal */}
      <Modal
        open={Boolean(renamingAccount)}
        onClose={() => setRenamingAccount(null)}
        title="Rename Connected Account"
        maxWidth="max-w-md"
      >
        <form onSubmit={handleRename} className="space-y-4">
          <Input value={renameText} onChange={(e) => setRenameText(e.target.value)} required />
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setRenamingAccount(null)}>
              Cancel
            </Button>
            <Button type="submit">Save Name</Button>
          </div>
        </form>
      </Modal>

      <DirectoryBrowserModal
        open={browserOpen}
        onClose={() => setBrowserOpen(false)}
        onSelect={(p, suggested) => {
          setBindPath(p);
          if (!folderName) setFolderName(suggested);
        }}
      />

      <ConfirmDialog
        open={Boolean(removingAccount)}
        onClose={() => setRemovingAccount(null)}
        onConfirm={() => void handleRemove()}
        title={`Disconnect "${removingAccount?.name}"?`}
        description="Removing this account deletes its managed secret file and pauses any monitored folders currently assigned to it so they cannot upload to another account. Local photos and upload history remain completely untouched."
        confirmLabel="Disconnect Account"
        destructive
        busy={removingAccount ? busyAccountId === removingAccount.id : false}
      />
    </div>
  );
}
