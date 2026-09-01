import { ReactNode } from 'react';
import { TitleBar } from './TitleBar';
import { Sidebar } from './Sidebar';

interface AppShellProps {
  children: ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  return (
    <div className="appShell" style={{
      background: 'var(--thot-canvas)',
      borderRadius: 'var(--thot-radius-window)',
    }}>
      {/* Phase 1 : TitleBar masquée (show=false) — Tauri gère la fenêtre */}
      <TitleBar show={false} />
      <div className="appMain" style={{ display: 'flex', flex: 1, overflow: 'hidden', minHeight: 0 }}>
        <Sidebar />
        <main
          role="main"
          className="appMain"
        >
          {children}
        </main>
      </div>
    </div>
  );
}
