#!/usr/bin/env node
// Preflight du mode développement : remet la session dans un état cohérent.
//
// Symptôme d'origine (q-r.md) :
// un lancement interrompu laissait derrière lui son Vite, adopté par
// systemd --user. Le port 1420 restait pris et le lancement suivant échouait sur
// « Port 1420 is already in use », l'application se rechargeant alors contre le
// serveur du lancement précédent.
//
// En mode `npm start` (bundle local) il n'y a ni port ni Vite : ce script n'est
// utile qu'à `npm run dev`, qui passe par le serveur de développement.
//
// Règle : on ne touche qu'à un processus dont la lignée est reconnue comme
// appartenant à ce dépôt. Tout autre occupant du port est signalé, jamais tué.

import { execFileSync } from 'node:child_process'
import { readFileSync, realpathSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const port = 1420

// Electron ^44.5.0 verrouille Node >= 22.12.0. On refuse d'aller plus loin sur
// une version incompatible plutot que de laisser echouer l'installation a froid
// ou le lancement avec une erreur opaque plus tard.
const MIN_NODE = [22, 12]
const [nodeMajor, nodeMinor] = process.versions.node.split('.').map(Number)
if (
  nodeMajor < MIN_NODE[0] ||
  (nodeMajor === MIN_NODE[0] && nodeMinor < MIN_NODE[1])
) {
  abort(
    `Node.js ${MIN_NODE.join('.')}+ est requis par Electron (version actuelle : ${process.versions.node}).`
  )
}

function say(message) {
  process.stdout.write(`[dev] ${message}\n`)
}

function abort(message) {
  process.stderr.write(`\n[dev] ${message}\n\n`)
  process.exit(1)
}

function cmdlineOf(pid) {
  try {
    return readFileSync(`/proc/${pid}/cmdline`, 'utf8').replace(/\0/g, ' ').trim()
  } catch {
    return ''
  }
}

function inRepo(path) {
  try {
    return realpathSync(path).startsWith(realpathSync(repoRoot))
  } catch {
    return false
  }
}

/** Vite de ce dépôt. */
function isOurVite(pid) {
  const cmd = cmdlineOf(pid)
  if (!/vite/.test(cmd)) return false
  const bin = cmd.match(/(\/[^\s]*\/vite)(\s|$)/)?.[1]
  return bin ? inRepo(bin) : false
}

function portHolders() {
  try {
    return execFileSync('ss', ['-tlnp'], { encoding: 'utf8' })
      .split('\n')
      .filter((line) => line.includes(`:${port} `) && line.includes('LISTEN'))
      .map((line) => {
        const pid = line.match(/pid=(\d+)/)?.[1]
        return pid ? { pid, cmd: cmdlineOf(pid) } : null
      })
      .filter(Boolean)
  } catch {
    return [] // `ss` absent (macOS) : rien à conclure, on laisse passer.
  }
}

function stop(pids, label) {
  for (const pid of pids) {
    try {
      process.kill(Number(pid), 'SIGTERM') // SIGTERM d'abord : fermeture propre
    } catch {
      /* déjà parti */
    }
  }
  const deadline = Date.now() + 5000
  while (Date.now() < deadline) {
    const alive = pids.filter((pid) => {
      try {
        process.kill(Number(pid), 0)
        return true
      } catch {
        return false
      }
    })
    if (alive.length === 0) return true
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 100)
  }
  for (const pid of pids) {
    try {
      process.kill(Number(pid), 'SIGKILL')
    } catch {
      /* déjà parti */
    }
  }
  say(`${label} arrêté(s) : ${pids.join(', ')}`)
  return true
}

// 2. Port 1420.
const holders = portHolders()
if (holders.length === 0) process.exit(0)

const ours = holders.filter((h) => isOurVite(h.pid))
const foreign = holders.filter((h) => !isOurVite(h.pid))

if (foreign.length > 0) {
  abort(
    `Le port ${port} est occupé par un processus qui n'appartient pas à ce dépôt :\n` +
      foreign.map((h) => `    pid ${h.pid}  ${h.cmd || '(commande inconnue)'}`).join('\n') +
      `\n\nCe processus n'a pas été touché. Fermez-le, puis relancez.`,
  )
}

say(`Serveur de développement de ce dépôt encore actif sur ${port} :`)
for (const h of ours) say(`    pid ${h.pid}  ${h.cmd}`)
stop(
  ours.map((h) => h.pid),
  'Vite',
)

const remaining = portHolders()
if (remaining.length > 0) {
  abort(
    `Le port ${port} reste occupé après arrêt :\n` +
      remaining.map((h) => `    pid ${h.pid}  ${h.cmd}`).join('\n') +
      `\n\nArrêtez-le manuellement, puis relancez.`,
  )
}
say(`Port ${port} libéré.`)
process.exit(0)
