export type PendingAttachment = {
  id: string;
  file: File;
  name: string;
  mimeType: string;
  size: number;
  previewUrl?: string;
  kind: "image" | "pdf";
  stagedPath?: string; // Chemin Rust stage_dropped_files (annexe 09), évite re-upload vide
  stageItemId?: string; // Identifiant stable annexe 10, transité jusqu'à artifactId
  needsResize?: boolean; // true si >5Mo et sera réduit côté backend (NaFlex 512)
};

export type AttachmentDraftState = {
  attachments: PendingAttachment[];
  isDraggingFiles: boolean;
  addFiles: (files: File[]) => void;
  removeAttachment: (id: string) => void;
  clearAttachments: () => void;
  setDraggingFiles: (value: boolean) => void;
};
