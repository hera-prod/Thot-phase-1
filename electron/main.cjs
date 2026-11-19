'use strict'

// Processus principal Electron.
//
// Modèle de sécurité, imposed ici et non laissé au developers d'après la revue :
//   - `contextIsolation: true` / `nodeIntegration: false` / `sandbox: true` ;
//   - `ipcRenderer` n'est jamais exposé au renderer, seulement des verbes
//     nommés (electron/preload.cjs) ;
//   - toute navigation et toute `window.open` sont bloquées ;
//   - toutes les permissions (média, géoloc, notifications) sont refusées.
//
// `app://` est enregistré en schéma privilégié AVANT `whenReady()` : c'est un
// piège d'ordre classique, l'enregistrement tardif fait échouer le chargement.

const path = require('node:path')
const { existsSync, readFileSync, statSync, mkdirSync, writeFileSync, renameSync, unlinkSync, lstatSync } = require('node:fs')
const crypto = require('node:crypto')
const { app, BrowserWindow, ipcMain, protocol, net, shell } = require('electron')
const { Sidecar } = require('./sidecar.cjs')
const { ALLOWED, FIRE_AND_FORGET, SIDECAR_METHODS, RESULT_KEY, replyEventFor } = require('./commands.cjs')

const PROJECT_ROOT = path.resolve(__dirname, '..')
const FRONTEND_DIST = path.join(PROJECT_ROOT, 'frontend', 'dist')
// Le mode est piloté par l'environnement, pas par `app.isPackaged` : on lance
// toujours Electron non empaqueté, donc ce test désignait toujours le dev.
// Sans THOT_DEV_SERVER, l'app charge le bundle `dist` via app:// — aucun port,
// aucun Vite, donc plus aucune classe de problème d'orphelin.
const DEV_SERVER_URL = process.env.THOT_DEV_SERVER || ''
const isDev = DEV_SERVER_URL !== ''

let mainWindow = null
let sidecar = null
// `backend_ready` est émis par Python au démarrage, potentiellement avant que le
// renderer ne soit monté. On le retient et on le rejoue à l'abonnement.
const bufferedEvents = []
let rendererSubscribed = false

// ─── Schéma applicatif privilégié ────────────────────────────────────────────
const __electron = require('electron')
if (__electron && typeof __electron.protocol?.registerSchemesAsPrivileged === 'function') {
  __electron.protocol.registerSchemesAsPrivileged([
    { scheme: 'app', privileges: { standard: true, secure: true, supportFetchAPI: true } }
  ])
}

function log (line) {
  process.stdout.write(`${line}\n`)
}

function broadcast (name, payload) {
  if (rendererSubscribed && mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('thot:event', name, payload)
  } else {
    // Reservoir borné : au-delà de 200 événements d'avance, on abandonne les
    // plus anciens plutôt que de faire grossir la mémoire sans limite.
    bufferedEvents.push({ name, payload })
    if (bufferedEvents.length > 200) bufferedEvents.shift()
  }
}

function createWindow () {
  mainWindow = new BrowserWindow({
    title: 'Thot Phase 1',
    width: 1200,
    height: 800,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      spellcheck: false
    }
  })

  mainWindow.once('ready-to-show', () => mainWindow.show())

  // Le contenu vient d'une origine unique : toute navigation est un défaut.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) shell.openExternal(url)
    return { action: 'deny' }
  })
  mainWindow.webContents.on('will-navigate', (event, url) => {
    const allowed = isDev ? url.startsWith(DEV_SERVER_URL) : url.startsWith('app://thot/')
    if (!allowed) event.preventDefault()
  })
  mainWindow.webContents.session.setPermissionRequestHandler((_wc, _perm, callback) => callback(false))
  mainWindow.webContents.session.setPermissionCheckHandler(() => false)

  mainWindow.on('closed', () => { mainWindow = null })

  if (isDev) {
    log(`[thot] mode developpement -> ${DEV_SERVER_URL}`)
    mainWindow.loadURL(DEV_SERVER_URL)
  } else {
    log('[thot] mode bundle local -> app://thot/')
    mainWindow.loadURL(process.env.THOT_ROUTE || 'app://thot/')
  }
}

// ─── Service `app://` ────────────────────────────────────────────────────────
function registerAppProtocol () {
  protocol.handle('app', (request) => {
    const url = new URL(request.url)
    const requested = decodeURIComponent(url.pathname)
    const root = path.resolve(FRONTEND_DIST)
    const resolved = path.resolve(path.join(root, requested))

    // Refus de toute traversée : `app://thot/../../etc/passwd` doit échouer.
    if (resolved !== root && !resolved.startsWith(root + path.sep)) {
      return new Response('Interdit', { status: 403 })
    }

    const isNavigation = (request.headers.get('accept') || '').includes('text/html')
    if (resolved === root || !existsSync(resolved)) {
      // Repli SPA : `/` et les routes du routeur doivent tous rendre index.html.
      // Sans cela, React Router ne trouve pas la route `/` sur `app://thot/`
      // et l'outlet reste vide.
      if (isNavigation || resolved === root) {
        return net.fetch(pathToFileUrl(path.join(root, 'index.html')).toString())
      }
      return new Response('Introuvable', { status: 404 })
    }

    return net.fetch(pathToFileUrl(resolved).toString())
  })
}

function pathToFileUrl (p) {
  return require('node:url').pathToFileURL(p)
}

// ─── IPC : surface minimale et vérifiée ─────────────────────────────────────
if (ipcMain && typeof ipcMain.handle === 'function') ipcMain.handle('thot:paths', () => {
  if (!sidecar || !sidecar.paths) return null
  return sidecar.paths
})

// Dispatch : chaque commande est validée contre le registre, puis routée. Le
// renderer ne peut pas atteindre un canal absent du registre même en forgeant
// l'appel, car le preload ne lui expose pas de verbe correspondant.

if (ipcMain && typeof ipcMain.handle === 'function') ipcMain.handle('thot:cmd', async (_event, command, args) => {
  if (!ALLOWED.has(command)) {
    throw new Error(`Commande refusée : ${command}`)
  }
  if (command === 'image_stage_work') {
    return stageWorkImage(args)
  }
  const method = SIDECAR_METHODS[command]
  if (!method) {
    throw new Error(`Commande sans routage : ${command}`)
  }
    // Le protocole exige un request_id (pattern [A-Za-z0-9_-]{1,128}) et s'en sert
    // pour coreler la reponse a la demande. Or les wrappers du frontend n'en
    // fournissent pas pour les commandes de base : leur reponse arrivait sans
    // passer par le protocole. On en fabrique donc un ici plutot que de
    // contraindre chaque page a en envoyer un.
  const requestId = args?.requestId || nextRequestId()
  const params = sidecarParams(command, args)

  // Les octets ne passent JAMAIS par le protocole : Python résout le chemin,
  // le processus principal lit le fichier. Sérialiser 2 Mo en JSON `number[]`
  // sur stdout serait un mur de performance.
  if (command === 'get_attachment_bytes') {
    return readAttachmentBytes(requestId, args)
  }

  log(`[ipc] ${command} -> ${method} (rid=${requestId})`)
  try {
    await sidecar.write(requestId, method, params)
    if (FIRE_AND_FORGET.has(command)) return null
    const payload = await sidecar.awaitReply(requestId, replyEventFor(method), method)
    return unwrap(command, payload)
  } catch (error) {
    // Le frontend avale parfois l'echec (etat vide) : sans ce log, une commande
    // cassee est invisible.
    log(`[ipc] ${command} a echoue: ${error && error.message ? error.message : error}`)
    throw error
  }
})

let requestCounter = 0

/** Identifiant de correlation fabrique quand le frontend n'en fournit pas. */
function nextRequestId () {
  requestCounter += 1
  return `ipc_${Date.now().toString(36)}_${requestCounter}`
}

// 20 Mio : la règle d'entrée utilisateur (backend INPUT_LIMITS.upload_bytes).
// L'aperçu ne doit pas être plus permissif que le dépôt qu'il prévisualise.
const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
// Exception documentée (INPUT_LIMITS.image_studio_bytes) : les images de travail
// sont des PNG produits par l'éditeur, pas des entrées brutes.
const MAX_WORK_IMAGE_BYTES = 32 * 1024 * 1024
const MAX_WORK_IMAGE_B64_CHARS = 4 * Math.ceil(MAX_WORK_IMAGE_BYTES / 3)

/**
 * Résout l'artefact via Python, puis lit le fichier.
 * Le chemin est confiné sous le répertoire de données : un `relative_path`
 * contenant `..`, une barre oblique inverse ou un préfixe de lecteur Windows
 * est refusé avant tout accès disque.
 */
async function readAttachmentBytes (requestId, args) {
  await sidecar.write(requestId, 'get.attachment_bytes', { artifactId: args?.artifactId })
  const { relative_path: relativePath, mime } =
    await sidecar.awaitReply(requestId, 'get_attachment_bytes', 'get.attachment_bytes')

  if (typeof relativePath !== 'string' || relativePath === '') {
    throw new Error('Chemin de pièce jointe refusé')
  }
  if (path.isAbsolute(relativePath) || /\0|\\/.test(relativePath) || /^[A-Za-z]:/.test(relativePath)) {
    throw new Error('Chemin de pièce jointe refusé')
  }

  const root = path.resolve(sidecar.paths.dataDir)
  const target = path.resolve(path.join(root, relativePath))
  if (target !== root && !target.startsWith(root + path.sep)) {
    throw new Error('Chemin de pièce jointe refusé')
  }

  const stats = statSync(target)
  if (!stats.isFile()) throw new Error('Fichier joint introuvable sur disque')
  if (stats.size > MAX_ATTACHMENT_BYTES) {
    throw new Error('Fichier trop volumineux pour la vignette (max 20 Mo)')
  }
  return [readFileSync(target), mime || 'application/octet-stream']
}

/** Ne renvoie au renderer que ce que le wrapper TypeScript attend. */
function unwrap (command, payload) {
  if (!payload || typeof payload !== 'object') return payload
  const key = RESULT_KEY[command]
  if (key) return payload[key] ?? null
  const { request_id: _ignored, ...rest } = payload
  return rest
}

/** Normalise une représentation arbitraire d'octets en `Buffer`. */
function normalizeWorkImageBytes (raw) {
  if (raw === null || raw === undefined) {
    throw new Error('Données d\'image manquantes')
  }

  // Chaîne base64, éventuellement préfixée par `data:*;base64,`.
  if (typeof raw === 'string') {
    let payload = raw.trim()
    const dataMatch = payload.match(/^data:[^,]*;base64,(.*)$/is)
    if (dataMatch) payload = dataMatch[1]
    payload = payload.replace(/\s+/g, '')
    if (payload === '') {
      throw new Error('Données d\'image manquantes')
    }
    // La chaîne est bornée AVANT `Buffer.from` : décoder d'abord puis mesurer
    // allouait un buffer de plusieurs centaines de Mo pour un payload refusé.
    if (payload.length > MAX_WORK_IMAGE_B64_CHARS) {
      throw new Error('Taille d\'image invalide (max 32 Mo)')
    }
    let buffer = null
    try {
      buffer = Buffer.from(payload, 'base64')
    } catch (error) {
      throw new Error('Données d\'image invalides')
    }
    if (!buffer || buffer.length === 0) {
      throw new Error('Données d\'image invalides')
    }
    if (buffer.length > MAX_WORK_IMAGE_BYTES) {
      throw new Error('Taille d\'image invalide (max 32 Mo)')
    }
    return buffer
  }

  if (Buffer.isBuffer(raw)) {
    return raw
  }

  if (raw instanceof ArrayBuffer) {
    return Buffer.from(raw)
  }

  if (ArrayBuffer.isView(raw)) {
    return Buffer.from(raw.buffer, raw.byteOffset, raw.byteLength)
  }

  if (Array.isArray(raw)) {
    return Buffer.from(raw)
  }

  // Objet indexé par position (`{0: b, 1: b, ..., length: n}`) ou array-like.
  if (typeof raw === 'object' && typeof raw.length === 'number'
      && Number.isFinite(raw.length) && raw.length >= 0) {
    let arrayLike = null
    try {
      arrayLike = Array.from(raw)
    } catch (error) {
      arrayLike = null
    }
    if (Array.isArray(arrayLike)) {
      return Buffer.from(arrayLike)
    }
  }

  throw new Error('Données d\'image manquantes')
}

function stageWorkImage (args) {
  const MIN_BYTES = 1

  const hasBytesProp = !!args && typeof args === 'object'
    && Object.prototype.hasOwnProperty.call(args, 'bytes')
  const rawBytes = hasBytesProp ? args.bytes : args
  const mime = hasBytesProp ? (args.mime || args.MIME || 'image/png') : 'image/png'

  if (mime !== 'image/png') {
    throw new Error('Type MIME refusé : seul image/png est accepté')
  }

  const bytes = normalizeWorkImageBytes(rawBytes)
  // La chaîne base64 est bornée AVANT `Buffer.from` dans `normalizeWorkImageBytes`.
  // Ce second contrôle couvre les autres formes (Buffer, ArrayBuffer, tableau).
  if (bytes.length < MIN_BYTES || bytes.length > MAX_WORK_IMAGE_BYTES) {
    throw new Error('Taille d\'image invalide (max 32 Mo)')
  }

  const PNG_SIG = Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
  const directPng = bytes.length >= 8 && Buffer.from(bytes).subarray(0, 8).equals(PNG_SIG)

  let finalBytes = bytes
  if (!directPng) {
    let converted = null
    try {
      const electronModule = require('electron')
      const nativeImage = electronModule && electronModule.nativeImage
      if (nativeImage && typeof nativeImage.createFromBuffer === 'function') {
        const image = nativeImage.createFromBuffer(Buffer.from(bytes))
        if (!image.isEmpty()) {
          const png = image.toPNG()
          if (png && png.length >= MIN_BYTES && png.length <= MAX_WORK_IMAGE_BYTES) {
            converted = png
          }
        }
      }
    } catch (error) {
      converted = null
    }

    if (converted) {
      finalBytes = Buffer.from(converted)
      // la conversion doit réellement produire un PNG
      if (finalBytes.length < 8 || !finalBytes.subarray(0, 8).equals(PNG_SIG)) {
        converted = null
      }
    }

    if (!converted) {
      const prefix = Buffer.from(bytes).subarray(0, 8)
      const hex = prefix.toString('hex')
      const decimals = Array.from(prefix).join(', ')
      throw new Error(`Signature PNG invalide (premiers octets hex=${hex}, bytes=[${decimals}])`)
    }
  }

  if (finalBytes.length < MIN_BYTES || finalBytes.length > MAX_WORK_IMAGE_BYTES) {
    throw new Error('Taille d\'image invalide (max 32 Mo)')
  }

  if (!sidecar || !sidecar.paths || !sidecar.paths.tempDir) {
    throw new Error('Staging non disponible')
  }
  const root = path.resolve(sidecar.paths.tempDir, 'image-studio')
  const tempRoot = path.resolve(sidecar.paths.tempDir)
  const tempStat = lstatSync(tempRoot)
  if (tempStat.isSymbolicLink() || !tempStat.isDirectory()) {
    throw new Error('Chemin de staging invalide')
  }
  try {
    lstatSync(root)
  } catch (e) {
    // not exists
  }
  if (existsSync(root)) {
    const st = lstatSync(root)
    if (st.isSymbolicLink() || !st.isDirectory()) {
      throw new Error('Racine de staging invalide')
    }
  } else {
    mkdirSync(root, { recursive: true, mode: 0o700 })
  }
  // Ensure mode 0700
  try { (require('node:fs')).chmodSync(root, 0o700) } catch (e) {}
  const token = crypto.randomBytes(16).toString('hex')
  const target = path.join(root, token + '.png')
  if (existsSync(target)) {
    throw new Error('Collision de token')
  }
  // atomic write
  const tmp = target + '.tmp'
  if (existsSync(tmp)) { try { unlinkSync(tmp) } catch (e) {} }
  writeFileSync(tmp, finalBytes, { mode: 0o600 })
  try {
    unlinkSync(target) // no-op if absent
  } catch (e) {}
  renameSync(tmp, target)
  try { (require('node:fs')).chmodSync(target, 0o600) } catch (e) {}
  return { token }
}

/** Mapping pur des arguments camelCase/snake_case de `image_generate`. */
function mapImageGenerateArgs (args) {
  return {
    prompt: args?.prompt ?? '',
    aspect_ratio: args?.aspectRatio ?? '1:1',
    reference_ids: args?.referenceIds ?? [],
    seed: args?.seed ?? null,
    idempotency_key: args?.idempotencyKey ?? null,
    scope: 'library',
    mode: args?.mode ?? null,
    source_artifact_id: args?.sourceArtifactId ?? args?.source_artifact_id ?? null,
    work_token: args?.workToken ?? args?.work_token ?? null,
    mask_token: args?.maskToken ?? args?.mask_token ?? null,
    reference_tokens: args?.referenceTokens ?? args?.reference_tokens ?? null,
    work_sha256: args?.workSha256 ?? args?.work_sha256 ?? null,
    mask_sha256: args?.maskSha256 ?? args?.mask_sha256 ?? null,
    reference_sha256: args?.referenceSha256 ?? args?.reference_sha256 ?? null,
    canvas_width: args?.canvasWidth ?? args?.canvas_width ?? null,
    canvas_height: args?.canvasHeight ?? args?.canvas_height ?? null,
    strength: args?.strength ?? null,
    session_id: args?.sessionId ?? args?.session_id ?? null,
    revision: args?.revision ?? null,
    mask_kind: args?.maskKind ?? args?.mask_kind ?? null,
    source_transform: args?.sourceTransform ?? args?.source_transform ?? null,
    source_size: args?.sourceSize ?? args?.source_size ?? null
  }
}

/** Traduit les arguments camelCase du frontend vers les clés attendues par Python. */
function sidecarParams (command, args) {
  switch (command) {
case 'chat_send':
        return {
          message: args?.message ?? '',
          image_paths: args?.imagePaths ?? null,
          attachment_ids: args?.attachmentIds ?? null,
          conv_id: args?.convId ?? args?.requestId,
          workspace_id: args?.workspaceId ?? null
        }
    case 'cancel_run':
      return { request_id: args?.requestId }
    case 'image_generate':
      return mapImageGenerateArgs(args)
    case 'image_status':
      return {}
    default:
      return args ?? {}
  }
}

/** Renvoie les arguments camelCase tels quels pour les méthodes base Python. */
function toBackendParams (args) {
  return args ?? {}
}

if (ipcMain && typeof ipcMain.handle === 'function') ipcMain.handle('thot:send', async (_event, command, payload) => {
  const ALLOWED = new Set(['chat.send', 'cancel', 'image.generate', 'image.status'])
  if (!ALLOWED.has(command)) {
    throw new Error(`Commande refusée : ${command}`)
  }
  await sidecar.write(payload.id, command, payload.params)
})

// ─── Cycle de vie ───────────────────────────────────────────────────────────
if (app && typeof app.whenReady === 'function') app.whenReady().then(async () => {
  registerAppProtocol()

  sidecar = new Sidecar({
    projectRoot: PROJECT_ROOT,
    resourcesDir: process.resourcesPath,
    // Le mode est porté par `app.isPackaged` ET par `THOT_PACKAGED`, parce que les
    // deux disent la même chose par des chemins différents : le premier par la
    // construction Electron, le second par l'intention de celui qui lance
    // (`npm start` sur un dossier gelé doit rester un test possible). Tant que le
    // conditionnement n'existe pas, `isPackaged` vaut toujours faux — donc dev.
    packaged: app.isPackaged || process.env.THOT_PACKAGED === '1',
    onEvent: broadcast,
    onLog: log
  })

  createWindow()
  sidecar.start()

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

// Le renderer s'abonne : on lui rejoue ce qui a été émis avant son montage.
if (ipcMain && typeof ipcMain.on === 'function') ipcMain.on('thot:renderer-ready', () => {
  if (rendererSubscribed) return
  rendererSubscribed = true
  for (const { name, payload } of bufferedEvents) {
    mainWindow?.webContents.send('thot:event', name, payload)
  }
  bufferedEvents.length = 0
})

// Tuer le sidecar est explicite : sans cela Python survit à la fermeture et
// devient un orphelin adopté par systemd --user.
let shuttingDown = false
if (app && typeof app.on === 'function') app.on('before-quit', (event) => {
  if (shuttingDown || !sidecar) return
  shuttingDown = true
  event.preventDefault()
  sidecar.stop().finally(() => app.exit(0))
})

if (app && typeof app.on === 'function') app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

if (typeof module !== 'undefined' && module.exports) {
  module.exports.stageWorkImage = stageWorkImage
  module.exports.normalizeWorkImageBytes = normalizeWorkImageBytes
  module.exports.mapImageGenerateArgs = mapImageGenerateArgs
}
