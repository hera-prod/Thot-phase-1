// Thot Phase 1 — un seul mode de discussion. Le chat appelle vision/doc_ocr en
// toolcalls directs : il n'y a pas de second mode à basculer, donc pas de route
// ni de paramètre qui l'exprimerait.
import { Component, type ReactNode } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AppShell } from './components/shell/AppShell';
import { ToastContainer } from './components/ui/Toast';
import { ChatHome } from './pages/chat/ChatHome';
import { ChatConversation } from './pages/chat/ChatConversation';
import { HistoryPage } from './pages/history/HistoryPage';
import { ImagesPage } from './pages/images/ImagesPage';
import { GalleryPage } from './pages/gallery/GalleryPage';
import { SettingsPage } from './pages/settings/SettingsPage';
import { EspacesPage } from './pages/espaces/EspacesPage';
import { EspacePage } from './pages/espaces/EspacePage';
import { AttachmentDraftProvider } from './features/attachments/AttachmentDraftContext';
import { InferenceActivityProvider } from './features/inference/InferenceActivityContext';

// Filet de sécurité applicatif : une erreur de rendu ne doit pas laisser un
// écran blanc. Pas de dépendance : une classe React suffit.
class AppErrorBoundary extends Component<{ children: ReactNode }, { hasError: boolean; message: string }> {
  state = { hasError: false, message: '' };

  static getDerivedStateFromError(error: unknown) {
    return {
      hasError: true,
      message: error instanceof Error ? error.message : String(error),
    };
  }

  render() {
    if (!this.state.hasError) return this.props.children;
    return (
      <div
        role="alert"
        style={{
          minHeight: '100vh', display: 'flex', flexDirection: 'column',
          alignItems: 'center', justifyContent: 'center', gap: 12,
          padding: 32, textAlign: 'center', background: 'var(--thot-surface-1)',
          color: 'var(--thot-text)', fontFamily: 'var(--thot-font-family)',
        }}
      >
        <h1 style={{ fontSize: 22, fontWeight: 700 }}>L’application a rencontré un problème.</h1>
        <p style={{ fontSize: 14, color: 'var(--thot-text-muted)', maxWidth: 520, lineHeight: 1.6 }}>
          Une erreur inattendue a interrompu l’affichage. Vos données locales sont intactes.
        </p>
        {this.state.message ? (
          <p style={{ fontSize: 12, color: 'var(--thot-text-faint)', maxWidth: 520, wordBreak: 'break-word' }}>
            {this.state.message}
          </p>
        ) : null}
        <button
          type="button"
          onClick={() => window.location.reload()}
          style={{
            marginTop: 8, padding: '10px 20px', borderRadius: 'var(--thot-radius-sm)',
            border: 'none', background: 'var(--thot-gold)', color: '#fff',
            cursor: 'pointer', fontSize: 14, fontWeight: 600,
            fontFamily: 'var(--thot-font-family)',
          }}
        >
          Recharger l’application
        </button>
      </div>
    );
  }
}

function GlobalDragGuard() {
  return null;
}

export default function App() {
  return (
    <BrowserRouter>
      <AttachmentDraftProvider>
        <InferenceActivityProvider>
          <GlobalDragGuard />
          <AppErrorBoundary>
            <AppShell>
              <Routes>
                <Route path="/" element={<Navigate to="/chat" replace />} />
                <Route path="/chat" element={<ChatHome />} />
                <Route path="/chat/:id" element={<ChatConversation />} />
                {/* Espaces : l'accueil, puis un espace et son onglet */}
                <Route path="/espaces" element={<EspacesPage />} />
                <Route path="/espaces/:id" element={<EspacePage />} />
                <Route path="/espaces/:id/:onglet" element={<EspacePage />} />
                <Route path="/images" element={<ImagesPage />} />
                <Route path="/gallery" element={<GalleryPage />} />
                <Route path="/history" element={<HistoryPage />} />
                <Route path="/settings" element={<SettingsPage />} />
                <Route path="*" element={<Navigate to="/chat" replace />} />
              </Routes>
            </AppShell>
          </AppErrorBoundary>
          <ToastContainer />
        </InferenceActivityProvider>
      </AttachmentDraftProvider>
    </BrowserRouter>
  );
}
