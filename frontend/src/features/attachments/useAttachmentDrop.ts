import { useCallback, useRef } from "react";
import { useAttachmentDraft } from "./AttachmentDraftContext";
import { validateAttachments } from "./attachmentValidation";
import { showToast } from "../../components/ui/Toast";

export function useAttachmentDrop() {
  const { addFiles, setDraggingFiles } = useAttachmentDraft();
  const dragCounterRef = useRef(0);

  const processFiles = useCallback((files: File[]) => {
    const { files: accepted, errors } = validateAttachments(files);
    if (errors.length > 0) showToast({ type: "error", title: "Fichiers ignores", message: errors.join(", ") });
    if (accepted.length > 0) addFiles(accepted);
  }, [addFiles]);

  const handleDragEnter = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation();
    dragCounterRef.current += 1;
    if (e.dataTransfer.types.includes("Files")) setDraggingFiles(true);
  }, [setDraggingFiles]);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation();
    if (e.dataTransfer.types.includes("Files")) {
      e.dataTransfer.dropEffect = "copy";
      setDraggingFiles(true);
    }
  }, [setDraggingFiles]);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation();
    dragCounterRef.current -= 1;
    if (dragCounterRef.current <= 0) { dragCounterRef.current = 0; setDraggingFiles(false); }
  }, [setDraggingFiles]);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation();
    dragCounterRef.current = 0; setDraggingFiles(false);
    const files = Array.from(e.dataTransfer.files);
    if (files.length > 0) processFiles(files);
  }, [processFiles, setDraggingFiles]);

  return { processFiles, handleDragEnter, handleDragOver, handleDragLeave, handleDrop };
}
