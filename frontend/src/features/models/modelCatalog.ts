// Source unique côté interface pour les modèles et leurs licences.
//
// Volontairement statique : pas d'IPC, pas de chargement, pas de vérification de
// présence des poids sur le disque (un composant frontend ne peut pas lire le
// disque). Si un modèle change un jour, on met à jour cette constante à la main.
// La présence réelle des poids n'est pas vérifiée ici.

export type ModelLicenseKind = 'permissive' | 'community' | 'non_commercial';
export type ModelCommercialUse = 'allowed' | 'conditional' | 'forbidden';

export interface ModelCatalogEntry {
  id: string;
  role: 'generation' | 'vision' | 'ocr' | 'embedding' | 'image_diffusion' | 'image_text_encoder' | 'image_vae';
  roleLabel: string;
  name: string;
  vendor: string;
  license: string;
  licenseKind: ModelLicenseKind;
  commercialUse: ModelCommercialUse;
  quantization: string | null;
  context: number | null;
}

export const MODEL_CATALOG: readonly ModelCatalogEntry[] = [
  {
    id: 'lfm2.5-8b',
    role: 'generation',
    roleLabel: 'Génération et orchestrateur',
    name: 'LFM2.5-8B A1B',
    vendor: 'Liquid AI',
    license: 'LFM 1.0 (Liquid AI)',
    licenseKind: 'community',
    commercialUse: 'conditional',
    quantization: 'Q4_K_M',
    context: 131072,
  },
  {
    id: 'lfm2.5-vl-3b',
    role: 'vision',
    roleLabel: 'Vision générale',
    name: 'LFM2.5-VL-3B',
    vendor: 'Liquid AI',
    license: 'LFM 1.0 (Liquid AI)',
    licenseKind: 'community',
    commercialUse: 'conditional',
    quantization: 'Q4_K_XL',
    context: 32768,
  },
  {
    id: 'paddleocr-vl-1.6',
    role: 'ocr',
    roleLabel: 'OCR des documents',
    name: 'PaddleOCR-VL-1.6 + PP-DocLayoutV3',
    vendor: 'PaddlePaddle',
    license: 'Apache-2.0',
    licenseKind: 'permissive',
    commercialUse: 'allowed',
    quantization: null,
    context: 8192,
  },
  {
    id: 'lfm2.5-colbert-350m',
    role: 'embedding',
    roleLabel: 'Recherche dans l’atelier',
    name: 'LFM2.5-ColBERT-350M',
    vendor: 'Liquid AI',
    license: 'LFM 1.0 (Liquid AI)',
    licenseKind: 'community',
    commercialUse: 'conditional',
    quantization: 'bf16',
    context: null,
  },
  {
    id: 'flux2-klein-9b',
    role: 'image_diffusion',
    roleLabel: 'Diffusion d’images',
    name: 'FLUX.2 Klein 9B KV',
    vendor: 'Black Forest Labs',
    license: 'FLUX.2 [klein] Non-Commercial License',
    licenseKind: 'non_commercial',
    commercialUse: 'forbidden',
    quantization: 'Q4_K_M',
    context: null,
  },
  {
    id: 'qwen3-8b',
    role: 'image_text_encoder',
    roleLabel: 'Encodage texte des images',
    name: 'Qwen3-8B',
    vendor: 'Alibaba Qwen',
    license: 'Apache-2.0',
    licenseKind: 'permissive',
    commercialUse: 'allowed',
    quantization: 'Q4_K_M',
    context: null,
  },
  {
    id: 'flux2-vae',
    role: 'image_vae',
    roleLabel: 'Décodage des images',
    name: 'FLUX.2 VAE',
    vendor: 'Black Forest Labs',
    license: 'Apache-2.0',
    licenseKind: 'permissive',
    commercialUse: 'allowed',
    quantization: null,
    context: null,
  },
];

export function modelByRole(role: ModelCatalogEntry['role']): ModelCatalogEntry | undefined {
  return MODEL_CATALOG.find((m) => m.role === role);
}