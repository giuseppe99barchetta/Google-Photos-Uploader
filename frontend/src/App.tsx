import { useCallback, useEffect, useState } from 'react';
import { api, setCsrfToken, setUnauthorizedHandler } from './lib/api';
import type { LiveSnapshot, NavPage, NotificationItem, SessionStatus } from './types';
import { Layout } from './components/Layout';
import { Skeleton, ToastProvider } from './components/ui';
import { AuthPage } from './pages/AuthPage';
import { FailedPage } from './pages/FailedPage';
import { FoldersPage } from './pages/FoldersPage';
import { HealthPage } from './pages/HealthPage';
import { HistoryPage } from './pages/HistoryPage';
import { ImmichPage } from './pages/ImmichPage';
import { LoginPage } from './pages/LoginPage';
import { LogsPage } from './pages/LogsPage';
import { OverviewPage } from './pages/OverviewPage';
import { QueuePage } from './pages/QueuePage';
import { SettingsPage } from './pages/SettingsPage';

function DashboardApp() {
  const [session, setSession] = useState<SessionStatus | null>(null);
  const [page, setPage] = useState<NavPage>('overview');
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = window.localStorage.getItem('gphotos_theme');
    if (saved === 'light' || saved === 'dark') return saved;
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  });

  const [live, setLive] = useState<LiveSnapshot | null>(null);
  const [sseConnected, setSseConnected] = useState(false);
  const [refreshSignal, setRefreshSignal] = useState(0);
  const [failedCount, setFailedCount] = useState(0);
  const [maxThreads, setMaxThreads] = useState(2);
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [unreadCount, setUnreadCount] = useState(0);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', theme === 'dark');
    window.localStorage.setItem('gphotos_theme', theme);
  }, [theme]);

  const checkSession = useCallback(async () => {
    try {
      const st = await api.sessionStatus();
      setCsrfToken(st.csrf_token);
      setSession(st);
    } catch {
      setSession({ authenticated: false, setup_required: false, username: null, csrf_token: null });
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setSession((prev) => ({
        authenticated: false,
        setup_required: prev?.setup_required ?? false,
        username: null,
        csrf_token: null,
      }));
    });
    void checkSession();
  }, [checkSession]);

  const refreshMeta = useCallback(async () => {
    try {
      const [overview, notifs] = await Promise.all([api.overview(), api.listNotifications()]);
      setFailedCount(overview.stats.failed + overview.stats.retrying);
      setMaxThreads(overview.upload_threads);
      setNotifications(notifs.items);
      setUnreadCount(notifs.unread_count);
    } catch {
      // Handled by unauthorized handler if session expired.
    }
  }, []);

  // Subscribe to Server-Sent Events when authenticated.
  useEffect(() => {
    if (!session?.authenticated) {
      setSseConnected(false);
      return;
    }
    void refreshMeta();

    const source = new EventSource('/api/events', { withCredentials: true });
    source.onopen = () => setSseConnected(true);
    source.onerror = () => setSseConnected(false);

    const handleSnapshot = (ev: MessageEvent) => {
      try {
        const parsed = JSON.parse(ev.data) as { data?: { live?: LiveSnapshot; stats?: { failed: number; retrying: number } } };
        if (parsed.data?.live) setLive(parsed.data.live);
        if (parsed.data?.stats) {
          setFailedCount(parsed.data.stats.failed + parsed.data.stats.retrying);
        }
      } catch {
        // Ignore malformed frames.
      }
    };

    const handleLive = (ev: MessageEvent) => {
      try {
        const parsed = JSON.parse(ev.data) as { data?: LiveSnapshot };
        if (parsed.data) setLive(parsed.data);
      } catch {
        // Ignore malformed frames.
      }
    };

    const handleInvalidate = () => {
      setRefreshSignal((n) => n + 1);
      void refreshMeta();
    };

    source.addEventListener('snapshot', handleSnapshot);
    source.addEventListener('batch_started', handleLive);
    source.addEventListener('upload_progress', handleLive);
    source.addEventListener('batch_completed', (ev: MessageEvent) => {
      handleLive(ev);
      handleInvalidate();
    });
    source.addEventListener('queue_updated', handleInvalidate);
    source.addEventListener('folders_updated', handleInvalidate);
    source.addEventListener('auth_updated', handleInvalidate);
    source.addEventListener('settings_updated', handleInvalidate);
    source.addEventListener('notification', handleInvalidate);

    return () => {
      source.close();
      setSseConnected(false);
    };
  }, [session?.authenticated, refreshMeta]);

  const handleLogout = async () => {
    try {
      await api.sessionLogout();
    } catch {
      // Proceed with local sign-out regardless.
    }
    setCsrfToken(null);
    setSession({ authenticated: false, setup_required: false, username: null, csrf_token: null });
  };

  if (!session) {
    return (
      <div className="flex min-h-screen items-center justify-center p-8">
        <Skeleton className="h-48 w-full max-w-md" />
      </div>
    );
  }

  if (!session.authenticated) {
    return (
      <LoginPage
        setupRequired={session.setup_required}
        onAuthenticated={(username, token) => {
          setCsrfToken(token);
          setSession({ authenticated: true, setup_required: false, username, csrf_token: token });
        }}
      />
    );
  }

  return (
    <Layout
      page={page}
      onNavigate={setPage}
      theme={theme}
      onToggleTheme={() => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))}
      username={session.username || 'admin'}
      onLogout={() => void handleLogout()}
      activeUploads={live?.active_count ?? 0}
      failedCount={failedCount}
      sseConnected={sseConnected}
      notifications={notifications}
      unreadCount={unreadCount}
      onMarkRead={async (id) => {
        await api.markNotificationRead(id);
        await refreshMeta();
      }}
      onMarkAllRead={async () => {
        await api.markAllNotificationsRead();
        await refreshMeta();
      }}
      onDismissNotification={async (id) => {
        await api.dismissNotification(id);
        await refreshMeta();
      }}
    >
      {page === 'overview' && (
        <OverviewPage liveOverride={live} refreshSignal={refreshSignal} onNavigate={setPage} />
      )}
      {page === 'immich' && <ImmichPage refreshSignal={refreshSignal} onNavigate={setPage} />}
      {page === 'folders' && <FoldersPage refreshSignal={refreshSignal} />}
      {page === 'queue' && (
        <QueuePage liveOverride={live} refreshSignal={refreshSignal} maxThreads={maxThreads} />
      )}
      {page === 'history' && <HistoryPage refreshSignal={refreshSignal} />}
      {page === 'failed' && <FailedPage refreshSignal={refreshSignal} />}
      {page === 'auth' && <AuthPage refreshSignal={refreshSignal} />}
      {page === 'settings' && <SettingsPage />}
      {page === 'logs' && <LogsPage refreshSignal={refreshSignal} />}
      {page === 'health' && <HealthPage refreshSignal={refreshSignal} />}
    </Layout>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <DashboardApp />
    </ToastProvider>
  );
}
