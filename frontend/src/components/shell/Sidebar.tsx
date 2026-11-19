import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  Chat24Regular,
  DocumentFolder24Regular,
  Image24Regular,
  Grid24Regular,
  History24Regular,
  Settings24Regular,
  PanelLeftContract24Regular,
  PanelLeftExpand24Regular,
} from '@fluentui/react-icons';
import { useInferenceActivity } from '../../features/inference/InferenceActivityContext';

// L'ordre suit la maquette 05-1 : Chat, Espaces, puis le reste. Un espace n'est
// ni un chat ni un dossier — c'est le CONTENEUR des deux, donc il vient avant
// les vues qui le remplissent, pas après.
const NAV_ITEMS = [
  { path: '/chat', icon: <Chat24Regular />, label: 'Chat' },
  { path: '/espaces', icon: <DocumentFolder24Regular />, label: 'Espaces' },
  { path: '/images', icon: <Image24Regular />, label: 'Images' },
  { path: '/gallery', icon: <Grid24Regular />, label: 'Galerie' },
  { path: '/history', icon: <History24Regular />, label: 'Historique' },
] as const;

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(false);
  const [brandImageAvailable, setBrandImageAvailable] = useState(true);
  const location = useLocation();
  const navigate = useNavigate();
  const { activity, memory } = useInferenceActivity();
  const activePath = `/${location.pathname.split('/').filter(Boolean)[0] ?? 'chat'}`;
  const settingsActive = activePath === '/settings';
  const inferenceLabel = activity.phase === 'cancelling'
    ? 'Arrêt en cours…'
    : activity.kind === 'image'
      ? 'Génération d’image en cours'
      : 'Discussion en cours';
  const inferenceRoute = activity.kind === 'image'
    ? '/images'
    : activity.conversationId
      ? `/chat/${activity.conversationId}`
      : '/chat';
  const memoryLabel = memory.status === 'running'
    ? 'Entretien mémoire en cours'
    : memory.status === 'deferred'
      ? 'Entretien mémoire différé'
      // `waiting` est réservé au statut durable "en_attente" de l'onglet Mémoire ;
      // le scheduler n'émet aujourd'hui que "differe".
      : 'Entretien mémoire en attente';
  const memoryRoute = memory.workspaceId
    ? `/espaces/${encodeURIComponent(memory.workspaceId)}/memoire`
    : '/espaces';

  return (
    <aside className={`sidebar${collapsed ? ' sidebarCollapsed' : ''}`} aria-label="Navigation principale">
      <div className="sidebarHeader">
        <div className="sidebarBrand" aria-label="Thot, Atelier local">
          <span className="sidebarBrandMark">
            {brandImageAvailable ? (
              <img
                src="/assets/brand/Thot_logo.svg"
                alt=""
                aria-hidden="true"
                onError={() => setBrandImageAvailable(false)}
              />
            ) : (
              <span aria-hidden="true">T</span>
            )}
          </span>
          {!collapsed && (
            <span className="sidebarBrandCopy">
              <span className="sidebarBrandName">Thot</span>
              <span className="sidebarBrandBaseline">Atelier local</span>
            </span>
          )}
        </div>
        <button
          type="button"
          className="sidebarToggle"
          onClick={() => setCollapsed((value) => !value)}
          aria-label={collapsed ? 'Déplier la navigation' : 'Replier la navigation'}
          aria-expanded={!collapsed}
          aria-controls="sidebar-navigation"
        >
          {collapsed ? <PanelLeftExpand24Regular /> : <PanelLeftContract24Regular />}
        </button>
      </div>

      <div className="sidebarRule" aria-hidden="true" />

      {!collapsed && <div className="sidebarSectionLabel">Navigation</div>}

      <nav id="sidebar-navigation" className="sidebarNav" aria-label="Sections de l'atelier">
        {NAV_ITEMS.map((item) => {
          const isActive = activePath === item.path;
          return (
            <button
              key={item.path}
              type="button"
              className={`sidebarNavItem${isActive ? ' sidebarNavItemActive' : ''}`}
              aria-current={isActive ? 'page' : undefined}
              aria-label={item.label}
              title={item.label}
              onClick={() => navigate(item.path)}
            >
              <span className="sidebarNavIcon" aria-hidden="true">
                {item.icon}
              </span>
              {!collapsed && <span className="sidebarNavLabel">{item.label}</span>}
              {isActive && <span className="sidebarNavMarker" aria-hidden="true" />}
            </button>
          );
        })}
      </nav>

      {activity.active && (
        <button
          type="button"
          className="sidebarInference"
          onClick={() => navigate(inferenceRoute)}
          title={inferenceLabel}
          aria-label={`${inferenceLabel} — ouvrir`}
          aria-live="polite"
        >
          <span className="sidebarInferenceDot" aria-hidden="true" />
          {!collapsed && <span className="sidebarInferenceLabel">{inferenceLabel}</span>}
        </button>
      )}

      {memory.status !== 'idle' && (
        <button
          type="button"
          className="sidebarInference"
          data-state={memory.status}
          onClick={() => navigate(memoryRoute)}
          title={memoryLabel}
          aria-label={`${memoryLabel} — ouvrir`}
          aria-live="polite"
        >
          <span className="sidebarInferenceDot" aria-hidden="true" />
          {!collapsed && <span className="sidebarInferenceLabel">{memoryLabel}</span>}
        </button>
      )}

      <div className="sidebarSpacer" />

      {/* Les réglages d'un atelier appartiennent à l'atelier : ils sontaccessible
          depuis sa fiche d'état, pas depuis la navigation des destinations. */}
      {collapsed ? (
        <button
          type="button"
          className="sidebarCompactStatus"
          onClick={() => navigate('/settings')}
          aria-label="Réglages de l'atelier local"
          aria-current={settingsActive ? 'page' : undefined}
          title="Réglages de l'atelier local"
        >
          <span className="sidebarStatusDot" aria-hidden="true" />
        </button>
      ) : (
        <button
          type="button"
          className="sidebarFooter"
          onClick={() => navigate('/settings')}
          aria-current={settingsActive ? 'page' : undefined}
        >
          <span className="sidebarFooterKicker">
            État de l'atelier
            <span className="sidebarFooterGear" aria-hidden="true">
              <Settings24Regular />
            </span>
          </span>
          <span className="sidebarFooterTitle">
            <span className="sidebarStatusDot" aria-hidden="true" />
            Atelier local
          </span>
          <span className="sidebarFooterText">Exécution locale · espace de travail</span>
        </button>
      )}
    </aside>
  );
}
