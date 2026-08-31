interface TagProps {
  variant: 'chat' | 'agent' | 'done' | 'running' | 'error';
  children?: string;
}

const variantMap: Record<string, { bg: string; color: string }> = {
  chat: { bg: '#E9E5DD', color: 'var(--thot-text-muted)' },
  agent: { bg: '#E9E5DD', color: 'var(--thot-text-muted)' },
  done: { bg: 'var(--thot-success-soft)', color: 'var(--thot-success)' },
  running: { bg: 'var(--thot-info-soft)', color: 'var(--thot-info)' },
  error: { bg: 'var(--thot-danger-soft)', color: 'var(--thot-danger)' },
};

export function Tag({ variant, children }: TagProps) {
  const v = variantMap[variant];
  return (
    <span style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: 4,
      padding: '2px 8px',
      borderRadius: 'var(--thot-radius-pill)',
      fontSize: 12,
      fontWeight: 500,
      background: v.bg,
      color: v.color,
      lineHeight: '18px',
    }}>
      {variant === 'running' && (
        <span style={{
          width: 6, height: 6, borderRadius: '50%',
          background: v.color,
          animation: 'pulse 1.5s ease infinite',
        }} />
      )}
      {children}
    </span>
  );
}
