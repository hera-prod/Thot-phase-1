import { useEffect, useState, useCallback } from 'react';
import { Dismiss24Regular, CheckmarkCircle24Regular, ErrorCircle24Regular, Info24Regular } from '@fluentui/react-icons';

export interface ToastData {
  id: string;
  type: 'success' | 'error' | 'info';
  title: string;
  message?: string;
}

let toastListeners: ((toast: ToastData) => void)[] = [];

export function showToast(toast: Omit<ToastData, 'id'>) {
  const id = Math.random().toString(36).slice(2);
  toastListeners.forEach((fn) => fn({ ...toast, id }));
}

const iconMap = {
  success: <CheckmarkCircle24Regular style={{ color: 'var(--thot-success)' }} />,
  error: <ErrorCircle24Regular style={{ color: 'var(--thot-danger)' }} />,
  info: <Info24Regular style={{ color: 'var(--thot-info)' }} />,
};

export function ToastContainer() {
  const [toasts, setToasts] = useState<ToastData[]>([]);

  useEffect(() => {
    const listener = (toast: ToastData) => {
      setToasts((prev) => [...prev, toast]);
      setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== toast.id));
      }, 4000);
    };
    toastListeners.push(listener);
    return () => {
      toastListeners = toastListeners.filter((l) => l !== listener);
    };
  }, []);

  const remove = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return (
    <div
      aria-live="polite"
      style={{
        position: 'fixed',
        bottom: 24,
        right: 24,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        zIndex: 1000,
        pointerEvents: 'none',
      }}
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          style={{
            display: 'flex',
            alignItems: 'flex-start',
            gap: 10,
            padding: 14,
            background: 'var(--thot-surface-raised)',
            borderRadius: 12,
            boxShadow: 'var(--thot-shadow-float)',
            pointerEvents: 'auto',
            animation: 'slideIn 200ms ease',
            maxWidth: 380,
          }}
        >
          <span style={{ flexShrink: 0, marginTop: 2 }}>{iconMap[toast.type]}</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 14, fontWeight: 600 }}>{toast.title}</div>
            {toast.message && (
              <div style={{ fontSize: 13, color: 'var(--thot-text-muted)', marginTop: 2 }}>{toast.message}</div>
            )}
          </div>
          <button
            onClick={() => remove(toast.id)}
            aria-label="Fermer"
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              color: 'var(--thot-text-faint)', padding: 2, flexShrink: 0,
            }}
          >
            <Dismiss24Regular />
          </button>
        </div>
      ))}
    </div>
  );
}
