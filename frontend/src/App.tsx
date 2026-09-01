// Thot Phase 1 — App chat seul + VLM (pas de fonction agent séparée)
// Chat peut appeler vision/doc_ocr en toolcalls directs, pas besoin de /agent
import { useEffect } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { AppShell } from './components/shell/AppShell';
import { ToastContainer } from './components/ui/Toast';
import { ChatHome } from './pages/chat/ChatHome';
import { ChatConversation } from './pages/chat/ChatConversation';
import { HistoryPage } from './pages/history/HistoryPage';
import { AttachmentDraftProvider } from './features/attachments/AttachmentDraftContext';

function GlobalDragGuard() {
  useEffect(() => {
    const prevent = (e: DragEvent) => { if (e.dataTransfer?.types.includes("Files")) e.preventDefault(); };
    window.addEventListener("dragover", prevent);
    window.addEventListener("drop", prevent);
    return () => { window.removeEventListener("dragover", prevent); window.removeEventListener("drop", prevent); };
  }, []);
  return null;
}

export default function App() {
  return (
    <BrowserRouter>
      <AttachmentDraftProvider>
        <GlobalDragGuard />
        <AppShell>
          <Routes>
            <Route path="/" element={<Navigate to="/chat" replace />} />
            <Route path="/chat" element={<ChatHome />} />
            <Route path="/chat/:id" element={<ChatConversation mode="chat" />} />
            <Route path="/history" element={<HistoryPage />} />
          </Routes>
        </AppShell>
        <ToastContainer />
      </AttachmentDraftProvider>
    </BrowserRouter>
  );
}
