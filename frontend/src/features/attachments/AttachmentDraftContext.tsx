import { createContext, useContext, useCallback, useState, useEffect, type ReactNode } from "react";
import type { PendingAttachment, AttachmentDraftState } from "./attachmentTypes";
import { MAX_ATTACHMENTS } from "./attachmentValidation";
import { showToast } from "../../components/ui/Toast";

const AttachmentDraftContext = createContext<AttachmentDraftState | null>(null);

export function AttachmentDraftProvider({ children }: { children: ReactNode }) {
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [isDraggingFiles, setDraggingFiles] = useState(false);

  const addFiles = useCallback((files: File[]) => {
    setAttachments((prev) => {
      // Dedup §6 + annexe 09 : même nom déjà présent → ignore (évite triple dépôt HTML5+natif+page)
      // Fenêtre courte : seul le nom suffit (staged placeholder a size 0 vs réel)
      const deduped = files.filter(f => {
        const staged = (f as unknown as { _stagedPath?: string })._stagedPath;
        if (staged) {
          return !prev.some(p => p.name === f.name);
        }
        return !prev.some(p => p.name === f.name);
      });
      if (deduped.length === 0 && files.length > 0) {
        // Tous doublons → silencieux (évite 3x)
        return prev;
      }
      const remaining = MAX_ATTACHMENTS - prev.length;
      if (remaining <= 0) {
        showToast({ type: "error", title: "Limite atteinte", message: `Max ${MAX_ATTACHMENTS} fichiers` });
        return prev;
      }
      const slice = deduped.slice(0, remaining);
      if (deduped.length > remaining) {
        showToast({ type: "error", title: "Trop de fichiers", message: `Seuls ${remaining} ajoutés sur ${deduped.length}` });
      }
      const mapped: PendingAttachment[] = slice.map((file) => {
        const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
        const stagedPath = (file as unknown as { _stagedPath?: string })._stagedPath;
        const stageItemId = (file as unknown as { _stageItemId?: string })._stageItemId;
        const WARN_IMAGE_BYTES = 5*1024*1024;
        const needsResize = file.size > WARN_IMAGE_BYTES && !isPdf;
        // Preview : File réel → objectURL, stagé (size 0) → convertFileSrc asset://
        let previewUrl: string | undefined;
        if (!isPdf) {
          try {
            if (file.size > 0) previewUrl = URL.createObjectURL(file);
            else if (stagedPath) {
              // Preview pour stagé géré côté Composer via convertFileSrc (évite require sync)
              previewUrl = undefined;
            }
          } catch { previewUrl = undefined; }
        }
        return {
          id: crypto.randomUUID(),
          file,
          name: file.name,
          mimeType: file.type || (isPdf ? "application/pdf" : "image/png"),
          size: file.size,
          previewUrl,
          kind: isPdf ? "pdf" : "image",
          stagedPath,
          stageItemId,
          needsResize,
        };
      });
      return [...prev, ...mapped];
    });
  }, []);

  // Annexe 09 §5-6 et §7 : même pipeline que le bouton parcourir (T-DnD-09)
  // Les fichiers stagés par Tauri (stage_dropped_files) arrivent via custom event thot:add-files
  useEffect(() => {
    const handler = (e: Event) => {
      const files = (e as CustomEvent<File[]>).detail ?? [];
      if (files.length) addFiles(files);
    };
    window.addEventListener('thot:add-files', handler as EventListener);
    return () => window.removeEventListener('thot:add-files', handler as EventListener);
  }, [addFiles]);

  // Nettoyage preview URLs à la fermeture (évite fuite §6)
  useEffect(() => {
    return () => {
      attachments.forEach((a) => { if (a.previewUrl) URL.revokeObjectURL(a.previewUrl); });
    };
  }, [attachments]);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => {
      const found = prev.find((a) => a.id === id);
      if (found?.previewUrl) URL.revokeObjectURL(found.previewUrl);
      return prev.filter((a) => a.id !== id);
    });
  }, []);

  const clearAttachments = useCallback(() => {
    setAttachments((prev) => {
      prev.forEach((a) => { if (a.previewUrl) URL.revokeObjectURL(a.previewUrl); });
      return [];
    });
  }, []);

  return (
    <AttachmentDraftContext.Provider value={{ attachments, isDraggingFiles, addFiles, removeAttachment, clearAttachments, setDraggingFiles }}>
      {children}
    </AttachmentDraftContext.Provider>
  );
}

export function useAttachmentDraft(): AttachmentDraftState {
  const ctx = useContext(AttachmentDraftContext);
  if (!ctx) throw new Error("useAttachmentDraft must be used within AttachmentDraftProvider");
  return ctx;
}
