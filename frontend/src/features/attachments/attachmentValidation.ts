export const MAX_ATTACHMENTS = 4;
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
export const MAX_PDF_BYTES = 20 * 1024 * 1024;

export const IMAGE_MIME_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
export const PDF_MIME_TYPE = "application/pdf";

export type ValidationResult = {
  files: File[];
  errors: string[];
};

export function validateAttachments(files: File[]): ValidationResult {
  const errors: string[] = [];
  const accepted: File[] = [];
  for (const f of files) {
    const isImage = f.type.startsWith("image/");
    const isPdf = f.type === PDF_MIME_TYPE || f.name.toLowerCase().endsWith(".pdf");
    if (!isImage && !isPdf) {
      errors.push(`${f.name}: type non supporte`);
      continue;
    }
    if (f.size > MAX_PDF_BYTES) {
      errors.push(`${f.name}: >20 Mo`);
      continue;
    }
    if (isImage && f.size > MAX_IMAGE_BYTES) {
      errors.push(`${f.name}: image >5 Mo`);
      continue;
    }
    accepted.push(f);
  }
  return { files: accepted, errors };
}
