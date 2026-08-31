import { ReactNode } from 'react';

interface ButtonProps {
  variant?: 'primary' | 'creative' | 'secondary' | 'destructive' | 'icon';
  size?: 'sm' | 'md' | 'lg';
  disabled?: boolean;
  onClick?: () => void;
  ariaLabel?: string;
  children?: ReactNode;
  style?: React.CSSProperties;
}

const variantStyles: Record<string, React.CSSProperties> = {
  primary: { background: 'var(--thot-gold)', color: '#fff', border: 'none' },
  creative: { background: 'var(--thot-violet)', color: '#fff', border: 'none' },
  secondary: { background: 'var(--thot-surface-raised)', color: 'var(--thot-text)', border: '1px solid var(--thot-border)' },
  destructive: { background: 'transparent', color: 'var(--thot-danger)', border: '1px solid var(--thot-danger)' },
  icon: { background: 'transparent', color: 'var(--thot-text-muted)', border: 'none' },
};

const sizeMap: Record<string, { h: number; fz: number; px: number }> = {
  sm: { h: 32, fz: 13, px: 12 },
  md: { h: 36, fz: 14, px: 16 },
  lg: { h: 40, fz: 14, px: 20 },
};

export function Button({ variant = 'secondary', size = 'md', disabled, onClick, ariaLabel, children, style }: ButtonProps) {
  const sz = sizeMap[size];
  const base: React.CSSProperties = {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 8,
    height: sz.h,
    padding: `0 ${sz.px}px`,
    fontSize: sz.fz,
    fontWeight: 600,
    fontFamily: 'var(--thot-font-family)',
    borderRadius: 'var(--thot-radius-sm)',
    cursor: disabled ? 'not-allowed' : 'pointer',
    opacity: disabled ? 0.5 : 1,
    transition: 'var(--thot-transition-fast)',
    outline: 'none',
    whiteSpace: 'nowrap',
    ...variantStyles[variant],
    ...style,
  };

  return (
    <button
      style={base}
      disabled={disabled}
      onClick={onClick}
      aria-label={ariaLabel}
      onMouseDown={(e) => {
        if (!disabled) e.currentTarget.style.transform = `scale(var(--thot-active-scale))`;
      }}
      onMouseUp={(e) => e.currentTarget.style.transform = 'scale(1)'}
      onMouseEnter={(e) => {
        if (variant === 'primary' && !disabled) e.currentTarget.style.background = 'var(--thot-gold-hover)';
        if (variant === 'secondary' && !disabled) e.currentTarget.style.background = 'var(--thot-surface-2)';
      }}
      onMouseLeave={(e) => {
        if (variant === 'primary') e.currentTarget.style.background = 'var(--thot-gold)';
        if (variant === 'secondary') e.currentTarget.style.background = 'var(--thot-surface-raised)';
      }}
    >
      {children}
    </button>
  );
}
