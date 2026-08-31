import { Subtract24Regular, Square24Regular, Dismiss24Regular } from '@fluentui/react-icons';

interface TitleBarProps {
  show?: boolean;
}

export function TitleBar({ show = true }: TitleBarProps) {
  if (!show) return null;

  const dragRegion: React.CSSProperties & { WebkitAppRegion?: string } = {
    WebkitAppRegion: 'drag',
  };
  const noDrag: React.CSSProperties & { WebkitAppRegion?: string } = {
    WebkitAppRegion: 'no-drag',
  };

  return (
    <div style={{
      height: 44,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'space-between',
      ...dragRegion,
      background: 'var(--thot-sidebar-bg)',
      borderBottom: '1px solid var(--thot-border-subtle)',
      flexShrink: 0,
    }}>
      <div style={{ width: 260 }} />
      <div style={{ display: 'flex', gap: 4, paddingRight: 12, ...noDrag }}>
        <button
          className="titlebar-btn"
          aria-label="Réduire"
          onClick={() => (window as unknown as { electron?: { minimize: () => void } }).electron?.minimize?.()}
        >
          <Subtract24Regular />
        </button>
        <button
          className="titlebar-btn"
          aria-label="Maximiser"
          onClick={() => (window as unknown as { electron?: { maximize: () => void } }).electron?.maximize?.()}
        >
          <Square24Regular />
        </button>
        <button
          className="titlebar-btn titlebar-close"
          aria-label="Fermer"
          onClick={() => (window as unknown as { electron?: { close: () => void } }).electron?.close?.()}
        >
          <Dismiss24Regular />
        </button>
      </div>
    </div>
  );
}

declare global {
  interface Window {
    electron?: {
      minimize: () => void;
      maximize: () => void;
      close: () => void;
    };
  }
}
