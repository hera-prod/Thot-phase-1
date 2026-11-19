#!/usr/bin/env node
// Lanceur unique de l'application.
//
// Un seul point d'entrée, deux modes, et une garantie : quand Electron se
// ferme, tout ce qu'il a démarré se ferme avec lui.
//
//   npm start        bundle local (app://), aucun port, aucun Vite
//   npm run dev      Vite + rechargement a chaud, toujours via Electron
//
// Avant, `npm run dev` demarrait Vite seul et `npm start` supposait Vite deja
// lance : deux commandes, deux etats possibles, et l'ouverture directe de
// http://127.0.0.1:1420 dans un navigateur produisait un pont absent.

import { spawn, spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import electron from 'electron'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const DEV_URL = 'http://127.0.0.1:1420'
const useDevServer = process.argv.includes('--dev')
const useFrozenSidecar = process.env.THOT_PACKAGED === '1'
const npmCli = process.env.npm_execpath
if (!npmCli) throw new Error('Utilisez npm start ou npm run dev pour lancer Thot.')

// Electron ^44.5.0 verrouille Node >= 22.12.0. Fail fast sur une version
// incompatible avant toute tentative de lancement d'Electron.
const MIN_NODE = [22, 12]
const [nodeMajor, nodeMinor] = process.versions.node.split('.').map(Number)
if (
  nodeMajor < MIN_NODE[0] ||
  (nodeMajor === MIN_NODE[0] && nodeMinor < MIN_NODE[1])
) {
  throw new Error(
    `Node.js ${MIN_NODE.join('.')}+ est requis par Electron (version actuelle : ${process.versions.node}).`
  )
}

function log (line) {
  process.stdout.write(`[lanceur] ${line}\n`)
}

function run (command, args, options = {}) {
  // stdin en «ignore» : aucun enfant ne retient la TTY du terminal, donc la
  // fermeture de la fenêtre ou du shell ne déclenche plus d'EIO chez un
  // processus fantôme qui essaye encore de lire stdin.
  return spawn(command, args, {
    cwd: ROOT,
    stdio: ['ignore', 'inherit', 'inherit'],
    ...options
  })
}

function sidecarEnvironment (environment) {
  const isWindows = process.platform === 'win32'
  const localPython = isWindows
    ? join(ROOT, '.venv', 'Scripts', 'python.exe')
    : join(ROOT, '.venv', 'bin', 'python')
  const pathPython = isWindows ? 'python.exe' : 'python3'
  const python = environment.THOT_DEV_PYTHON || (existsSync(localPython) ? localPython : pathPython)
  const env = { ...environment, THOT_DEV_PYTHON: python }
  // Une extension native ne doit pas venir du site-packages d'un autre Python.
  for (const key of Object.keys(env)) {
    if (/^PYTHON(PATH|HOME)$/i.test(key)) delete env[key]
  }
  return env
}

function checkSidecarPython (env) {
  const python = env.THOT_DEV_PYTHON
  const probe = [
    'import sys',
    'if sys.version_info < (3, 11): raise SystemExit("Python >= 3.11 est requis")',
    'import sqlalchemy'
  ].join('\n')
  const result = spawnSync(python, ['-c', probe], { cwd: ROOT, env, stdio: 'inherit' })
  if (result.status === 0) return
  const detail = result.error ? ` (${result.error.message})` : ''
  throw new Error(`Python du sidecar inutilisable : ${python}${detail}. ` +
    'Installez requirements.txt avec cet interpréteur (-m pip install -r requirements.txt), ' +
    'ou désignez un environnement Python prêt via THOT_DEV_PYTHON.')
}

const children = new Set()
let shuttingDown = false

function runToCompletion (command, args) {
  const child = run(command, args)
  children.add(child)
  return new Promise((resolve, reject) => {
    child.once('error', reject)
    child.once('exit', (code) => resolve(code ?? 1))
  }).finally(() => children.delete(child))
}

function track (child) {
  children.add(child)
  child.once('error', (error) => {
    children.delete(child)
    log(`impossible de démarrer ${child.spawnfile} : ${error.message}`)
    shutdown(1)
  })
  child.once('exit', (code, signal) => {
    children.delete(child)
    // Le premier qui sort entraîne les autres : pas de processus fantome.
    if (!shuttingDown) shutdown(code ?? (signal ? 1 : 0))
  })
  return child
}

function shutdown (code) {
  if (shuttingDown) return
  shuttingDown = true
  process.exitCode = code
  for (const child of children) {
    try { child.kill('SIGTERM') } catch { /* deja parti */ }
  }
  // Laisse 3 s au shutdown propre du sidecar avant de relancer le processus parent.
  setTimeout(() => process.exit(code), 3000).unref()
}

process.on('SIGINT', () => shutdown(0))
process.on('SIGTERM', () => shutdown(0))

async function waitForDevServer (timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs
  while (!shuttingDown && Date.now() < deadline) {
    try {
      const response = await fetch(DEV_URL, { signal: AbortSignal.timeout(2000) })
      if (response.ok) return true
    } catch { /* pas encore pret */ }
    await new Promise((r) => setTimeout(r, 400))
  }
  return false
}

const electronEnv = useFrozenSidecar ? process.env : sidecarEnvironment(process.env)
if (!useFrozenSidecar) checkSidecarPython(electronEnv)

if (useDevServer) {
  log('mode developpement : Vite + Electron')
  const preflightCode = await runToCompletion(process.execPath, ['scripts/dev-preflight.mjs'])
  if (preflightCode !== 0) {
    log('le preflight a echoue')
    process.exit(process.exitCode ?? preflightCode)
  }

  if (!shuttingDown) track(run(process.execPath, [npmCli, '--prefix', 'frontend', 'run', 'dev']))

  if (!(await waitForDevServer())) {
    log('le serveur Vite ne repond pas, abandon')
    shutdown(1)
  } else if (!shuttingDown) {
    log('Vite pret, demarrage d\'Electron')
    track(run(electron, ['.'], { env: { ...electronEnv, THOT_DEV_SERVER: DEV_URL } }))
  }
} else {
  const dist = join(ROOT, 'frontend', 'dist', 'index.html')
  if (!existsSync(dist)) {
    log('bundle absent, construction...')
    const code = await runToCompletion(process.execPath, [npmCli, 'run', 'build'])
    if (code !== 0) {
      log('la construction a echoue')
      shutdown(code)
    }
  } else {
    log('bundle deja construit')
  }
  if (!shuttingDown) {
    log('demarrage d\'Electron (app://)')
    track(run(electron, ['.'], { env: electronEnv }))
  }
}
