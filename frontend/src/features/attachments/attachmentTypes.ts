export type StagedFileMeta = { _stagedPath?: string; _stageItemId?: string; _previewToken?: string };

export function stageMetaOf(file: File): StagedFileMeta {
  return file as unknown as StagedFileMeta;
}

// Clé dedup Phase 3.3 : nom + taille + stageItemId (le nom seul collisionne).
export function dedupeKey(name: string, size: number, stageItemId?: string): string {
  return `${name}::${size}::${stageItemId ?? ""}`;
}

export type PendingAttachment = {
  id: string;
  file: File;
  name: string;
  mimeType: string;
  size: number;
  previewUrl?: string;
  kind: "image" | "pdf";
  stagedPath?: string; // Chemin Electron stage_dropped_paths (annexe 10), évite re-upload vide
  stageItemId?: string; // Identifiant stable annexe 10, transité jusqu'à artifactId
  previewToken?: string; // Token get_staged_preview_bytes, preview unique via convertFileSrc
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
