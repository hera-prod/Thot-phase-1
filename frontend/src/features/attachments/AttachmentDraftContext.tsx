import { createContext, useContext, useCallback, useState, type ReactNode } from "react";
import type { PendingAttachment, AttachmentDraftState } from "./attachmentTypes";
import { MAX_ATTACHMENTS } from "./attachmentValidation";
import { showToast } from "../../components/ui/Toast";

const AttachmentDraftContext = createContext<AttachmentDraftState | null>(null);

export function AttachmentDraftProvider({ children }: { children: ReactNode }) {
  const [attachments, setAttachments] = useState<PendingAttachment[]>([]);
  const [isDraggingFiles, setDraggingFiles] = useState(false);

  const addFiles = useCallback((files: File[]) => {
    setAttachments((prev) => {
      const remaining = MAX_ATTACHMENTS - prev.length;
      if (remaining <= 0) {
        showToast({ type: "error", title: "Limite atteinte", message: `Max ${MAX_ATTACHMENTS} fichiers` });
        return prev;
      }
      const slice = files.slice(0, remaining);
      if (files.length > remaining) {
        showToast({ type: "error", title: "Trop de fichiers", message: `Seuls ${remaining} ajoutes sur ${files.length}` });
      }
      const mapped: PendingAttachment[] = slice.map((file) => {
        const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
        return {
          id: crypto.randomUUID(),
          file,
          name: file.name,
          mimeType: file.type,
          size: file.size,
          previewUrl: isPdf ? undefined : URL.createObjectURL(file),
          kind: isPdf ? "pdf" : "image",
        };
      });
      return [...prev, ...mapped];
    });
  }, []);

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
