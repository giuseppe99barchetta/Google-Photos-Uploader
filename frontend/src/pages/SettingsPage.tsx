import React, { useEffect, useState } from 'react';
import { AlertTriangle, CheckCircle2, Info, Save } from 'lucide-react';
import { api } from '../lib/api';
import type { AppSettings } from '../types';
import {
  Badge,
  Button,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
  ConfirmDialog,
  Input,
  Select,
  Skeleton,
  Switch,
  useToast,
} from '../components/ui';

export function SettingsPage() {
  const { toast } = useToast();
  const [settings, setSettings] = useState<AppSettings | null>(null);
  const [watcherHealthy, setWatcherHealthy] = useState(true);
  const [watcherCounts, setWatcherCounts] = useState({ active: 0, configured: 0 });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [confirmDeleteOpen, setConfirmDeleteOpen] = useState(false);

  const load = async () => {
    try {
      const res = await api.getSettings();
      setSettings(res.settings);
      setWatcherHealthy(res.watcher.healthy);
      setWatcherCounts({
        active: res.watcher.active_watchers,
        configured: res.watcher.configured_watchers,
      });
    } catch (err) {
      toast({ variant: 'error', title: 'Failed to load settings', description: err instanceof Error ? err.message : undefined });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const handleSave = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!settings) return;
    if (settings.stability_timeout < settings.stable_seconds) {
      toast({ variant: 'error', title: 'Validation error', description: 'Stability timeout must be at least stable duration.' });
      return;
    }
    if (settings.retry_max < settings.retry_base) {
      toast({ variant: 'error', title: 'Validation error', description: 'Max retry delay must be at least base retry delay.' });
      return;
    }

    setSaving(true);
    try {
      const res = await api.updateSettings(settings);
      setSettings(res.settings);
      toast({
        variant: 'success',
        title: 'Settings saved',
        description: 'Changes persist across restarts and apply safely to the next upload batch.',
      });
    } catch (err) {
      toast({ variant: 'error', title: 'Failed to save settings', description: err instanceof Error ? err.message : undefined });
    } finally {
      setSaving(false);
    }
  };

  if (loading || !settings) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-44" />
        <Skeleton className="h-44" />
      </div>
    );
  }

  return (
    <form onSubmit={handleSave} className="space-y-6">
      {/* Concurrency */}
      <Card>
        <CardHeader>
          <CardTitle>Concurrency & Batch Engine</CardTitle>
          <CardDescription>
            Controls GPMC batch size and parallel uploads per batch. Changes take effect safely on the next batch.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Concurrent Uploads (1–8)</label>
              <Input
                type="number"
                min={1}
                max={8}
                value={settings.upload_threads}
                onChange={(e) => setSettings({ ...settings, upload_threads: Number(e.target.value) })}
              />
              <p className="text-xs text-muted-foreground">Recommended default: 2</p>
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Batch Size (1–64 files)</label>
              <Input
                type="number"
                min={1}
                max={64}
                value={settings.upload_batch_size}
                onChange={(e) => setSettings({ ...settings, upload_batch_size: Number(e.target.value) })}
              />
              <p className="text-xs text-muted-foreground">Recommended default: 4</p>
            </div>
          </div>
          <div className="flex items-start gap-2 rounded-lg border bg-muted/30 p-3 text-xs text-muted-foreground">
            <Info className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
            <span>
              Higher concurrent upload values increase network load and may trigger Google Photos API throttling.
              The application keeps a single queue worker so no file can be claimed twice.
            </span>
          </div>
        </CardContent>
      </Card>

      {/* Folder Scanning */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-2">
          <div>
            <CardTitle>Folder Scanning & File Stability</CardTitle>
            <CardDescription>Ensures copied or moving files finish writing before queueing for upload.</CardDescription>
          </div>
          <Badge variant={watcherHealthy ? 'success' : 'warning'}>
            {watcherHealthy ? <CheckCircle2 className="h-3 w-3" /> : <AlertTriangle className="h-3 w-3" />}
            Watchers: {watcherCounts.active}/{watcherCounts.configured}
          </Badge>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Scan Interval (seconds)</label>
              <Input
                type="number"
                min={5}
                max={86400}
                value={settings.scan_interval}
                onChange={(e) => setSettings({ ...settings, scan_interval: Number(e.target.value) })}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">File Stability Duration (s)</label>
              <Input
                type="number"
                min={1}
                max={300}
                step="0.5"
                value={settings.stable_seconds}
                onChange={(e) => setSettings({ ...settings, stable_seconds: Number(e.target.value) })}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Max Stability Wait (s)</label>
              <Input
                type="number"
                min={10}
                max={3600}
                value={settings.stability_timeout}
                onChange={(e) => setSettings({ ...settings, stability_timeout: Number(e.target.value) })}
              />
            </div>
          </div>

          <div className="flex items-center justify-between rounded-lg border p-3">
            <div>
              <p className="text-sm font-medium">Default Recursive Scanning</p>
              <p className="text-xs text-muted-foreground">Default recursive setting for newly added folders.</p>
            </div>
            <Switch
              checked={settings.default_recursive}
              onCheckedChange={(v) => setSettings({ ...settings, default_recursive: v })}
            />
          </div>
        </CardContent>
      </Card>

      {/* Upload Behaviour */}
      <Card>
        <CardHeader>
          <CardTitle>Upload Behaviour</CardTitle>
          <CardDescription>Configure quality, quota, duplicate detection, and local file retention.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Upload Quality</label>
              <Select
                className="w-full"
                value={settings.upload_quality}
                onChange={(e) =>
                  setSettings({ ...settings, upload_quality: e.target.value as 'original' | 'saver' })
                }
              >
                <option value="original">Original Quality</option>
                <option value="saver">Storage Saver</option>
              </Select>
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Filename Conflict Handling</label>
              <Select
                className="w-full"
                value={settings.filename_conflict_policy}
                onChange={(e) =>
                  setSettings({
                    ...settings,
                    filename_conflict_policy: e.target.value as 'hash_dedup' | 'skip_existing_filename',
                  })
                }
              >
                <option value="hash_dedup">Content SHA-1 Hash Deduplication (Recommended)</option>
                <option value="skip_existing_filename">Skip Existing Filenames</option>
              </Select>
            </div>
          </div>

          <div className="divide-y rounded-lg border">
            <div className="flex items-center justify-between p-3.5">
              <div>
                <p className="text-sm font-medium">Hash-Based Duplicate Detection</p>
                <p className="text-xs text-muted-foreground">
                  Uses GPMC SHA-1 remote hash lookup to skip files already in Google Photos.
                </p>
              </div>
              <Switch
                checked={settings.duplicate_detection}
                onCheckedChange={(v) => setSettings({ ...settings, duplicate_detection: v })}
              />
            </div>

            <div className="flex items-center justify-between p-3.5">
              <div>
                <p className="text-sm font-medium">Use Google Account Storage Quota</p>
                <p className="text-xs text-muted-foreground">Passed directly to GPMC use_quota option.</p>
              </div>
              <Switch
                checked={settings.use_quota}
                onCheckedChange={(v) => setSettings({ ...settings, use_quota: v })}
              />
            </div>

            <div className="flex items-center justify-between p-3.5">
              <div>
                <p className="flex items-center gap-2 text-sm font-medium">
                  Delete Local Original After Confirmed Upload
                  <Badge variant={settings.delete_after_upload ? 'destructive' : 'success'}>
                    {settings.delete_after_upload ? 'Enabled' : 'Disabled (Safe)'}
                  </Badge>
                </p>
                <p className="text-xs text-muted-foreground">
                  Disabled by default so your local originals are always preserved.
                </p>
              </div>
              <Switch
                checked={settings.delete_after_upload}
                onCheckedChange={(v) => {
                  if (v) {
                    setConfirmDeleteOpen(true);
                  } else {
                    setSettings({ ...settings, delete_after_upload: false });
                  }
                }}
              />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Retry Behaviour */}
      <Card>
        <CardHeader>
          <CardTitle>Retry Behaviour</CardTitle>
          <CardDescription>Exponential backoff configuration for transient upload errors.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-3">
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Maximum Attempts (1–50)</label>
              <Input
                type="number"
                min={1}
                max={50}
                value={settings.max_attempts}
                onChange={(e) => setSettings({ ...settings, max_attempts: Number(e.target.value) })}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Base Retry Delay (s)</label>
              <Input
                type="number"
                min={1}
                max={3600}
                value={settings.retry_base}
                onChange={(e) => setSettings({ ...settings, retry_base: Number(e.target.value) })}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Max Retry Delay (s)</label>
              <Input
                type="number"
                min={5}
                max={86400}
                value={settings.retry_max}
                onChange={(e) => setSettings({ ...settings, retry_max: Number(e.target.value) })}
              />
            </div>
          </div>
          <div className="flex items-center justify-between rounded-lg border p-3">
            <div>
              <p className="text-sm font-medium">Retry Failed Jobs Automatically</p>
              <p className="text-xs text-muted-foreground">
                Re-activate exhausted jobs after cooldown during periodic folder reconciliation.
              </p>
            </div>
            <Switch
              checked={settings.auto_retry_failed}
              onCheckedChange={(v) => setSettings({ ...settings, auto_retry_failed: v })}
            />
          </div>
        </CardContent>
      </Card>

      {/* Notification Preferences */}
      <Card>
        <CardHeader>
          <CardTitle>In-Dashboard Notification Preferences</CardTitle>
          <CardDescription>Duplicate alerts for the same unresolved issue are automatically suppressed.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-2">
            {(
              [
                ['notify_upload_completed', 'Upload completed alerts'],
                ['notify_upload_failed', 'Upload failed alerts'],
                ['notify_auth_issues', 'Authentication invalid / expired alerts'],
                ['notify_folder_unavailable', 'Folder unavailable alerts'],
                ['notify_low_disk', 'Low disk space alerts'],
                ['notify_watcher_stopped', 'Folder watcher stopped alerts'],
              ] as const
            ).map(([key, label]) => (
              <div key={key} className="flex items-center justify-between rounded-lg border p-3 text-sm">
                <span>{label}</span>
                <Switch
                  checked={settings[key]}
                  onCheckedChange={(v) => setSettings({ ...settings, [key]: v })}
                />
              </div>
            ))}
          </div>
          <div className="max-w-xs space-y-1.5 pt-1">
            <label className="text-sm font-medium">Low Disk Space Threshold (GB)</label>
            <Input
              type="number"
              min={0.1}
              max={500}
              step="0.5"
              value={settings.low_disk_threshold_gb}
              onChange={(e) => setSettings({ ...settings, low_disk_threshold_gb: Number(e.target.value) })}
            />
          </div>
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button type="submit" disabled={saving}>
          <Save className="h-4 w-4" />
          {saving ? 'Saving...' : 'Save Settings'}
        </Button>
      </div>

      <ConfirmDialog
        open={confirmDeleteOpen}
        onClose={() => setConfirmDeleteOpen(false)}
        onConfirm={() => {
          setSettings({ ...settings, delete_after_upload: true });
          setConfirmDeleteOpen(false);
        }}
        title="Enable Local File Deletion?"
        description="Enabling this option will delete original local photos and videos from mounted folders after Google Photos confirms each upload. Are you sure you want to enable deletion?"
        confirmLabel="Enable Deletion"
        destructive
      />
    </form>
  );
}
