'use strict'

// Surface exposée au renderer — liste blanche explicite.
//
  // Aucun bac a sable ne borne les canaux IPC : exposer
  // `invoke: (channel, args) => ipcRenderer.invoke(channel, args)` laisserait le
  // renderer viser n'importe quel canal, donc executer du code a distance. Les
  // canaux sont donc FIGES ici, dans la source du preload : le renderer ne peut
  // pas en choisir un autre, et le main process refuse de meme tout nom absent
  // du registre. Un verbe par commande, jamais un routage generique.
//
// CONTRAINTE STRUCTURELLE : la fenêtre est ouverte avec `sandbox: true`, donc ce
// fichier ne peut `require()` QUE 'electron'. Il doit rester autonome — pas de
// `require('./commands.cjs')`, qui échouerait en silence et laisserait
// `window.thot` indéfini. La liste est donc dupliquée depuis commands.cjs (que le
// main process utilise pour valider) et l'egalite des deux listes est garantie
// par `tests/electron/commands.test.cjs`.
//
// Le frontend appelle deja ces noms dans `frontend/src/lib/ipcBridge.ts` : seule la
// source de l'implementation change, pas les appels.

const { contextBridge, ipcRenderer, webUtils } = require('electron')

const COMMANDS = Object.freeze([
  'chat_send',
  'cancel_run',
  'image_generate',
  'image_status',
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
  'save_upload',
  'stage_dropped_paths',
  'stage_picker_bytes',
  'attachments_prepare',
  'artifact_ingestion_retry',
  'get_staged_preview_bytes',
  'get_attachment_bytes',
  'image_stage_work',
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

/** Un verbe nomme par commande : `thot.chat_send(...)`, jamais un canal libre. */
const api = {}
for (const command of COMMANDS) {
  api[command] = (args) => ipcRenderer.invoke('thot:cmd', command, args)
}

contextBridge.exposeInMainWorld('thot', {
  ...api,

  /**
   * Abonne le renderer aux evenements du sidecar.
   * `backend_ready` peut preceder le montage : le main process le met en tampon
   * et le rejoue, donc le renderer le recoit toujours.
   */
  subscribe: (handler) => {
    const listener = (_event, name, payload) => handler(name, payload)
    ipcRenderer.on('thot:event', listener)
    ipcRenderer.send('thot:renderer-ready')
    return () => ipcRenderer.removeListener('thot:event', listener)
  },

  /**
   * Chemin absolu d'un fichier depose. `File.path` a ete retire d'Electron :
   * `webUtils.getPathForFile` le remplace. L'appel est synchrone et local au
   * renderer — un objet `File` n'etant pas serialisable, il ne doit jamais
   * traverser l'IPC.
   */
  pathForFile: (file) => {
    try {
      return webUtils.getPathForFile(file)
    } catch {
      return null
    }
  },

  paths: () => ipcRenderer.invoke('thot:paths')
})

module.exports = { COMMANDS }
