import { ReactNode } from 'react';
import { TitleBar } from './TitleBar';
import { Sidebar } from './Sidebar';

interface AppShellProps {
  children: ReactNode;
}

export function AppShell({ children }: AppShellProps) {
  return (
    <div className="appShell">
      <TitleBar show={false} />
      <div className="appShellBody">
        <Sidebar />
        <main role="main" className="appMain appContent">
          {children}
        </main>
      </div>
    </div>
  );
}
