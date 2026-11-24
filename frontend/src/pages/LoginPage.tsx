import React, { useState } from 'react';
import { CloudUpload, Lock, ShieldCheck } from 'lucide-react';
import { api } from '../lib/api';
import { Button, Card, CardContent, CardDescription, CardHeader, CardTitle, Input } from '../components/ui';

export function LoginPage({
  setupRequired,
  onAuthenticated,
}: {
  setupRequired: boolean;
  onAuthenticated: (username: string, csrfToken: string) => void;
}) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (setupRequired) {
      if (password.length < 8) {
        setError('Password must be at least 8 characters.');
        return;
      }
      if (password !== confirm) {
        setError('Passwords do not match.');
        return;
      }
    }
    setBusy(true);
    try {
      const res = setupRequired
        ? await api.sessionSetup(username, password)
        : await api.sessionLogin(username, password);
      onAuthenticated(res.username, res.csrf_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Authentication failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-md shadow-lg">
        <CardHeader className="space-y-2 text-center">
          <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-primary text-primary-foreground shadow">
            <CloudUpload className="h-6 w-6" />
          </div>
          <CardTitle className="text-xl">Google Photos Sync Manager</CardTitle>
          <CardDescription>
            {setupRequired
              ? 'Create your local administrator account to protect dashboard and credential controls.'
              : 'Sign in to monitor folders, uploads, settings, and authentication.'}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            {error && (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
                {error}
              </div>
            )}

            <div className="space-y-1.5">
              <label htmlFor="username" className="text-sm font-medium">
                Username
              </label>
              <Input
                id="username"
                autoComplete="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
              />
            </div>

            <div className="space-y-1.5">
              <label htmlFor="password" className="text-sm font-medium">
                Password
              </label>
              <Input
                id="password"
                type="password"
                autoComplete={setupRequired ? 'new-password' : 'current-password'}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={setupRequired ? 'Minimum 8 characters' : 'Enter your password'}
                required
              />
            </div>

            {setupRequired && (
              <div className="space-y-1.5">
                <label htmlFor="confirm" className="text-sm font-medium">
                  Confirm Password
                </label>
                <Input
                  id="confirm"
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  required
                />
              </div>
            )}

            <Button type="submit" className="w-full" disabled={busy}>
              {setupRequired ? <ShieldCheck className="h-4 w-4" /> : <Lock className="h-4 w-4" />}
              {busy ? 'Please wait...' : setupRequired ? 'Create Admin Account' : 'Sign In'}
            </Button>

            <p className="text-center text-xs text-muted-foreground">
              Session cookies are HttpOnly and CSRF-protected. Local media files are never deleted by default.
            </p>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
