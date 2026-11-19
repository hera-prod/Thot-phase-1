'use strict'

// Registre des commandes, source unique.
//
// Importé par le preload (qui n'expose que ces verbes) ET par le main process
// (qui refuse tout canal absent du registre). Le preload NE PEUT PAS être
// importé depuis le main : il appelle `contextBridge.exposeInMainWorld` au
// chargement, ce qui n'a de sens que dans un renderer.

const COMMANDS = Object.freeze([
  // Écritures vers le stdin du sidecar Python.
  'chat_send',
  'cancel_run',
  'image_generate',
  'image_status',
  // Lectures et écritures base, résolues par le sidecar Python.
  'list_conversations',
  'rename_conversation',
  'delete_conversation',
  'get_conversation',
  'list_messages',
  'image_list',
  'image_delete',
  'image_rename',
  'image_set_backend',
  'web_settings_set',
  'web_settings_request',
  // Pièces jointes.
  'save_upload',
  'stage_dropped_paths',
  'stage_picker_bytes',
  'attachments_prepare',
  'artifact_ingestion_retry',
  'get_staged_preview_bytes',
  'get_attachment_bytes',
  'image_stage_work',
  // Espaces (C17). Ce sont des COMMANDES UTILISATEUR, pas des outils : le
  // registre ne dit rien de ce que le modèle peut appeler, il dit seulement
  // quels canaux existent. `01_classe_espace.md:74-84` interdit de les
  // exposer comme outils ; ici elles sont des verbes que le renderer invoque
  // lui-même, ce qui n'ouvre aucun chemin vers le modèle.
  'espace_list',
  'espace_create',
  'espace_get',
  'espace_stats',
  'espace_conversations',
  'espace_memoire',
  'espace_memoire_rafraichir',
  'espace_memoire_forcer',
  'espace_memoire_corriger',
  'espace_memoire_supprimer',
  'espace_depot',
  'espace_depot_retirer',
  'espace_depot_verifier',
  'espace_depot_reparer',
  'espace_conversation_deplacer',
  'espace_conversation_partager',
  'espace_depot_renommer',
])

const ALLOWED = new Set(COMMANDS)

/** Canaux dont la réponse arrive en événement, pas en valeur de retour. */
const FIRE_AND_FORGET = new Set([
  'chat_send',
  'cancel_run',
  'image_generate',
  'image_status',
  'web_settings_set',
  'web_settings_request'
])

/** Commandes routées vers une méthode de protocole du sidecar. */
const SIDECAR_METHODS = Object.freeze({
  chat_send: 'chat.send',
  cancel_run: 'cancel',
  image_generate: 'image.generate',
  image_status: 'image.status',
  image_list: 'image.list',
  web_settings_set: 'web.settings',
  web_settings_request: 'web.settings',
  // Méthodes base : à porter côté Python (elles vivaient en rusqlite dans Rust).
  list_conversations: 'list.conversations',
  get_conversation: 'get.conversation',
  list_messages: 'list.messages',
  rename_conversation: 'rename.conversation',
  delete_conversation: 'delete.conversation',
  image_delete: 'image.delete',
  image_rename: 'image.rename',
  image_set_backend: 'image.set_backend',
  // Pièces jointes.
  save_upload: 'save.upload',
  stage_dropped_paths: 'stage.paths',
  stage_picker_bytes: 'stage.picker_bytes',
  attachments_prepare: 'attachments.prepare',
  artifact_ingestion_retry: 'artifact.ingestion_retry',
  get_staged_preview_bytes: 'get.staged_preview_bytes',
  get_attachment_bytes: 'get.attachment_bytes',
  // Espaces.
  espace_list: 'espace.list',
  espace_create: 'espace.create',
  espace_get: 'espace.get',
  espace_stats: 'espace.stats',
  espace_conversations: 'espace.conversations',
  espace_memoire: 'espace.memoire',
  espace_memoire_rafraichir: 'espace.memoire.rafraichir',
  espace_memoire_forcer: 'espace.memoire.forcer',
  espace_memoire_corriger: 'espace.memoire.corriger',
  espace_memoire_supprimer: 'espace.memoire.supprimer',
  espace_depot: 'espace.depot',
  espace_depot_retirer: 'espace.depot_retirer',
  espace_depot_verifier: 'espace.depot_integrite',
  espace_depot_reparer: 'espace.depot_reparer',
  espace_conversation_deplacer: 'espace.conversation_deplacer',
  espace_conversation_partager: 'espace.conversation_partager',
  espace_depot_renommer: 'espace.depot_renommer',
})

/**
 * L'événement de réponse suit la méthode : le point devient un tiret bas
 * (`image.list` -> `image_list`). C'est la convention déjà en place côté Python
 * pour `image_list`, `image_status` et `web_settings`.
 */
function replyEventFor (method) {
  return method.replace(/\./g, '_')
}

/**
 * Cle sous laquelle le payload de reponse contient la valeur attendue.
 * `null` = renvoyer le payload entier moins `request_id`.
 *
   * Le renderer attend la valeur DIRECTEMENT (`invoke<ConversationSummary[]>`),
   * alors que le protocole émet un événement enveloppé. Sans cette table, il
   * recevrait `{conversations: [...]}` là où il attend un tableau — un objet
   * qui ressemble au contenu sans l'être, et l'échec est silencieux.
   */
const RESULT_KEY = Object.freeze({
  list_conversations: 'conversations',
  list_messages: 'messages',
  // Les espaces rendent `{workspaces: [...]}` et `{conversations: [...]}` : sans
  // cette table, le frontend recevrait un objet là où il attend un tableau.
  espace_list: 'workspaces',
  espace_conversations: 'conversations'
})

module.exports = { COMMANDS, ALLOWED, FIRE_AND_FORGET, SIDECAR_METHODS, RESULT_KEY, replyEventFor }
