import React, { useState } from 'react';
import {
  Activity,
  AlertOctagon,
  Bell,
  CheckCheck,
  CloudUpload,
  FolderSync,
  HeartPulse,
  History,
  Image,
  KeyRound,
  LayoutDashboard,
  ListOrdered,
  LogOut,
  Menu,
  Moon,
  ScrollText,
  Settings,
  Sun,
  X,
} from 'lucide-react';
import type { NavPage, NotificationItem } from '../types';
import { cn, formatRelativeTime } from '../lib/utils';
import { Badge, Button } from './ui';

const NAV_ITEMS: Array<{ id: NavPage; label: string; icon: React.ReactNode }> = [
  { id: 'overview', label: 'Overview', icon: <LayoutDashboard className="h-4 w-4" /> },
  { id: 'immich', label: 'Immich Sync', icon: <Image className="h-4 w-4" /> },
  { id: 'folders', label: 'Monitored Folders', icon: <FolderSync className="h-4 w-4" /> },
  { id: 'queue', label: 'Upload Queue', icon: <ListOrdered className="h-4 w-4" /> },
  { id: 'history', label: 'Upload History', icon: <History className="h-4 w-4" /> },
  { id: 'failed', label: 'Failed Uploads', icon: <AlertOctagon className="h-4 w-4" /> },
  { id: 'auth', label: 'Google Accounts', icon: <KeyRound className="h-4 w-4" /> },
  { id: 'settings', label: 'Upload Settings', icon: <Settings className="h-4 w-4" /> },
  { id: 'logs', label: 'Activity Logs', icon: <ScrollText className="h-4 w-4" /> },
  { id: 'health', label: 'System Health', icon: <HeartPulse className="h-4 w-4" /> },
];

export function Layout({
  page,
  onNavigate,
  theme,
  onToggleTheme,
  username,
  onLogout,
  activeUploads,
  failedCount,
  sseConnected,
  notifications,
  unreadCount,
  onMarkRead,
  onMarkAllRead,
  onDismissNotification,
  children,
}: {
  page: NavPage;
  onNavigate: (page: NavPage) => void;
  theme: 'light' | 'dark';
  onToggleTheme: () => void;
  username: string;
  onLogout: () => void;
  activeUploads: number;
  failedCount: number;
  sseConnected: boolean;
  notifications: NotificationItem[];
  unreadCount: number;
  onMarkRead: (id: number) => void;
  onMarkAllRead: () => void;
  onDismissNotification: (id: number) => void;
  children: React.ReactNode;
}) {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [notifOpen, setNotifOpen] = useState(false);

  const renderNav = (onPicked?: () => void) => (
    <nav className="flex-1 space-y-1 p-3" aria-label="Primary navigation">
      {NAV_ITEMS.map((item) => {
        const active = page === item.id;
        return (
          <button
            key={item.id}
            type="button"
            onClick={() => {
              onNavigate(item.id);
              onPicked?.();
            }}
            className={cn(
              'flex w-full items-center justify-between rounded-lg px-3 py-2 text-sm font-medium transition-colors',
              active
                ? 'bg-primary text-primary-foreground shadow-xs'
                : 'text-muted-foreground hover:bg-accent hover:text-foreground'
            )}
          >
            <span className="flex items-center gap-2.5">
              {item.icon}
              {item.label}
            </span>
            {item.id === 'failed' && failedCount > 0 && (
              <span
                className={cn(
                  'rounded-full px-2 py-0.5 text-xs font-semibold',
                  active ? 'bg-white/20 text-white' : 'bg-red-500/15 text-red-600 dark:text-red-400'
                )}
              >
                {failedCount}
              </span>
            )}
            {item.id === 'queue' && activeUploads > 0 && (
              <span
                className={cn(
                  'rounded-full px-2 py-0.5 text-xs font-semibold',
                  active ? 'bg-white/20 text-white' : 'bg-primary/15 text-primary'
                )}
              >
                {activeUploads}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );

  return (
    <div className="flex min-h-screen bg-background">
      {/* Desktop Sidebar */}
      <aside className="hidden w-64 shrink-0 flex-col border-r bg-card lg:flex">
        <div className="flex h-16 items-center gap-2.5 border-b px-5">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-primary-foreground shadow-xs">
            <CloudUpload className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm font-bold leading-tight">Photos Sync</p>
            <p className="truncate text-xs text-muted-foreground">Self-Hosted Manager</p>
          </div>
        </div>
        {renderNav()}
        <div className="border-t p-4">
          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span className="flex items-center gap-1.5">
              <span
                className={cn(
                  'h-2 w-2 rounded-full',
                  sseConnected ? 'bg-emerald-500' : 'bg-amber-500'
                )}
              />
              {sseConnected ? 'Live stream active' : 'Reconnecting...'}
            </span>
            <span className="font-mono">{username}</span>
          </div>
        </div>
      </aside>

      {/* Mobile Drawer */}
      {mobileOpen && (
        <div className="fixed inset-0 z-50 flex lg:hidden">
          <div
            className="fixed inset-0 bg-black/50 backdrop-blur-xs"
            onClick={() => setMobileOpen(false)}
          />
          <aside className="relative z-10 flex w-72 max-w-[85vw] flex-col bg-card shadow-xl">
            <div className="flex h-16 items-center justify-between border-b px-4">
              <div className="flex items-center gap-2.5">
                <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
                  <CloudUpload className="h-4 w-4" />
                </div>
                <span className="text-sm font-bold">Photos Sync Manager</span>
              </div>
              <Button variant="ghost" size="icon" onClick={() => setMobileOpen(false)} aria-label="Close menu">
                <X className="h-4 w-4" />
              </Button>
            </div>
            {renderNav(() => setMobileOpen(false))}
          </aside>
        </div>
      )}

      {/* Main Content */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-16 items-center justify-between gap-3 border-b bg-card/90 px-4 backdrop-blur sm:px-6">
          <div className="flex items-center gap-3">
            <Button
              variant="ghost"
              size="icon"
              className="lg:hidden"
              onClick={() => setMobileOpen(true)}
              aria-label="Open navigation menu"
            >
              <Menu className="h-5 w-5" />
            </Button>
            <h1 className="text-base font-semibold sm:text-lg">
              {NAV_ITEMS.find((i) => i.id === page)?.label}
            </h1>
          </div>

          <div className="flex items-center gap-2">
            {activeUploads > 0 && (
              <button
                type="button"
                onClick={() => onNavigate('queue')}
                className="hidden items-center gap-1.5 rounded-full border border-primary/30 bg-primary/10 px-3 py-1 text-xs font-medium text-primary sm:inline-flex"
              >
                <Activity className="h-3.5 w-3.5 animate-pulse" />
                {activeUploads} uploading
              </button>
            )}

            {/* Notifications */}
            <div className="relative">
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setNotifOpen((v) => !v)}
                aria-label="Toggle notifications"
              >
                <Bell className="h-4 w-4" />
                {unreadCount > 0 && (
                  <span className="absolute right-1.5 top-1.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-destructive px-1 text-[10px] font-bold text-destructive-foreground">
                    {unreadCount > 9 ? '9+' : unreadCount}
                  </span>
                )}
              </Button>

              {notifOpen && (
                <div className="absolute right-0 mt-2 w-80 max-w-[90vw] rounded-xl border bg-card shadow-xl sm:w-96">
                  <div className="flex items-center justify-between border-b px-4 py-3">
                    <span className="text-sm font-semibold">Notifications</span>
                    {unreadCount > 0 && (
                      <Button variant="ghost" size="sm" onClick={onMarkAllRead}>
                        <CheckCheck className="h-3.5 w-3.5" />
                        Mark all read
                      </Button>
                    )}
                  </div>
                  <div className="max-h-80 overflow-y-auto divide-y">
                    {notifications.length === 0 ? (
                      <p className="p-6 text-center text-sm text-muted-foreground">
                        No active notifications.
                      </p>
                    ) : (
                      notifications.map((n) => (
                        <div
                          key={n.id}
                          className={cn(
                            'flex items-start justify-between gap-2 p-3.5 text-sm',
                            !n.read && 'bg-primary/5'
                          )}
                        >
                          <div
                            className="min-w-0 flex-1 cursor-pointer"
                            onClick={() => !n.read && onMarkRead(n.id)}
                          >
                            <div className="flex items-center gap-2">
                              <Badge
                                variant={
                                  n.severity === 'error'
                                    ? 'destructive'
                                    : n.severity === 'warning'
                                      ? 'warning'
                                      : n.severity === 'success'
                                        ? 'success'
                                        : 'info'
                                }
                              >
                                {n.severity}
                              </Badge>
                              <span className="text-xs text-muted-foreground">
                                {formatRelativeTime(n.updated_at)}
                              </span>
                            </div>
                            <p className="mt-1 font-medium">{n.title}</p>
                            <p className="mt-0.5 text-xs text-muted-foreground">{n.message}</p>
                          </div>
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 shrink-0"
                            onClick={() => onDismissNotification(n.id)}
                            aria-label="Dismiss notification"
                          >
                            <X className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      ))
                    )}
                  </div>
                </div>
              )}
            </div>

            <Button
              variant="ghost"
              size="icon"
              onClick={onToggleTheme}
              aria-label="Toggle color theme"
            >
              {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>

            <Button variant="outline" size="sm" onClick={onLogout}>
              <LogOut className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Sign out</span>
            </Button>
          </div>
        </header>

        <main className="mx-auto w-full max-w-7xl flex-1 p-4 sm:p-6">{children}</main>
      </div>
    </div>
  );
}
