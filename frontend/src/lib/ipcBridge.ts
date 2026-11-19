// Pont renderer → sidecar Python, via les verbes IPC d'Electron.
//
// Chaque fonction ci-dessous est un verbe du registre `electron/commands.cjs`,
// invoqué par `ipc.ts`. Le renderer ne connaît ni Electron ni le protocole du
// sidecar : il ne connaît que des verbes nommés.
//
// Ce que le pont cache : le déballage des réponses (fait par le main process via
// RESULT_KEY, pas ici) et la forme des rejets, convertis en `IpcError` pour
// qu'aucun écran n'ait à traiter une chaîne brute. Ce qu'il ne cache pas : les
// RETOURS et les PAYLOADS D'ÉVÉNEMENTS restent en snake_case, c'est la forme
// du service — d'où les `snake_case` dans les interfaces ci-dessous.

import { invoke, listen } from "./ipc";
import type { MessageStarted, Thinking, ToolCall, ToolResult, Answer, MessageCompleted, ErrorEvent, BackendReady, OrchestratorBrief } from "../types/ipc";

export type ExecutionPlan = { request_id: string; attachment_kind?: string | null; intent: string; selected_tool?: string | null; selected_model?: string | null; reason: string };

// === INVOKE : Frontend → sidecar Python (stdin JSON Lines) ===

export async function chatSend(params: {
  request_id: string;      // UUID généré côté frontend pour corréler les events
  message: string;
  image_paths?: string[];   // chemins /tmp/thot/{id}/{hash}.png déjà validés par save_upload (compat déprécié annexe 10)
  attachment_ids?: string[]; // IDs durables après attachments.prepare (cible annexe 10)
  conv_id?: string;         // id de conversation pour persistance DB
  workspace_id?: string;    // espace auquel la conversation est rattachée
}) {
  // Les arguments ENTRANTS sont en camelCase, convention du transport, alors que
  // les retours restent en snake_case (cf. les deux invariants de `ipc.ts`).
  return invoke("chat_send", {
    requestId: params.request_id,
    message: params.message,
    imagePaths: params.image_paths,
    attachmentIds: params.attachment_ids,
    convId: params.conv_id,
    workspaceId: params.workspace_id,
  } as unknown as Record<string, unknown>);
}

// 20 Mio : règle d'entrée utilisateur (backend INPUT_LIMITS.upload_bytes).
// Vérifiée AVANT `File.arrayBuffer()` par les pages qui lisent un fichier.
export const MAX_PICKER_ATTACHMENT_BYTES = 20 * 1024 * 1024;

/** Base64 d'un `Uint8Array` sans dépasser la pile de `String.fromCharCode`. */
export function bytesToBase64(bytes: Uint8Array): string {
  const bloc = 0x8000;
  let binaire = "";
  for (let i = 0; i < bytes.length; i += bloc) {
    binaire += String.fromCharCode(...bytes.subarray(i, i + bloc));
  }
  return btoa(binaire);
}

/** Inverse de `bytesToBase64`, pour un aperçu reçu du service. */
export function base64ToBytes(value: string): Uint8Array {
  const binaire = atob(value);
  const octets = new Uint8Array(binaire.length);
  for (let i = 0; i < binaire.length; i += 1) {
    octets[i] = binaire.charCodeAt(i);
  }
  return octets;
}

export async function saveUpload(request_id: string, filename: string, data: Uint8Array): Promise<[string, string]> {
  // Envoie le binaire au service, qui valide magic bytes + taille puis écrit dans data/temp/{id}/
  // Retourne [path, hash] ex: ["/tmp/thot/uuid/abc.png", "sha256:abc..."] — compat Lot 2
  return invoke<[string, string]>("save_upload", {
    requestId: request_id,
    filename,
    // Base64 : le JSON ne porte pas de binaire, et un tableau de nombres
    // dépassait la borne de ligne pour un fichier de quelques dizaines de Ko.
    data: bytesToBase64(data),
  } as unknown as Record<string, unknown>);
}

// Annexe 10 : pipeline unifié — même forme pour drag, trombone, collage, capture
export type StagingItem = {
  stageItemId: string;
  filename: string;
  displayName: string;
  mimeDetected: string;
  sizeBytes: number;
  sha256: string;
  preview?: { kind: string; token: string; width?: number; height?: number };
  status: string;
};
export type StagingResponse = {
  stageId: string;
  items: StagingItem[];
  rejected: Array<{ path?: string; code: string; message: string }>;
};

export type IpcError = { code: string; message: string; retryable: boolean };

export function toIpcError(e: unknown, fallbackCode = "ELECTRON_ERROR"): IpcError {
  const message = e instanceof Error ? e.message : String(e ?? "");
  const retryable = /timeout|temporaire|réseau|network|busy|indisponible/i.test(message);
  return { code: fallbackCode, message: message || fallbackCode, retryable };
}

export async function cancelChat(requestId: string): Promise<void> {
  try {
    await invoke("cancel_run", { requestId } as unknown as Record<string, unknown>);
  } catch (e) {
    throw toIpcError(e, "CANCEL_FAILED");
  }
}
export async function stageDroppedPaths(requestId: string, paths: string[]): Promise<StagingResponse> {
  return invoke<StagingResponse>("stage_dropped_paths", { requestId, paths } as unknown as Record<string, unknown>);
}

export async function stagePickerBytes(requestId: string, filename: string, bytes: Uint8Array): Promise<StagingResponse> {
  if (bytes.length > MAX_PICKER_ATTACHMENT_BYTES) {
    throw new Error(`Fichier trop volumineux : ${filename} dépasse 20 Mo.`);
  }
  return invoke<StagingResponse>("stage_picker_bytes", { requestId, filename, bytes: bytesToBase64(bytes) } as unknown as Record<string, unknown>);
}

// Promotion annexe 10 : attachments.prepare → artifact_ids (Python coordinateur)
// Lève IpcError (ATTACH_PREPARE_FAILED) : l'appelant bascule sur image_paths + toast.
export async function attachmentsPrepare(conversationId: string, stageItemIds: string[], requestId: string): Promise<{ items: Array<{ stageItemId: string; artifactId: string; status: string }> }> {
  try {
    return await invoke("attachments_prepare", { conversationId, stageItemIds, requestId } as unknown as Record<string, unknown>);
  } catch (e) {
    throw toIpcError(e, "ATTACH_PREPARE_FAILED");
  }
}

export interface ConversationSummary {
  id: string;
  title: string;
  mode: string;
  /** Espace de rattachement ; absent si la conversation est hors espace.
   *  Rendu par le service pour que l'historique puisse regrouper. */
  workspace_id?: string | null;
  workspace_name?: string | null;
  created_at?: string;
  updated_at?: string;
  last_message_preview?: string;
  message_count?: number;
}

export async function listConversations(limit = 50, offset = 0): Promise<ConversationSummary[]> {
  return invoke<ConversationSummary[]>("list_conversations", { limit, offset });
}

export function renameConversation(conversationId: string, title: string): Promise<void> {
  const trimmedTitle = title.trim();
  if (!trimmedTitle) return Promise.reject(new Error('Le titre ne peut pas être vide.'));
  return invoke("rename_conversation", { conversationId, title: trimmedTitle });
}

export function deleteConversation(conversationId: string): Promise<void> {
  return invoke("delete_conversation", { conversationId });
}

export async function getConversation(conversationId: string): Promise<ConversationSummary | null> {
  try {
    return await invoke<ConversationSummary | null>("get_conversation", { conversationId } as unknown as Record<string, unknown>);
  } catch {
    return null;
  }
}

export type HistoryAttachment = { id: string; kind: string; name: string; mime: string };

export type HistoryToolCall = { tool_call_id: string; tool: string; arguments: Record<string, unknown>; result: string; ok: boolean; latency_ms: number };
export type HistoryPlan = { attachmentKind?: string | null; intent: string; selectedTool?: string | null; selectedModel?: string | null; reason: string };

// L'erreur se propage : une conversation vide et un historique illisible ne
// sont pas le même état, et les confondre affichait « Commencez une nouvelle
// conversation » alors que les messages existaient toujours côté base.
export async function listMessages(conversationId: string): Promise<Array<{ id: string; role: string; content: string; thinking?: string; orchestrator_thinking?: string | null; brief?: string | null; plan?: HistoryPlan | string | null; toolcalls?: HistoryToolCall[]; attachments?: HistoryAttachment[] }>> {
  return invoke("list_messages", { conversationId } as unknown as Record<string, unknown>);
}

// Vignettes d'historique : bytes du blob via le service (objectURL côté front).
// D'où vient l'id : champ attachments[] de listMessages (ids art_xxx ou sources legacy).
// Pourquoi des bytes et pas un accès direct au fichier : l'IPC bytes marche quelle que
// soit l'origine du fichier (temp/ vs data/) ; le prix est un appel par image
// (conversations = quelques images, acceptable).
export async function getAttachmentBytes(artifactId: string): Promise<[number[], string]> {
  try {

    return invoke<[number[], string]>("get_attachment_bytes", { artifactId } as unknown as Record<string, unknown>);
  } catch (e) {
    throw toIpcError(e, "ATTACH_BYTES_FAILED");
  }
}

// === LISTEN : Python → main process → Frontend (stdout JSON Lines → subscribe) ===
// Chaque listen retourne une fonction unlisten() à appeler au démontage du composant

export function onMessageStarted(cb: (p: MessageStarted) => void) { return listen<MessageStarted>("message_started", e => cb(e.payload)); }
export function onThinking(cb: (p: Thinking) => void) { return listen<Thinking>("thinking", e => cb(e.payload)); }
export function onToolCall(cb: (p: ToolCall) => void) { return listen<ToolCall>("tool_call", e => cb(e.payload)); }
export function onToolResult(cb: (p: ToolResult) => void) { return listen<ToolResult>("tool_result", e => cb(e.payload)); }
export function onAnswer(cb: (p: Answer) => void) { return listen<Answer>("answer", e => cb(e.payload)); }
export function onMessageCompleted(cb: (p: MessageCompleted) => void) { return listen<MessageCompleted>("message_completed", e => cb(e.payload)); }
export function onError(cb: (p: ErrorEvent) => void) { return listen<ErrorEvent>("error", e => cb(e.payload)); }
export function onBackendReady(cb: (p: BackendReady) => void) { return listen<BackendReady>("backend_ready", e => cb(e.payload)); }
export function onExecutionPlan(cb: (p: ExecutionPlan) => void) { return listen<ExecutionPlan>("execution_plan", e => cb(e.payload)); }
export function onOrchestratorBrief(cb: (p: OrchestratorBrief) => void) { return listen<OrchestratorBrief>("orchestrator_brief", e => cb(e.payload)); }

/* ── Wrapper ajoutés après le passage à la couche Electron ───────────
   Ces surfaces n'existaient pas dans le pont d'origine : elles ont été
   ajoutées quand le dépôt de pièces jointes est devenu chaînable, puis
   perdues lors d'un retour arrière sur ce fichier. Elles sont regroupées ici
   plutôt que dispersées, pour que le prochain passage à l'arrière sache
   exactement quoi vérifier. */

/* ── Espaces ──────────────────────────────────────────────────────── */

export interface MemoryElement {
  entry_id: string;
  entry_key?: string;
  content: string;
  status?: string;
  category?: string | null;
  affirmation_status?: string | null;
  created_at?: string | null;
  origin?: string | null;
  origin_context?: string | null;
  context?: string | null;
  freshness?: string | null;
  sources_count?: number;
  source_refs?: string[];
  source_artifact_ids?: string[];
  version?: number;
}

export interface MemorySynthesis {
  content: string;
  version: number;
  created_at?: string | null;
  source_entry_ids?: string[];
}

export interface MemoryEntretien {
  tours: number;
  seuil_tours: number;
  inactivite_s: number | null;
  seuil_inactivite_s: number;
  etat: 'en_cours' | 'differe' | 'en_attente' | 'a_jour' | 'inconnu';
  raison: string | null;
  derniere_activite?: string | null;
  dernier_traitement?: string | null;
}

export interface WorkspaceMemorySnapshot {
  elements: MemoryElement[];
  synthese: MemorySynthesis | null;
  entretien?: MemoryEntretien | null;
}

export interface WorkspaceStats {  workspace_id: string;
  /** Compteurs RECALCULÉS par le service, jamais stockés. */
  discussions: number;
  fichiers: number;
  octets: number;
  /** Entrées de mémoire validées : un fait mesuré, pas un quota. */
  memoire: number;
  memoire_par_statut: {
    candidate: number;
    accepted: number;
    stale: number;
    rejected: number;
  };
}

export interface Workspace {
  id: string;
  name: string;
  description?: string | null;
  state?: string;
  created_at?: string | null;
  updated_at?: string | null;
  stats: WorkspaceStats;
}

export interface WorkspaceConversation {
  id: string;
  title: string;
  mode?: string;
  created_at?: string | null;
  updated_at?: string | null;
  deleted_at?: string | null;
  [cle: string]: unknown;
}

export async function listWorkspaces(): Promise<Workspace[]> {
  // Le pont Electron déballe déjà la réponse via RESULT_KEY : ce que
  // `invoke` renvoie EST le tableau. Relire `.workspaces` ici renvoyait
  // `undefined` sur un tableau, donc une liste vide en cas d'échec silencieux :
  // la page Espaces affichait « aucun espace » sans jamais lever d'erreur.
  return invoke<Workspace[]>("espace_list") ?? [];
}

export async function createWorkspace(
  name: string,
  description?: string,
): Promise<Workspace> {
  const reponse = await invoke<{ workspace?: Workspace }>("espace_create", {
    name,
    description,
  });
  if (!reponse?.workspace) throw new Error("Espace non renvoyé par le service.");
  return reponse.workspace;
}

export async function getWorkspace(id: string): Promise<{
  workspace: Workspace;
  stats: WorkspaceStats;
}> {
  return invoke<{ workspace: Workspace; stats: WorkspaceStats }>("espace_get", {
    workspaceId: id,
  });
}

export async function listWorkspaceConversations(
  workspaceId: string,
): Promise<WorkspaceConversation[]> {
  // Même contrat que listWorkspaces : le pont déballe, on relit pas.
  return invoke<WorkspaceConversation[]>(
    "espace_conversations",
    { workspaceId },
  ) ?? [];
}

export async function chargerMemoireEspace(
  workspaceId: string,
): Promise<WorkspaceMemorySnapshot> {
  const reponse = await invoke<{
    elements?: MemoryElement[];
    synthese?: MemorySynthesis | null;
    entretien?: MemoryEntretien | null;
  }>("espace_memoire", { workspaceId });
  return {
    elements: Array.isArray(reponse?.elements) ? reponse.elements : [],
    synthese: reponse?.synthese ?? null,
    entretien: reponse?.entretien ?? null,
  };
}

export async function listWorkspaceMemory(
  workspaceId: string,
): Promise<MemoryElement[]> {
  return (await chargerMemoireEspace(workspaceId)).elements;
}

export async function rafraichirMemoireEspace(
  workspaceId: string,
  options?: { force?: boolean; seuilMessages?: number; contenu?: string },
): Promise<unknown> {
  return invoke(
    "espace_memoire_rafraichir",
    {
      workspaceId,
      force: options?.force ?? false,
      seuilMessages: options?.seuilMessages,
      contenu: options?.contenu,
    },
  );
}

export async function forcerMemoireEspace(workspaceId: string): Promise<unknown> {
  return invoke("espace_memoire_forcer", { workspaceId });
}

export async function corrigerMemoireEspace(
  workspaceId: string,
  entryId: string,
  content: string,
  expectedVersion?: number,
): Promise<unknown> {
  return invoke("espace_memoire_corriger", {
    workspaceId,
    entryId,
    content,
    expectedVersion,
  });
}

export async function supprimerMemoireEspace(
  workspaceId: string,
  entryId: string,
): Promise<unknown> {
  return invoke("espace_memoire_supprimer", { workspaceId, entryId });
}

/* ── Le dépôt d'un espace ─────────────────────────────────────────────
   Le dépôt est une catégorie, pas un dossier : ce que l'UTILISATEUR y a
   versé. Ce qu'une discussion a déposé lui appartient et n'y figure pas
   (`backend/services/espaces.py:384`). Chaque fichier du dépôt est en
   revanche lisible par TOUTES les discussions de l'espace — c'est
   `scope.py:92-142` qui le déduit, et cette page n'ouvre aucun accès. */

export interface DepotFile {
  artifact_id: string;
  display_name: string;
  /** `pdf`, `image`, `sheet` ou `text` — un type ÉTABLI, jamais une extension devinée. */
  kind: string;
  size_bytes: number;
  /** Faux quand la ligne `blobs` est introuvable : `size_bytes` vaut alors 0 par
   *  convention de contrat, et NON parce que le fichier serait vide. */
  size_known: boolean;
  added_at: string;
  /** L'état d'ingestion, tel que `backend/ingestion/etat.py` l'écrit. Champ
   *  descriptif : absent sur une réponse ancienne, il reste lisible comme
   *  « état fourni par une version précédente du service », jamais comme
   *  « prêt ». depot.md §6 : l'état distingue disponible / partiel / échec. */
  ingest_state?: string;
  /** Motif lisible joint à l'état, quand le service en a un. */
  ingest_reason?: string;
}

/** Motifs d'écartement, tels que `services/espaces.py:_raison_ecarte` les écrit. */
export type DepotHiddenReason = "supprime" | "sans_nom" | "lien_orphelin";

/**
 * Un lien du dépôt que l'écran ne présente pas. Le service ne les ÉCARTE plus en
 * silence : sans cette clé, un dépôt amputé se lisait comme un dépôt complet.
 *
 * `artifact_id` est technique : un écran doit en COMPTER les motifs, jamais les
 * afficher ligne par ligne. Ce payload ne part pas vers le modèle — la ligne
 * d'annonce de `inference/loop.py:_depot_announcement` ne lit que `files`.
 */
export interface DepotHiddenFile {
  artifact_id: string;
  display_name: string;
  reason: DepotHiddenReason;
}

export interface DepotInventory {
  workspace_id: string;
  files: DepotFile[];
  /** Vide pour un dépôt sain. Jamais `undefined` ici : l'absence se confondrait
   *  avec un dépôt complet, qui est précisément ce qu'il ne faut pas affirmer. */
  hidden: DepotHiddenFile[];
  conversation_count: number;
  is_physical_folder: boolean;
}

export async function listWorkspaceDepot(
  workspaceId: string,
): Promise<DepotInventory> {
  const inventaire = await invoke<Partial<DepotInventory>>("espace_depot", {
    workspaceId,
  });
  // Un inventaire sans `files` n'est pas un dépôt vide : c'est une réponse
  // illisible. Les confondre afficherait « vous n'avez rien déposé » là où
  // l'écran n'a rien su, et l'utilisateur viderait un dépôt intact.
  if (!Array.isArray(inventaire?.files)) {
    throw new Error("Le service n'a pas renvoyé d'inventaire de dépôt.");
  }
  // `hidden` manquant n'est pas un dépôt sain : c'est un service qui ne dit pas
  // ce qu'il écarte. Le supposer vide rendrait à l'écran l'illusion que cette
  // clé sert à rien — exactement le silence qu'elle existe pour interdire.
  if (!Array.isArray(inventaire.hidden)) {
    throw new Error("Le service n'a pas dit ce qu'il a écarté du dépôt.");
  }
  return {
    workspace_id: inventaire.workspace_id ?? workspaceId,
    files: inventaire.files,
    hidden: inventaire.hidden,
    conversation_count: Number(inventaire.conversation_count ?? 0),
    is_physical_folder: inventaire.is_physical_folder === true,
  };
}

/** Retire un document du dépôt. Les octets, eux, restent : rien n'est purgé ici. */
export async function unshareWorkspaceFile(
  workspaceId: string,
  artifactId: string,
): Promise<{
  ok: boolean;
  link_id: string;
  already_removed: boolean;
  blob_preserved?: boolean;
}> {
  return invoke("espace_depot_retirer", { workspaceId, artifactId });
}

/* ── Renommer un document du dépôt ────────────────────────────────────
   Le parcours de `depot.md` §5.2 : « Cette action modifie le nom affiché
   par Thot, sans altérer son contenu ni les informations extraites qui en
   dépendent. » Le serment du pont est de ne transmettre QUE l'étiquette :
   le contenu, la fiche et son aperçu restent la responsabilité du service. */
export interface RetourRenommage {
  ok: boolean;
  artifact_id: string;
  /** Faux quand le document portait déjà ce nom : le renommage est idempotent. */
  changed: boolean;
  display_name: string;
}

export async function renommerDocumentDepot(
  workspaceId: string,
  artifactId: string,
  nouveauNom: string,
): Promise<RetourRenommage> {
  return invoke("espace_depot_renommer", { workspaceId, artifactId, nouveauNom });
}

/* ── Promouvoir un fichier de discussion vers le dépôt ────────────────
   espaces.md §6 : « Le dépôt rassemble les documents volontairement mis
   à disposition de toutes les discussions de l'espace » ; la procédure de
   promotion reste un point ouvert de la fiche (l.241). Le pont ne
   transmet AUCUNE cible : le serveur déduit l'espace du rattachement de
   la discussion — l'utilisateur ne choisit pas un espace, il verse dans
   celui de la discussion sous ses yeux — et un fichier privé ne sortira
   jamais d'un espace vers un autre sous couvert d'un partage. */
export interface RetourPartage {
  ok: boolean;
  link_id: string;
  created: boolean;
  artifact_id: string;
  note?: string;
}

export async function promouvoirVersDepot(
  conversationId: string,
  artifactId: string,
): Promise<RetourPartage> {
  return invoke("espace_conversation_partager", { conversationId, artifactId });
}

/* ── Dé-placer une discussion entre deux espaces ───────────────────────
   Le parcours de `espaces.md` §7.3 : un RAPPORT d'abord (sans `confirme`),
   une application ensuite. Le backend évalue ce qui transitera (la
   discussion, ses fichiers liés), ce qui reste à l'espace d'origine (le
   dépôt et la mémoire ne partent pas en bloc), et les fiches de mémoire
   qu'il marquera révisables — jamais de résultat fabriqué : les refus
   arrivent nommés ([CONVERSATION_NOT_FOUND],
   [WORKSPACE_NOT_FOUND]). */
export interface RapportDeplacementConversation {
  ok: boolean;
  allowed: boolean;
  conversation_id: string;
  source_workspace_id: string | null;
  target_workspace_id: string;
  /** Clé conservée pour l'enveloppe : §7.3 ne bloque plus, elle naît vide. */
  blocking: Array<Record<string, unknown>>;
  /** Liens de zone utilisateur qui restent À L'ESPACE D'ORIGINE : un fait
   *  à afficher, pas un refus. */
  lost_links: Array<{ artifact_id: string; display_name: string; reason: string }>;
  contributions: string[];
  /** Fiches de mémoire de l'origine que l'application marquera révisables. */
  syntheses_a_invalider: string[];
  /** Un déplacement ne déplace AUCUN octet : toujours 0, par contrat. */
  artifacts_moved: number;
  changed: boolean;
  note: string;
}

/** Évalue le déplacement : rien ne bouge, la discussion lit les conséquences. */
export async function evaluerDeplacementConversation(
  conversationId: string,
  targetWorkspaceId: string,
): Promise<RapportDeplacementConversation> {
  return invoke("espace_conversation_deplacer", {
    conversationId,
    targetWorkspaceId,
  });
}

/** Applique le déplacement : `confirme` s'écrit dans l'appel, jamais ailleurs. */
export function deplacerConversationEspace(
  conversationId: string,
  targetWorkspaceId: string,
): Promise<RapportDeplacementConversation> {
  return invoke("espace_conversation_deplacer", {
    conversationId,
    targetWorkspaceId,
    confirme: true,
  });
}

/* ------------------------------------------------------------------------
 * Intégrité des dépôts — la portée est TOUS les espaces.
 *
 * Trois états distincts, et l'écran doit pouvoir les dire séparément :
 *   · `sain` ET `lecture_complete`  → rien à signaler, et on a vraiment regardé ;
 *   · `!sain` ET `lecture_complete` → des anomalies, toutes comptées ;
 *   · `!lecture_complete`          → une table n'a pas répondu. Ce n'est PAS
 *                                     « zéro anomalie » : c'est « je n'ai pas su ».
 * Une réponse illisible LEVE, comme pour `listWorkspaceDepot` : l'écran affichera
 * « état inconnu », jamais « sain », parce qu'un dépôt dont on ignore l'état ne
 * doit pas s'affirmer sain.
 * ---------------------------------------------------------------------- */

export interface DepotIntegrityExample {
  type: string;
  display_name: string;
  raison: string;
  /** État d'ingestion de l'exemple. `echec` sur `extraction_absente` = à réessayer. */
  ingest_state?: string;
}

export interface DepotIntegritySpace {
  workspace_id: string;
  /** Le nom si la table des espaces a répondu, l'id sinon — jamais vide. */
  nom: string;
  nom_connu: boolean;
  par_type: Record<string, number>;
  /** Les refus décomposés, pas un total : l'écran doit pouvoir les nommer. */
  refuses_par_type: Record<string, number>;
  total: number;
  reparable: number;
  /** Échecs d'extraction relançables depuis le dépôt de cet espace. */
  a_reessayer: number;
  /** Refus de maintenance, HORS échecs relançables : les deux ne se confondent pas. */
  refuse: number;
  exemples: DepotIntegrityExample[];
}

export interface DepotIntegrityReport {
  ok: boolean;
  sain: boolean;
  lecture_complete: boolean;
  diagnostics: string[];
  total: number;
  reparable: number;
  /** Distingué de `refuse` : un échec d'extraction se relance, il ne se « répare » pas. */
  a_reessayer: number;
  refuse: number;
  espaces: DepotIntegritySpace[];
  mesure: Record<string, number>;
  lectures: number;
}

export interface DepotRepairReport {
  ok: boolean;
  avant: DepotIntegrityReport;
  apres: DepotIntegrityReport;
  repares: Record<string, number>;
  /** Ce que la réparation a refusé de faire, et ce qui a échoué. À montrer. */
  non_repares: Array<Record<string, unknown>>;
}

/**
 * Le moteur ne répond pas, ou répond une forme plus ancienne que celle attendue.
 *
 * Dans les deux cas l'action est la même : rouvrir l'application. Le plan ne les
 * distingue pas parce que l'architecture ne le permet plus — `electron/sidecar.cjs`
 * jette le `code` d'erreur et ne transmet que le `message` (`sidecar.cjs:52`), donc
 * toute discrimination se ferait sur une chaîne, et cassera à la première reformulation.
 *
 * Le détail technique — `Unknown espace.depot_integrite`, `thot:cmd` — part dans la
 * console, où l'atelier peut le lire. L'écran ne montre que ce que l'utilisateur peut
 * en FAIRE : une chaîne d'ingénieur affichée à un utilisateur est un mur, pas une
 * information.
 */
function moteurIndisponible(erreur: unknown, quoi: string): Error {
  console.error(`[ipc] ${quoi} : le moteur n'a pas répondu`, erreur);
  return new Error(
    "Le moteur de l’atelier ne répond pas. Rouvrez l’application pour le remettre en route.",
  );
}

/** La réponse a-t-elle la forme qu'on peut honnêtement afficher ? */
function lireRapport(charge: unknown, quoi: string): DepotIntegrityReport {
  const rapport = charge as Partial<DepotIntegrityReport> | undefined;
  // `diagnostics` absent n'est pas « aucune difficulté de lecture » : c'est un
  // service qui ne dit pas ce qu'il a su lire. Le supposer vide afficherait
  // « sain » là où personne n'a regardé.
  if (!Array.isArray(rapport?.diagnostics) || !Array.isArray(rapport?.espaces)) {
    console.error(`[ipc] rapport d'intégrité illisible (${quoi})`, rapport);
    throw new Error(
      "Le moteur a répondu dans une forme inattendue. Rouvrez l'application pour le remettre à jour.",
    );
  }
  return {
    ok: true,
    sain: rapport.sain === true && rapport.lecture_complete !== false,
    lecture_complete: rapport.lecture_complete === true,
    diagnostics: rapport.diagnostics,
    total: Number(rapport.total ?? 0),
    reparable: Number(rapport.reparable ?? 0),
    // Une réponse d'une version antérieure n'a pas ce compteur : 0 est la seule
    // lecture honnête, et l'écran n'inventera pas de relance.
    a_reessayer: Number(rapport.a_reessayer ?? 0),
    refuse: Number(rapport.refuse ?? 0),
    espaces: rapport.espaces.map((espace) => ({
      ...espace,
      a_reessayer: Number(espace.a_reessayer ?? 0),
    })),
    mesure: rapport.mesure ?? {},
    lectures: Number(rapport.lectures ?? 0),
  };
}

/** L'état des dépôts, tous espaces confondus. Lecture seule. */
export async function checkWorkspaceDepots(): Promise<DepotIntegrityReport> {
  try {
    return lireRapport(await invoke("espace_depot_verifier", {}), "vérification");
  } catch (erreur) {
    // `lireRapport` parle déjà en phrases d'écran ; on ne le ré-emballe pas.
    if (erreur instanceof Error && erreur.message.startsWith("Le moteur")) throw erreur;
    throw moteurIndisponible(erreur, "espace_depot_verifier");
  }
}

/**
 * Répare ce qui se répare. N'envoie `confirme` qu'une fois la confirmation
 * obtenue à l'écran : le canal refuse tout appel qui ne l'exprime pas, parce
 * qu'un appel égaré modifierait la base sans que personne l'ait demandé.
 * Rien n'est supprimé — c'est pourquoi la confirmation n'est pas un avertissement.
 */
export async function repairWorkspaceDepots(): Promise<DepotRepairReport> {
  let brut: Partial<DepotRepairReport>;
  try {
    brut = (await invoke("espace_depot_reparer", { confirme: true })) as
      Partial<DepotRepairReport>;
  } catch (erreur) {
    // Un refus du moteur — confirmation absente, base verrouillée — porte un message
    // qui lui est propre et reste affichable tel quel : il dit ce qui a été refusé.
    // Seule une panne de transport devient « rouvrez l'application ».
    const texte = typeof erreur === 'string' ? erreur : String(erreur ?? '');
    if (texte.includes('CONFIRMATION_REQUIRED')) {
      throw new Error("La réparation n’a pas été confirmée. Aucun dépôt n’a été modifié.");
    }
    throw moteurIndisponible(erreur, "espace_depot_reparer");
  }
  if (!brut?.apres || !brut?.avant) {
    console.error('[ipc] bilan de réparation illisible', brut);
    throw new Error(
      "Le moteur a répondu dans une forme inattendue. Rouvrez l'application pour le mettre à jour.",
    );
  }
  return {
    ok: true,
    avant: lireRapport(brut.avant, "avant"),
    apres: lireRapport(brut.apres, "après"),
    repares: brut.repares ?? {},
    non_repares: Array.isArray(brut.non_repares) ? brut.non_repares : [],
  };
}

/* Ajout au dépôt : stager, puis promouvoir. Il n'existe pas de commande
   « ajouter au dépôt » — la promotion `attachments_prepare` décide seule où va
   un fichier, et elle refuse un espace en corbeille.

   Ces wrappers reprennent les VRAIES formes de `attachments.py:249` :
   `{items, rejected}` où chaque item est PLAT et en snake_case
   (`stage_item_id`, `name`, `mime`…). Les types `StagingItem` /
   `StagingResponse` plus haut annoncent `{stageId, items: [{stageItemId}]}`,
   qui n'a jamais existé : on ne s'y appuie pas, et on ne les corrige pas
   ici — ce sont les chemins de discussion, pas celui du dépôt. */

export interface DepotStageItem {
  stage_item_id: string;
  sha256: string;
  name: string;
  mime: string;
  kind: string;
  size_bytes: number;
  draft_id: string;
}

export interface DepotStage {
  items: DepotStageItem[];
  rejected: Array<{ name?: string; message?: string }>;
}

export interface DepotPromotion {
  ok: boolean;
  artifact_ids: string[];
  count: number;
  scope_kind: string;
}

/** Fichiers choisis dans le système : le serveur les lit lui-même, chemins à l'appui. */
export async function stageDepotPaths(
  requestId: string,
  paths: string[],
): Promise<DepotStage> {
  return invoke<DepotStage>("stage_dropped_paths", { requestId, paths });
}

export async function stageDepotBytes(
  requestId: string,
  filename: string,
  bytes: Uint8Array,
): Promise<DepotStage> {
  if (bytes.length > MAX_PICKER_ATTACHMENT_BYTES) {
    throw new Error(`Fichier trop volumineux : ${filename} dépasse 20 Mo.`);
  }
  return invoke<DepotStage>("stage_picker_bytes", {
    requestId,
    filename,
    bytes: bytesToBase64(bytes),
  });
}

/**
 * `names` est indispensable : le staging n'a gardé que l'empreinte, et sans
 * l'étiquette d'origine l'inventaire afficherait un sha256. Elle est parallèle
 * à `stageItemIds` — le service les zippe position par position.
 */
export async function prepareDepotFiles(
  workspaceId: string,
  stageItemIds: string[],
  names: string[],
  requestId: string,
): Promise<DepotPromotion> {
  return invoke<DepotPromotion>("attachments_prepare", {
    workspaceId,
    stageItemIds,
    names,
    requestId,
  });
}

export interface ArtifactIngestionRetryResult {
  ok: boolean;
  /** `false` quand un travail couvre déjà le document : rien n'a été relancé. */
  accepted: boolean;
  artifact_id: string;
  state: string;
  reason?: string | null;
}

/**
 * Relance l'extraction d'UN document du dépôt. Le serveur décide seul du modèle
 * (`modeles_autorises=True`) et vérifie que le document est toujours partagé dans
 * un espace actif. `accepted: false` est une réponse honnête, pas une erreur :
 * l'écran doit dire « déjà en file/en cours », jamais « relancé ».
 */
export async function relancerExtraction(
  artifactId: string,
): Promise<ArtifactIngestionRetryResult> {
  return invoke<ArtifactIngestionRetryResult>("artifact_ingestion_retry", {
    artifactId,
  });
}

/* ── Galerie et images ───────────────────────────────────────────── */

export interface ImageScope {
  scope_kind: string;
  scope_id?: string;
  role?: string;
}

export interface ImageSpace {
  id: string;
  name: string;
}

export interface ImageItem {
  artifact_id: string;
  display_name: string;
  original_filename: string;
  created_at: string | null;
  kind: string;
  size_bytes: number;
  mime_detected: string;
  is_import: boolean;
  experimental: boolean;
  deprecated: boolean;
  /** Provenance de la génération : ce que la fiche a pu établir. */
  /** Provenance : présente quand la fiche a pu l'établir, absente sinon.
   *  L'interface teste l'absence (`isDeprecatedQuality(undefined)` doit rester
   *  faux), donc ces champs ne sont pas des chaînes vides par convention. */
  prompt?: string | null;
  backend?: string | null;
  quality?: string | null;
  aspect_ratio?: string | null;
  width?: string | number | null;
  height?: string | number | null;
  seed?: string | number | null;
  checkpoint?: string | null;
  origin?: string | null;
  run_id?: string | null;
  scopes: ImageScope[];
  /** Espaces auxquels l'image est rattachée. Absent des anciennes réponses. */
  spaces?: ImageSpace[];
}

export interface ImageListPage {
  items: ImageItem[];
  next_offset: number;
  has_more: boolean;
}

export async function imageList(
  params: {
    offset?: number;
    limit?: number;
    /** Les deux noms sont acceptés : la page de galerie écrit `include_imports`,
     *  les tests historiques écrivent `includeImports`. Les normaliser ici évite
     *  de casser l'un des deux à chaque évolution du nom d'argument. */
    include_imports?: boolean;
    includeImports?: boolean;
    scopeId?: string;
    scope_id?: string;
    spaceId?: string;
    noSpace?: boolean;
  } = {},
): Promise<ImageListPage> {
  let brut: Partial<ImageListPage> | ImageItem[];
  // Les filtres d'espace ne sont transmis que s'ils sont demandés : l'appel
  // par défaut garde exactement la même forme d'arguments qu'avant.
  const spaceParams: Record<string, unknown> = {};
  if (params.spaceId) spaceParams.spaceId = params.spaceId;
  if (params.noSpace) spaceParams.noSpace = true;
  try {
    brut = await invoke<Partial<ImageListPage> | ImageItem[]>("image_list", {
      offset: params.offset ?? 0,
      limit: params.limit ?? 24,
      includeImports: params.includeImports ?? params.include_imports ?? false,
      scopeId: params.scopeId ?? params.scope_id,
      ...spaceParams,
    });
  } catch (e) {
    // On ne laisse remonter l'erreur brute du transport : « base indisponible »
    // ne dit rien à l'utilisateur. Le code permet à la page de proposer un retry
    // ciblé au lieu d'afficher un échec technique.
    throw toIpcError(e, "IMAGE_LIST_FAILED");
  }
  // Le service rend un objet `{items, next_offset, has_more}`. Les anciennes
  // versions renvoyaient un tableau nu ; on l'accepte encore pour ne pas
  // casser une galerie déjà en cache chez l'utilisateur.
  if (Array.isArray(brut)) {
    // Un tableau nu est l'ancienne forme ; `next_offset` se déduit du décalage
    // demandé, sinon la page suivante recommence au début et l'utilisateur voit
    // les mêmes images en boucle.
    return {
      items: brut,
      next_offset: (params.offset ?? 0) + brut.length,
      has_more: false,
    };
  }
  return {
    items: brut?.items ?? [],
    next_offset: brut?.next_offset ?? 0,
    has_more: Boolean(brut?.has_more),
  };
}

export interface ImageRenameResult {
  ok: boolean;
  artifact_id: string;
  changed: boolean;
  display_name: string;
}

export async function imageRename(artifactId: string, name: string): Promise<ImageRenameResult> {
  return invoke<ImageRenameResult>("image_rename", { artifactId, name });
}

export async function imageGenerate(params: {
  request_id: string;
  prompt: string;
  aspect_ratio?: string;
  reference_ids?: string[];
  seed?: number;
  idempotency_key?: string;
  mode?: string;
  source_artifact_id?: string;
  work_token?: string;
  mask_token?: string;
  reference_tokens?: string[];
  work_sha256?: string;
  mask_sha256?: string;
  reference_sha256?: string | string[];
  canvas_width?: number;
  canvas_height?: number;
  strength?: number;
  mask_kind?: 'fill' | 'edit' | null;
  maskKind?: 'fill' | 'edit' | null;
  session_id?: string;
  revision?: string;
  source_transform?: { x: number; y: number; width: number; height: number };
  source_size?: { width: number; height: number };
}): Promise<void> {
  const charge: Record<string, unknown> = {
    requestId: params.request_id,
    prompt: params.prompt,
  };
  if (params.aspect_ratio !== undefined) charge.aspectRatio = params.aspect_ratio;
  if (params.reference_ids !== undefined) charge.referenceIds = params.reference_ids;
  if (params.seed !== undefined) charge.seed = params.seed;
  if (params.idempotency_key !== undefined) charge.idempotencyKey = params.idempotency_key;
  if (params.mode !== undefined) charge.mode = params.mode;
  if (params.source_artifact_id !== undefined) charge.sourceArtifactId = params.source_artifact_id;
  if (params.work_token !== undefined) charge.workToken = params.work_token;
  if (params.mask_token !== undefined) charge.maskToken = params.mask_token;
  if (params.reference_tokens !== undefined) charge.referenceTokens = params.reference_tokens;
  if (params.work_sha256 !== undefined) charge.workSha256 = params.work_sha256;
  if (params.mask_sha256 !== undefined) charge.maskSha256 = params.mask_sha256;
  if (params.reference_sha256 !== undefined) charge.referenceSha256 = params.reference_sha256;
  if (params.canvas_width !== undefined) charge.canvasWidth = params.canvas_width;
  if (params.canvas_height !== undefined) charge.canvasHeight = params.canvas_height;
  if (params.strength !== undefined) charge.strength = params.strength;
  if (params.session_id !== undefined) charge.sessionId = params.session_id;
  if (params.revision !== undefined) charge.revision = params.revision;
  if (params.mask_kind !== undefined) charge.maskKind = params.mask_kind;
  if (params.maskKind !== undefined) charge.maskKind = params.maskKind;
  if (params.source_transform !== undefined) charge.sourceTransform = params.source_transform;
  if (params.source_size !== undefined) charge.sourceSize = params.source_size;
  await invoke("image_generate", charge);
}

export async function imageStageWork(bytes: Uint8Array | number[] | ArrayBuffer | ArrayBufferView, mime?: string): Promise<{ token: string }> {
  const view = bytes instanceof Uint8Array
    ? bytes
    : bytes instanceof ArrayBuffer
      ? new Uint8Array(bytes)
      : ArrayBuffer.isView(bytes)
        ? new Uint8Array(bytes.buffer, bytes.byteOffset, bytes.byteLength)
        : new Uint8Array(bytes as number[]);
  // Garantir un tableau JSON sérialisable de valeurs numériques (octets),
  // au lieu de laisser l'IPC sérialiser un typed array en objet indexé.
  return invoke<{ token: string }>("image_stage_work", {
    bytes: Array.from(view),
    mime: mime ?? "image/png",
  } as unknown as Record<string, unknown>);
}

export async function deleteImage(artifactId: string): Promise<void> {
  await invoke("image_delete", { artifactId });
}

export async function imageSetBackend(enabled: boolean): Promise<void> {
  await invoke("image_set_backend", { enabled });
}

/* ── Réglages de la recherche web ─────────────────────────────────── */

export interface WebSettingsPayload {
  request_id: string;
  enabled: boolean;
  effective_enabled: boolean;
  provider: string;
  provider_options?: {
    name: string;
    label: string;
    /** Types de recherche couverts par ce moteur, tels que les définit le
     *  service. Rendu tel quel dans l'interface : c'est lui qui sait dire ce que
     *  « Actualités » ne couvre pas. */
    covers?: string[];
    coverage?: string;
    configured?: boolean;
    requires?: string;
    selected?: boolean;
  }[];
  searxng_url: string;
  allowed_domains?: string;
  domains_unrestricted?: string[];
  source: string;
  mode?: string;
  /** Faux quand une variable d'environnement prime sur le fichier : changer
   *  la case ne bougera rien, et l'interface doit le dire au lieu de mentir. */
  writable: boolean;
  written: boolean;
  error: string;
}

export async function webSettingsRequest(requestId: string): Promise<void> {
  await invoke("web_settings_request", { requestId });
}

export async function webSettingsSet(
  patch: Record<string, unknown>,
): Promise<void> {
  await invoke("web_settings_set", patch);
}

/* ── Événements de cycle de vie ───────────────────────────────────── */

export interface RunStartedEvent { request_id: string; run_id: string; conversation_id?: string; kind?: string }
export interface RunCompletedEvent {
  request_id: string;
  run_id: string;
  status?: string;
  /** Publié quand le run a produit un artefact : la galerie s'y abonne. */
  artifact_id?: string | null;
  [cle: string]: unknown;
}
export interface RunFailedEvent { request_id: string; run_id: string; code?: string; message?: string }
export interface RunCancelledEvent {
  request_id: string;
  run_id: string;
  /** Motif de l'annulation, pour ne pas laisser l'utilisateur deviner. */
  message?: string | null;
}
export interface RunCancellingEvent { request_id: string; run_id: string }
export interface RunStageEvent {
  request_id: string;
  run_id: string;
  stage: string;
  status: string;
  [cle: string]: unknown;
}
export interface ArtifactCreatedEvent {
  request_id: string;
  run_id?: string;
  artifact_id: string;
  experimental: boolean;
  public_id?: string;
}
export interface BackendTerminatedEvent {
  code: number | null;
  message: string;
  retryable: boolean;
}
export interface MemoryStartedEvent {
  request_id?: string | null;
  workspace_id: string;
  workspace_name?: string | null;
  kind: 'memory';
  mode: 'auto' | 'force';
  raison?: string | null;
  tours?: number | null;
  inactivite_s?: number | null;
  started_at?: string | null;
}
export interface MemoryDeferredEvent {
  workspace_id: string;
  workspace_name?: string | null;
  statut: 'differe' | 'en_attente';
  raison?: string | null;
  bloque_par?: string | null;
  tours?: number | null;
  retry_after_s?: number | null;
}
export interface MemoryFinishedEvent {
  workspace_id: string;
  workspace_name?: string | null;
  statut: string;
  raison?: string | null;
  tours?: number | null;
  messages?: number | null;
  faits?: number | null;
  fiches?: Record<string, number> | null;
  synthese?: { statut?: string; version?: number | null } | null;
  duree_ms?: number | null;
  code?: string | null;
  message?: string | null;
}
export interface ImageStatus {
  request_id: string;
  available: boolean;
  reason?: string | null;
  enabled?: boolean;
  details?: Record<string, unknown> | null;
}

/**
 * Un événement d'ingestion, hors cycle `run_*`.
 *
 * `workspace_id` est déduit côté serveur du lien d'espace (ou de la conversation) :
 * il peut être `null` quand aucun lien actif ne rattache encore l'artefact. L'écran
 * ne rafraîchit alors rien plutôt que de deviner un espace.
 */
export interface ArtifactIngestionEvent {
  artifact_id: string;
  workspace_id?: string | null;
  state: string;
  reason?: string | null;
  kind?: string | null;
  /** Vrai pour les états qu'une relance peut améliorer (`depose`, `echec`, ...). */
  retryable?: boolean;
  /** Nom réel de l'événement, ajouté par le wrapper (`artifact_ingestion_*`). */
  event?: "artifact_ingestion_started" | "artifact_ingestion_finished" | "artifact_ingestion_failed";
}

export function onRunStarted(cb: (p: RunStartedEvent) => void) {
  return listen<RunStartedEvent>("run_started", (e) => cb(e.payload));
}
export function onRunCompleted(cb: (p: RunCompletedEvent) => void) {
  return listen<RunCompletedEvent>("run_completed", (e) => cb(e.payload));
}
export function onRunFailed(cb: (p: RunFailedEvent) => void) {
  return listen<RunFailedEvent>("run_failed", (e) => cb(e.payload));
}
export function onRunCancelled(cb: (p: RunCancelledEvent) => void) {
  return listen<RunCancelledEvent>("run_cancelled", (e) => cb(e.payload));
}
export function onRunCancelling(cb: (p: RunCancellingEvent) => void) {
  return listen<RunCancellingEvent>("run_cancelling", (e) => cb(e.payload));
}
export function onRunStage(cb: (p: RunStageEvent) => void) {
  return listen<RunStageEvent>("run_stage", (e) => cb(e.payload));
}
export function onArtifactCreated(cb: (p: ArtifactCreatedEvent) => void) {
  return listen<ArtifactCreatedEvent>("artifact_created", (e) => cb(e.payload));
}

const INGESTION_EVENTS = [
  "artifact_ingestion_started",
  "artifact_ingestion_finished",
  "artifact_ingestion_failed",
] as const;

/**
 * Abonne aux trois événements d'ingestion et rend un désabonnement unique.
 *
 * Les trois sont écoutés séparément parce que `listen` d'`ipc.ts` filtre sur un
 * nom exact ; ce wrapper recompose la famille pour que l'écran n'ait qu'une
 * souscription à nettoyer. Le nom de l'événement est ajouté au payload : un
 * appelant qui veut distinguer « démarré » d'un terminal le peut.
 */
export function onArtifactIngestion(cb: (p: ArtifactIngestionEvent) => void) {
  let annule = false;
  const abonnements: Array<() => void> = [];
  const prets = INGESTION_EVENTS.map((event) =>
    listen<ArtifactIngestionEvent>(event, (e) => cb({ ...e.payload, event })),
  );
  return Promise.all(prets).then((offs) => {
    if (annule) {
      for (const off of offs) off();
      return () => {};
    }
    abonnements.push(...offs);
    return () => {
      annule = true;
      for (const off of abonnements.splice(0)) off();
    };
  });
}
export function onBackendTerminated(cb: (p: BackendTerminatedEvent) => void) {
  return listen<BackendTerminatedEvent>("backend_terminated", (e) => cb(e.payload));
}
export function onMemoryStarted(cb: (p: MemoryStartedEvent) => void) {
  return listen<MemoryStartedEvent>("memory_started", (e) => cb(e.payload));
}
export function onMemoryDeferred(cb: (p: MemoryDeferredEvent) => void) {
  return listen<MemoryDeferredEvent>("memory_deferred", (e) => cb(e.payload));
}
export function onMemoryFinished(cb: (p: MemoryFinishedEvent) => void) {
  return listen<MemoryFinishedEvent>("memory_finished", (e) => cb(e.payload));
}
export function onWebSettings(cb: (p: WebSettingsPayload) => void) {
  return listen<WebSettingsPayload>("web_settings", (e) => cb(e.payload));
}
/** Sonde de disponibilité du moteur d'images, pour trancher « je ne peux pas »
 *  sur un fait observé plutôt que sur une devinette. */
export function imageStatus(requestId: string): Promise<ImageStatus> {
  return invoke<ImageStatus>("image_status", { requestId });
}

export function onImageStatus(cb: (p: ImageStatus) => void) {
  return listen<ImageStatus>("image_status", (e) => cb(e.payload));
}

/** `unvalidated*` : qualité déclarée mais jamais confrontée à une preuve. */
export function isDeprecatedQuality(quality: unknown): boolean {
  return typeof quality === "string" && quality.trim().toLowerCase().startsWith("unvalidated");
}

/* ── Pièces jointes : aperçu de dépôt ─────────────────────────────── */

/**
 * Octets d'aperçu d'un fichier en cours de dépôt — jamais le fichier entier.
 *
 * Le service renvoie `data` en base64 : `emit` sérialise en JSON, et des
 * `bytes` Python y lèveraient `TypeError`. On décode ici pour que les appelants
 * construisent un `Blob` directement. Le service borne déjà la taille (aperçu,
 * pas fichier), ce qui évite qu'un PDF de vingt mégaoctets traverse le pont.
 */
export async function getStagedPreviewBytes(stageItemId: string): Promise<Uint8Array> {
  const reponse = await invoke<{
    data?: string | number[] | Uint8Array;
    encoding?: string;
    mime?: string;
  }>("get.staged_preview_bytes", { stageItemId });
  const brut = reponse?.data ?? "";
  // Le service envoie du base64 (JSON ne porte pas de `bytes`). La branche
  // tableau reste pour un ancien pont qui aurait déjà normalisé la réponse.
  if (typeof brut === "string") return base64ToBytes(brut);
  return brut instanceof Uint8Array ? brut : new Uint8Array(brut);
}
