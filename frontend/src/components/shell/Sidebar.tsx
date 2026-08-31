// Sidebar Phase 1 — Chat seul + VLM (pas de fonction Agent séparée)
import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  Chat24Regular,
  PanelLeftContract24Regular,
  PanelLeftExpand24Regular,
  ChevronRight24Regular,
} from '@fluentui/react-icons';

const NAV_ITEMS = [
  { path: '/chat', icon: <Chat24Regular />, label: 'Chat' },
  { path: '/history', icon: <Chat24Regular />, label: 'Historique' },
] as const;

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();

  // Active = prefix match (/chat ou /agent)
  const activePath = '/' + location.pathname.split('/')[1];

  return (
    <aside
      role="navigation"
      aria-label="Navigation principale"
      style={{
        width: collapsed ? 64 : 260,
        flexShrink: 0,
        background: 'var(--thot-sidebar-bg)',
        display: 'flex',
        flexDirection: 'column',
        gap: 4,
        padding: collapsed ? '16px 4px' : '16px 12px',
        borderRight: '1px solid var(--thot-border-subtle)',
        transition: 'var(--thot-transition-fast)',
        overflow: 'hidden',
      }}
    >
      {/* Brand header */}
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: collapsed ? 0 : 8,
        padding: 8,
        marginBottom: 12,
        justifyContent: collapsed ? 'center' : 'space-between',
      }}>
        {!collapsed && (
          <>
            <img
              src="/assets/brand/Thot_logo.svg"
              alt="Thot"
              onError={(e) => { (e.currentTarget as HTMLImageElement).style.display = 'none'; }}
              style={{ width: 36, height: 36, flexShrink: 0, objectFit: 'contain', display: 'block' }}
            />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: 'var(--thot-text-logo)', fontWeight: 700, lineHeight: 1.2 }}>Thot</div>
              <div style={{ fontSize: 12, color: 'var(--thot-text-faint)' }}>v0.8 → Phase 1</div>
            </div>
          </>
        )}
        <button
          onClick={() => setCollapsed(!collapsed)}
          aria-label={collapsed ? 'Déplier la sidebar' : 'Replier la sidebar'}
          style={{
            width: 32, height: 32, borderRadius: 'var(--thot-radius-pill)',
            border: 'none', background: 'transparent', cursor: 'pointer',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'var(--thot-text-muted)', flexShrink: 0,
            transition: 'var(--thot-transition-fast)',
          }}
          onMouseEnter={(e) => e.currentTarget.style.background = 'var(--thot-surface-2)'}
          onMouseLeave={(e) => e.currentTarget.style.background = 'transparent'}
        >
          {collapsed ? <PanelLeftExpand24Regular /> : <PanelLeftContract24Regular />}
        </button>
      </div>

      {/* Navigation — 2 items only */}
      <nav role="tablist" style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {NAV_ITEMS.map((item) => {
          const isActive = activePath === item.path;
          return (
            <button
              key={item.path}
              role="tab"
              aria-current={isActive ? 'page' : undefined}
              title={collapsed ? item.label : undefined}
              onClick={() => navigate(item.path)}
              style={{
                display: 'flex', alignItems: 'center', gap: collapsed ? 0 : 12,
                height: 40, padding: collapsed ? '0 0 0 22px' : '0 12px',
                borderRadius: 10, background: isActive ? 'var(--thot-gold-soft)' : 'transparent',
                color: 'var(--thot-text)', border: 'none', cursor: 'pointer',
                fontSize: 'var(--thot-text-sidebar-item)',
                fontFamily: 'var(--thot-font-family)',
                fontWeight: isActive ? 600 : 500,
                transition: 'var(--thot-transition-fast)',
                width: collapsed ? 56 : '100%',
                justifyContent: collapsed ? 'center' : 'flex-start',
              }}
              onMouseEnter={(e) => { if (!isActive) e.currentTarget.style.background = 'var(--thot-surface-2)'; }}
              onMouseLeave={(e) => { if (!isActive) e.currentTarget.style.background = 'transparent'; }}
            >
              <span style={{
                color: isActive ? 'var(--thot-gold)' : 'var(--thot-text-muted)',
                display: 'flex',
              }}>
                {item.icon}
              </span>
              {!collapsed && <span>{item.label}</span>}
            </button>
          );
        })}
      </nav>

      {/* Hint — chat avec VLM direct */}
      {!collapsed && (
        <div style={{ marginTop: 12, padding: '12px 8px' }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--thot-text-muted)', marginBottom: 6 }}>
            Chat + VLM
          </div>
          <p style={{ fontSize: 12, color: 'var(--thot-text-faint)', lineHeight: 1.5 }}>
            Vision (LFM2.5-VL-3B) + OCR (GLM-OCR) en toolcalls directs.
          </p>
        </div>
      )}

      <div style={{ flex: 1 }} />

      {/* Profile — reste minimal */}
      <button
        onClick={() => navigate('/chat')}
        style={{
          display: collapsed ? 'none' : 'flex', alignItems: 'center', gap: 10, padding: 10,
          borderRadius: 12, background: 'var(--thot-surface-2)',
          border: '1px solid transparent', cursor: 'pointer', width: '100%',
          transition: 'var(--thot-transition-fast)',
        }}
        onMouseEnter={(e) => e.currentTarget.style.borderColor = 'var(--thot-border)'}
        onMouseLeave={(e) => e.currentTarget.style.borderColor = 'transparent'}
      >
        <div style={{
          width: 36, height: 36, borderRadius: 'var(--thot-radius-pill)',
          background: 'var(--thot-gold-soft)', color: 'var(--thot-gold)',
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          fontWeight: 600, fontSize: 14, flexShrink: 0,
        }}>
          TH
        </div>
        <div style={{ flex: 1, textAlign: 'left' }}>
          <div style={{ fontSize: 14, fontWeight: 500 }}>Thot local</div>
          <div style={{ fontSize: 12, color: 'var(--thot-text-faint)' }}>LFM2.5-8B • 128K</div>
        </div>
        <ChevronRight24Regular style={{ fontSize: 16, color: 'var(--thot-text-faint)' }} />
      </button>
    </aside>
  );
}
