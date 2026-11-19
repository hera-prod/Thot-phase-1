export const MAX_ATTACHMENTS = 4;
// Limites basées sur capacités modèle vision (docs/depot/annexes/02_* + web : NaFlex 512)
// LFM2.5-VL-3B : SigLIP2 NaFlex 512×512 patches + thumbnail, contexte 32K, pas de limite Mo fixe.
// On supprime le rejet 5 Mo et on prétraite : JPEG 88 + thumbnail 1536 côté Python
// → on accepte jusqu'à 20 Mo (même que PDF) et on resize tracé (backend_outils.md:151)
export const MAX_IMAGE_BYTES = 20 * 1024 * 1024;
export const MAX_PDF_BYTES = 20 * 1024 * 1024;
export const WARN_IMAGE_BYTES = 5 * 1024 * 1024; // au-delà → resize avec info

export const IMAGE_MIME_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
export const PDF_MIME_TYPE = "application/pdf";

export type ValidationResult = {
  files: File[];
  errors: string[];
  warnings: string[];
};

export function validateAttachments(files: File[]): ValidationResult {
  const errors: string[] = [];
  const warnings: string[] = [];
  const accepted: File[] = [];
  for (const f of files) {
    // Fallback extension si type vide (drag OS) — doit matcher le magic côté electron/sidecar.cjs §5
    const nameLower = f.name.toLowerCase();
    const isPdf = f.type === PDF_MIME_TYPE || nameLower.endsWith(".pdf");
    const isImage = f.type.startsWith("image/") || nameLower.endsWith(".png") || nameLower.endsWith(".jpg") || nameLower.endsWith(".jpeg") || nameLower.endsWith(".webp");
    if (!isImage && !isPdf) {
      errors.push(`${f.name}: type non supporté (PNG/JPG/WEBP/PDF)`);
      continue;
    }
    if (f.size > MAX_PDF_BYTES) {
      errors.push(`${f.name}: >20 Mo (limite max)`);
      continue;
    }
    if (f.size === 0 && (f as unknown as { _stagedPath?: string })._stagedPath) {
      // Fichier stagé natif avec placeholder vide — valide (bytes réels côté electron/sidecar.cjs)
    } else if (f.size === 0) {
      errors.push(`${f.name}: fichier vide`);
      continue;
    }
    // Image grande → acceptée mais warn resize tracé (annexe 09 §6, backend_outils 151)
    if (isImage && f.size > WARN_IMAGE_BYTES) {
      warnings.push(`${f.name}: ${ (f.size/1024/1024).toFixed(1)} Mo → sera réduit à 1536px (NaFlex 512)`);
    }
    accepted.push(f);
  }
  return { files: accepted, errors, warnings };
}
