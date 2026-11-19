'use strict'

// Cycle de vie du sidecar Python.
//
// L'écriture sur stdin est sérialisée par une file d'attente : le pipe est
// partagé et deux lignes JSON qui se chevaucheraient côté Python corrompraient le
// flux. Une file d'attente ABSORBE l'attente là où un verrou à refus la
// transformerait en échec pour l'utilisateur.
//
// Le processus est tué explicitement à l'arrêt. Sans cela Python survit à la
// fermeture de la fenêtre, adopté par systemd, et bloque le lancement suivant :
// c'est l'origine des processus fantômes et du port 1420 déjà pris.

const path = require('node:path')
const fs = require('node:fs')
const { spawn } = require('node:child_process')
const { StdoutFramer } = require('./stdout-framer.cjs')
const {
  resolveAppDataDir, resolveDataDir, resolveDbPath, resolveExternalRoot, resolveSidecarLaunch, resolveTempDir
} = require('./paths.cjs')

class Sidecar {
  constructor ({ projectRoot, resourcesDir, packaged, onEvent, onLog }) {
    this.projectRoot = projectRoot
    this.resourcesDir = resourcesDir
    // `packaged` décide si l'on part du sidecar gelé ou de l'interpréteur du poste.
    // Il est faux par défaut : un appelant qui l'oublie reste en développement, et
    // `resolveSidecarLaunch` lève bruyamment s'il pretend l'inverse.
    this.packaged = packaged === true
    this.onEvent = onEvent
    this.onLog = onLog || (() => {})
    this.child = null
    this.framer = new StdoutFramer()
    this.terminated = false
    this._writeChain = Promise.resolve()
    this._waiters = new Set()
  }

  /**
   * Attend l'événement de réponse correspondant à une requête.
   * `request_id` est la clé de corrélation : le protocole renvoie la réponse
   * dans un événement portant le même identifiant, jamais dans la valeur de
   * l'appel. Sans cela, toute commande base serait un fire-and-forget.
   */
  awaitReply (requestId, eventName, method, timeoutMs = 60000) {
    return new Promise((resolve, reject) => {
      if (this.terminated) {
        reject(new Error('Sidecar Python non disponible — redémarrez l\'application'))
        return
      }
      const waiter = (name, payload) => {
        if (!requestId || payload?.request_id !== requestId) return
        if (name === eventName) {
          cleanup(); resolve(payload)
        } else if (name === 'error') {
          cleanup(); reject(new Error(payload?.message || 'Erreur du backend'))
        } else if (name === 'backend_terminated') {
          cleanup(); reject(new Error('Sidecar arrêté'))
        }
      }
      const timer = setTimeout(() => {
        cleanup()
        reject(new Error(`Le backend n'a pas répondu à ${method}`))
      }, timeoutMs)
      if (typeof timer.unref === 'function') timer.unref()
      const cleanup = () => {
        clearTimeout(timer)
        this._waiters.delete(waiter)
      }
      this._waiters.add(waiter)
    })
  }

  env () {
    // Racine externe : à côté du .AppImage en mode AppImage, racine projet sinon.
    // On l'utilise pour la base des données : `portable.json` côté montage étant
    // injoignable en écriture, c'est la seule racine transportable.
    const externalRoot = resolveExternalRoot({ resourcesDir: this.resourcesDir, projectRoot: this.projectRoot })
    this.externalRoot = externalRoot
    const appDataDir = resolveAppDataDir({ baseDir: externalRoot })
    const dataDir = resolveDataDir(appDataDir)
    const dbPath = resolveDbPath(dataDir)
    const devFlux = path.join(this.projectRoot, 'models', 'flux2-klein')
    const fluxModelsDir = fs_existsDir(devFlux)
      ? devFlux
      : path.join(appDataDir, 'models', 'flux2-klein')

    this.paths = { appDataDir, dataDir, dbPath, tempDir: resolveTempDir(appDataDir, dataDir) }

    // Les bibliothèques CUDA embarquées par Ollama ne sont pas sur le chemin de
    // recherche par défaut : on les ajoute au LD_LIBRARY_PATH transmis au sidecar
    // Python (qui charge llama.cpp avec CUDA). On conserve le chemin existant
    // tel quel et on n'ajoute que les dossiers présents, dans un ordre fixe, pour
    // que le résultat soit reproductible.
    const candidateCudaDirs = [
      '/usr/local/lib/ollama/cuda_v12',
      '/usr/local/lib/ollama/cuda_v13'
    ]
    const existingLibraryPath = process.env.LD_LIBRARY_PATH
    const libraryPathParts = []
    if (existingLibraryPath) {
      libraryPathParts.push(existingLibraryPath)
    }
    for (const candidateDir of candidateCudaDirs) {
      if (fs.existsSync(candidateDir)) {
        libraryPathParts.push(candidateDir)
      }
    }

    const environment = {
      THOT_DB_PATH: dbPath,
      THOT_APP_DATA_DIR: appDataDir,
      THOT_DATA_DIR: dataDir,
      THOT_FLUX_MODELS_DIR: fluxModelsDir,
      THOT_TOOL_RUNTIME: 'llama_cpp',
      // Python doit le savoir, pas seulement le lanceur : `is_packaged()` ferme le
      // PATH de la résolution des exécutables côté backend. Sans cette
      // propagation, une installation déployée continuerait d'emprunter les outils
      // du poste depuis le seul endroit où on ne regarde plus.
      THOT_PACKAGED: this.packaged ? '1' : '0'
    }
    if (libraryPathParts.length > 0) {
      environment.LD_LIBRARY_PATH = libraryPathParts.join(path.delimiter)
    }
    return environment
  }

  start () {
    const env = this.env()
    // Le choix de l'exécutable est une DÉCISION, pas un défaut : elle est prise
    // par `paths.cjs` et peut échouer. Si elle échoue ici, on ne lance rien du tout
    // plutôt qu'un backend dans une configuration qui n'est pas celle du produit.
    const externalRoot = this.externalRoot
      || resolveExternalRoot({ resourcesDir: this.resourcesDir, projectRoot: this.projectRoot })
    const lancement = resolveSidecarLaunch({
      packaged: this.packaged,
      projectRoot: this.projectRoot,
      resourcesDir: this.resourcesDir,
      appRoot: this.projectRoot,
      externalRoot
    })
    this.onLog(`[thot] data dir: ${env.THOT_DATA_DIR}`)
    this.onLog(`[thot] db path: ${env.THOT_DB_PATH}`)
    this.onLog(`[thot] sidecar: ${lancement.command} (${lancement.origine})`)

    this.child = spawn(lancement.command, lancement.args, {
      cwd: lancement.cwd,
      env: { ...process.env, ...env },
      stdio: ['pipe', 'pipe', 'pipe']
    })

    this.child.stdout.on('data', (chunk) => {
      for (const value of this.framer.push(chunk)) {
        // Pass-through strict : seul `event` et `payload` sont transmis, comme
        // en Rust. Le frontend compte sur la casse snake_case de `payload`.
        const name = value.event
        const payload = value.payload ?? null
        this.onEvent(name, payload)
        for (const waiter of this._waiters) waiter(name, payload)
      }
    })

    this.child.stderr.on('data', (chunk) => {
      this.onLog(`[python] ${String(chunk).trimEnd()}`)
    })

    this.child.on('error', (error) => {
      this.onLog(`[thot] échec du sidecar: ${error.message}`)
      this._emitTerminated(null, null, error.message)
    })

    this.child.on('close', (code, signal) => {
      this.framer.finish()
      if (this.framer.dropped > 0) {
        this.onLog(`[thot] lignes stdout ignorées: ${this.framer.dropped}`)
      }
      this._emitTerminated(code, signal)
    })
  }

  _emitTerminated (code, signal, reason) {
    if (this.terminated) return
    this.terminated = true
    let message
    if (reason) message = `Sidecar interrompu: ${reason}`
    else if (signal) message = `Sidecar arrêté par le signal ${signal}`
    else if (code === 0) message = 'Sidecar terminé'
    else message = `Sidecar terminé avec le code ${code}`
    this.onEvent('backend_terminated', { code: code ?? null, message, retryable: false })
  }

  /**
   * Sérialise les écritures : le pipe est partagé avec un seul writer à la
   * fois, sinon deux lignes JSON se chevaucheraient côté Python.
   */
  write (id, method, params) {
    if (!this.child || this.terminated) {
      return Promise.reject(new Error('Sidecar Python non disponible — redémarrez l\'application'))
    }
    const line = JSON.stringify({ id, method, params }) + '\n'
    this._writeChain = this._writeChain.then(
      () => new Promise((resolve, reject) => {
        this.child.stdin.write(line, (error) => (error ? reject(error) : resolve()))
      })
    )
    return this._writeChain
  }

  /** Ferme stdin (déclenche l'EOF qui arrête proprement Python) puis tue. */
  async stop () {
    if (!this.child || this.terminated) return
    const child = this.child
    try { child.stdin.end() } catch { /* déjà fermé */ }
    const exited = await Promise.race([
      new Promise((resolve) => child.once('close', () => resolve(true))),
      new Promise((resolve) => setTimeout(() => resolve(false), 5000))
    ])
    if (!exited) {
      this.onLog('[thot] sidecar muet, terminaison forcée')
      try { child.kill('SIGTERM') } catch { /* déjà parti */ }
    }
  }
}

function fs_existsDir (p) {
  try {
    return require('node:fs').statSync(p).isDirectory()
  } catch {
    return false
  }
}

module.exports = { Sidecar }
