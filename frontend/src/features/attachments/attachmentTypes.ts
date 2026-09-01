export type PendingAttachment = {
  id: string;
  file: File;
  name: string;
  mimeType: string;
  size: number;
  previewUrl?: string;
  kind: "image" | "pdf";
};

export type AttachmentDraftState = {
  attachments: PendingAttachment[];
  isDraggingFiles: boolean;
  addFiles: (files: File[]) => void;
  removeAttachment: (id: string) => void;
  clearAttachments: () => void;
  setDraggingFiles: (value: boolean) => void;
};
