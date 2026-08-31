import { ReactNode } from 'react';
import { TitleBar } from './TitleBar';
import { Sidebar } from './Sidebar';

interface AppShellProps {
  children: ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  return (
    <div style={{
      width: '100vw',
      height: '100vh',
      background: 'var(--thot-canvas)',
      borderRadius: 'var(--thot-radius-window)',
      overflow: 'hidden',
      display: 'flex',
      flexDirection: 'column',
    }}>
      {/* Phase 1 : TitleBar masquée (show=false) — Tauri gère la fenêtre */}
      <TitleBar show={false} />
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden', minHeight: 0 }}>
        <Sidebar />
        <main
          role="main"
          style={{
            flex: 1,
            overflow: 'auto',
            background: 'var(--thot-canvas)',
            display: 'flex',
            flexDirection: 'column',
            minHeight: 0,
            minWidth: 0,
          }}
        >
          {children}
        </main>
      </div>
    </div>
  );
}
