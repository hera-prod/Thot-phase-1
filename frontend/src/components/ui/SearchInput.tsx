import { Search24Regular } from '@fluentui/react-icons';

interface SearchInputProps {
  placeholder?: string;
  value?: string;
  onChange?: (value: string) => void;
  width?: string | number;
}

export function SearchInput({ placeholder = 'Rechercher…', value, onChange, width }: SearchInputProps) {
  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      gap: 8,
      height: 36,
      borderRadius: 10,
      background: 'var(--thot-surface-raised)',
      border: '1px solid var(--thot-border)',
      padding: '0 12px',
      width: width || '100%',
      transition: 'var(--thot-transition-fast)',
    }}>
      <Search24Regular style={{ fontSize: 16, color: 'var(--thot-text-faint)', flexShrink: 0 }} />
      <input
        type="text"
        value={value}
        onChange={(e) => onChange?.(e.target.value)}
        placeholder={placeholder}
        aria-label={placeholder}
        style={{
          flex: 1,
          border: 'none',
          outline: 'none',
          background: 'transparent',
          fontSize: 13,
          color: 'var(--thot-text)',
          fontFamily: 'var(--thot-font-family)',
        }}
      />
    </div>
  );
}
