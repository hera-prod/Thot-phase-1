'use strict'

// Où vivent la base, les blobs, le staging — et QUEL EXÉCUTABLE lance le backend.
//
// Le mode portable est PRIORITAIRE sur le dossier utilisateur du système : si
// `portable.json` existe à la racine du dépôt, tout reste sous `<base>/data`.
// C'est ce qui rend l'atelier transportable sur une clé USB — une app « tout
// local » qui écrit dans `~/.local/share` n'est pas vraiment locale.
//
// Deux conséquences, et elles sont liées : ce fichier ne consulte JAMAIS
// `process.cwd()` pour décider d'un emplacement, et le lanceur du sidecar est
// choisi par la même règle que les chemins. Une application portable dont le
// backend part du PATH est une application qui a besoin d'une machine précise.

const fs = require('node:fs')
const path = require('node:path')

const PORTABLE_MARKER = 'portable.json'
const MANIFESTE = 'distribution.json'
const INTERPRETEURS_INTERDITS = new Set(['python', 'python3', 'pythonw', 'py'])

/** Remonte jusqu'à 3 niveaux pour trouver la racine portable. */
function findPortableRoot (start) {
  let current = start
  for (let depth = 0; depth < 3; depth += 1) {
    if (fs.existsSync(path.join(current, PORTABLE_MARKER))) return current
    const parent = path.dirname(current)
    if (parent === current) break
    current = parent
  }
  return null
}

/**
 * Racine "externe" : là où vivent les données et le sidecar à côté de l'exécutable
 * livré. En mode AppImage, l'application est montée en lecture seule dans
 * `/tmp/.mount_*` : `__dirname`, `process.resourcesPath` et tout le reste pointent
 * donc dans ce montage, inaccessible en écriture et éphémère. Seul le dossier
 * contenant le fichier `.AppImage` est à la fois inscriptible et transportable
 * (clé USB). `APPIMAGE` est la variable qu'Electron/AppImage pose automatiquement
 * ; en développement, on retombe sur la racine du projet.
 */
function resolveExternalRoot ({ resourcesDir, projectRoot, env = process.env }) {
  if (env && typeof env.APPIMAGE === 'string' && env.APPIMAGE.trim() !== '') {
    return path.dirname(path.resolve(env.APPIMAGE))
  }
  return projectRoot
}

function resolveAppDataDir ({ resourcesDir, baseDir }) {
  if (baseDir) {
    const portable = findPortableRoot(baseDir)
    if (portable) return portable
  }
  if (resourcesDir) {
    const portable = findPortableRoot(path.dirname(resourcesDir))
    if (portable) return portable
    return path.dirname(resourcesDir)
  }
  return baseDir || process.cwd()
}

function resolveDataDir (appDataDir) {
  const dataDir = path.join(appDataDir, 'data')
  for (const name of ['database', 'blobs', 'logs', 'temp', 'backups']) {
    fs.mkdirSync(path.join(dataDir, name), { recursive: true })
  }
  return dataDir
}

function resolveDbPath (dataDir) {
  const preferred = path.join(dataDir, 'database', 'thot-phase-1.db')
  if (fs.existsSync(preferred)) return preferred
  const legacy = path.join(dataDir, 'thot-phase-1.db')
  if (fs.existsSync(legacy)) return legacy
  return preferred
}

/**
 * En mode portable le staging vit sous `<data>/temp`. Sinon, hors installation,
 * on retombe sur le temp système comme le faisait Rust.
 */
function resolveTempDir (appDataDir, dataDir) {
  if (fs.existsSync(path.join(appDataDir, PORTABLE_MARKER))) {
    return path.join(dataDir, 'temp')
  }
  return path.join(require('node:os').tmpdir(), 'thot')
}

// ─── Le lanceur du sidecar ───────────────────────────────────────────────────
//
// `distribution.json` déclare `"sidecar": "thot-sidecar-…"` : un binaire Python
// GELÉ, avec son interpréteur dedans. Il était déjà écrit, jamais lu : le
// lanceur faisait `spawn('python3')`, donc une machine sans Python 3 ne pouvait
// pas démarrer l'application, alors que le manifeste affirmait l'inverse.
//
// DEUX MODES, ET AUCUN TROISIÈME :
//   * DÉVELOPPEMENT — un checkout source. `python3` du PATH (ou `THOT_DEV_PYTHON`
//     si l'opérateur a un interpréteur ailleurs), `-m backend.main`, `cwd` racine.
//     C'est le mode qui sert aujourd'hui, il doit continuer de servir.
//   * DÉPLOYÉ — le binaire que le manifeste déclare, trouvé dans les racines de
//     l'application. PAS de repli : si le binaire est absent, `resolveSidecarLaunch`
//     lève. Lancer la version de production avec l'interpréteur du PATH revient à
//     dire que l'application est portable ET qu'elle a besoin, en plus, d'une
//     Python — celle du poste qui l'a construite. C'est contradictoire, donc le
//     code le refuse.

/** Racines où le sidecar gelé peut se trouver, de la plus fiable à la moins. */
function sidecarSearchRoots ({ resourcesDir, appRoot, externalRoot }) {
  const roots = []
  // Racine externe (dossier du .AppImage) en premier : c'est là que le sidecar
  // placé à côté de l'exécutable doit être trouvé avant celui du montage en
  // lecture seule. `externalRoot` peut être fourni explicitement ; sinon déduit.
  const ext = externalRoot || resolveExternalRoot({ resourcesDir, projectRoot: appRoot })
  // Déduplication en conservant l'ordre (chemins résolus).
  const seen = new Set()
  const push = (value) => {
    if (!value) return
    const resolved = path.resolve(value)
    if (seen.has(resolved)) return
    seen.add(resolved)
    roots.push(resolved)
  }
  push(ext)
  // `process.resourcesPath` est le dossier `resources` d'Electron. Son parent est
  // le dossier de l'application installée — là où le sidecar est posé.
  if (resourcesDir) push(path.dirname(resourcesDir))
  if (appRoot) {
    push(appRoot)
    push(path.dirname(appRoot))
  }
  return roots
}

/** Le `distribution.json` le plus proche des racines, ou `null`. */
function readDistribution ({ resourcesDir, appRoot, externalRoot }) {
  for (const base of sidecarSearchRoots({ resourcesDir, appRoot, externalRoot })) {
    const candidat = path.join(base, MANIFESTE)
    if (fs.existsSync(candidat)) {
      try {
        return JSON.parse(fs.readFileSync(candidat, 'utf8'))
      } catch (error) {
        console.error(`[thot] manifeste ${MANIFESTE} illisible dans ${base} : ${error && error.message ? error.message : error}`)
        return null
      }
    }
  }
  return null
}

/** Cherche un exécutable dans le PATH. Développement seulement, par construction. */
function whichSync (command, env) {
  if (command.includes(path.sep)) {
    return fs.existsSync(command) ? command : null
  }
  const dirs = (env.PATH || '').split(path.delimiter).filter(Boolean)
  for (const dir of dirs) {
    const candidat = path.join(dir, command)
    try {
      fs.accessSync(candidat, fs.constants.X_OK)
      return candidat
    } catch {
      /* suivant */
    }
  }
  return null
}

/**
 * Choisit la commande qui lance le backend. Lève si elle est introuvable : le
 * retour `null` obligerait l'appelant à décider entre « refuser » et « essayer
 * autre chose », et il choisirait « autre chose ».
 *
 * Le contrôle final (`INTERPRETEURS_INTERDITS`) n'est pas décoratif : c'est
 * l'invariant rendu EXPLICITE. Si quelqu'un réintroduit un jour un `python3` dans
 * ce chemin, la construction échoue ici, au lieu de produire une installation qui
 * « marche » sur le poste du développeur et pas sur la clé USB.
 */
function resolveSidecarLaunch ({ packaged, projectRoot, resourcesDir, appRoot = projectRoot, externalRoot, env = process.env }) {
  if (!packaged) {
    const commande = env.THOT_DEV_PYTHON || 'python3'
    const resolu = whichSync(commande, env)
    if (!resolu) {
      throw new Error(
        `Interpréteur Python introuvable pour le mode développement (cherché : ${commande}).\n` +
        `  Ce mode suppose un checkout source avec Python 3 ; une installation livrée, elle, ` +
        `ne dépend d'aucun Python du système.\n` +
        `  Remède : installer Python 3, ou pointer THOT_DEV_PYTHON sur l'interpréteur voulu.`
      )
    }
    return {
      command: resolu,
      args: ['-m', 'backend.main'],
      cwd: projectRoot,
      origine: 'développement : interpréteur du poste'
    }
  }

  const manifeste = readDistribution({ resourcesDir, appRoot, externalRoot })
  const declare = manifeste && manifeste.sidecar
  if (typeof declare !== 'string' || declare.trim() === '') {
    throw new Error(
      `${MANIFESTE} ne déclare pas de sidecar gelé. Une installation ne peut pas démarrer ` +
      `sans le binaire qu'elle déclare : lui emprunter le python3 du PATH ferait dépendre ` +
      `l'application d'une machine précise, ce qu'est exactement ce qu'un conditionnement ` +
      `portable exclut.`
    )
  }
  if (path.isAbsolute(declare)) {
    throw new Error(
      `${MANIFESTE} déclare un sidecar absolu (${declare}) : un manifeste ne peut pas nommer ` +
      `un chemin d'une autre machine.`
    )
  }
  // Refus de toute traversée : un segment '..' ferait sortir du dossier autorisé
  // (ex. '../x' ou 'a/../b'), et path.join(base, declare) échapperait alors au
  // confinement voulu. On refuse le manifeste au lieu de nettoyer à la main.
  const segments = declare.split(/[\\/]+/)
  if (segments.includes('..')) {
    throw new Error(
      `${MANIFESTE} déclare un sidecar hors dossier autorisé (${declare}) : la traversée de ` +
      `répertoire (« .. ») est interdite.`
    )
  }
  const examines = []
  for (const base of sidecarSearchRoots({ resourcesDir, appRoot, externalRoot })) {
    const candidat = path.join(base, declare)
    examines.push(candidat)
    if (fs.existsSync(candidat)) {
      const nom = path.basename(candidat).toLowerCase()
      if (INTERPRETEURS_INTERDITS.has(nom.replace(/\.exe$/, ''))) {
        throw new Error(
          `refus de lancer ${candidat} : un interpréteur Python du PATH n'est pas le ` +
          `sidecar livré. L'invariant « production ≠ python3 » est ici.`
        )
      }
      return {
        command: candidat,
        args: [],
        cwd: path.dirname(candidat),
        origine: `installé : ${declare}`
      }
    }
  }
  throw new Error(
    `Sidecar gelé introuvable (${declare}).\n` +
    `  Emplacements examinés : ${examines.join(', ')}\n` +
    `  Aucun repli sur python3 : en production, l'application est son propre runtime.`
  )
}

module.exports = {
  PORTABLE_MARKER,
  MANIFESTE,
  findPortableRoot,
  readDistribution,
  resolveAppDataDir,
  resolveDataDir,
  resolveDbPath,
  resolveExternalRoot,
  resolveSidecarLaunch,
  resolveTempDir,
  sidecarSearchRoots,
  whichSync
}
