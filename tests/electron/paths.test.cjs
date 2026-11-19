'use strict'

// Le choix du lanceur du sidecar est la SPECIFICATION de la portabilité, executée
// côté JavaScript. Le pendant Python vit dans
// `tests/backend/test_resolution_chemins.py` ; ici on prouve ce qui ne peut
// existir que dans le processus principal Electron.
//
// Executables : `node --test tests/electron/`

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const { resolveSidecarLaunch, readDistribution } = require('../../electron/paths.cjs')

const SIDECAR = 'thot-sidecar-x86_64-unknown-linux-gnu'

/** Un dossier d'application jetable, avec son installation packagée. */
function installation ({ avecSidecar = true, avecManifeste = true } = {}) {
  const racine = fs.mkdtempSync(path.join(os.tmpdir(), 'thot-instal-'))
  const appDir = path.join(racine, 'app')
  const resources = path.join(appDir, 'resources')
  fs.mkdirSync(resources, { recursive: true })
  if (avecManifeste) {
    fs.writeFileSync(
      path.join(appDir, 'distribution.json'),
      JSON.stringify({ product: 'Thot Phase 1', sidecar: SIDECAR }, null, 2)
    )
  }
  if (avecSidecar) {
    const binaire = path.join(appDir, SIDECAR)
    fs.writeFileSync(binaire, '#!/bin/false\n')
    fs.chmodSync(binaire, 0o755)
  }
  return { racine, appDir, resources }
}

// sidecar_launcher_never_borrows_path_python_when_packaged
test('en mode packagé, le sidecar du manifeste est utilisé, pas python3', () => {
  const { appDir, resources } = installation()

  const lancement = resolveSidecarLaunch({ packaged: true, projectRoot: appDir, resourcesDir: resources })

  assert.equal(lancement.command, path.join(appDir, SIDECAR))
  assert.deepEqual(lancement.args, [])
  assert.ok(!/python/.test(path.basename(lancement.command)))
})

// sidecar_launcher_refuses_missing_frozen_sidecar
test('sidecar gelé absent : refus, et le message dit qu\'il n\'y a pas de repli', () => {
  const { appDir, resources } = installation({ avecSidecar: false })

  assert.throws(
    () => resolveSidecarLaunch({ packaged: true, projectRoot: appDir, resourcesDir: resources }),
    (erreur) => {
      assert.match(erreur.message, /Sidecar gelé introuvable/)
      assert.match(erreur.message, /Aucun repli sur python3/)
      return true
    }
  )
})

// sidecar_launcher_refuses_manifest_without_sidecar
test('manifeste sans sidecar : refus plutôt que repli sur l\'interpréteur du poste', () => {
  const { appDir, resources } = installation({ avecManifeste: false })

  assert.throws(
    () => resolveSidecarLaunch({ packaged: true, projectRoot: appDir, resourcesDir: resources }),
    (erreur) => {
      assert.match(erreur.message, /ne déclare pas de sidecar gelé/)
      return true
    }
  )
})

// sidecar_launcher_refuses_absolute_sidecar
test('un sidecar ABSOLU est refusé : un manifeste ne nomme pas une machine', () => {
  const { appDir, resources } = installation({ avecManifeste: false, avecSidecar: false })
  fs.writeFileSync(
    path.join(appDir, 'distribution.json'),
    JSON.stringify({ sidecar: '/usr/bin/python3' })
  )

  assert.throws(
    () => resolveSidecarLaunch({ packaged: true, projectRoot: appDir, resourcesDir: resources }),
    (erreur) => {
      assert.match(erreur.message, /absolu/)
      return true
    }
  )
})

// sidecar_launcher_keeps_development_mode_working
test('le mode développement fonctionne toujours : c\'est lui qui sert aujourd\'hui', () => {
  const { appDir, resources } = installation()

  const lancement = resolveSidecarLaunch({
    packaged: false,
    projectRoot: appDir,
    resourcesDir: resources,
    env: { PATH: process.env.PATH }
  })

  assert.deepEqual(lancement.args, ['-m', 'backend.main'])
  assert.equal(lancement.cwd, appDir)
  assert.ok(lancement.command.endsWith('python3') || lancement.command.endsWith('python'))
})

// sidecar_launcher_reports_missing_development_interpreter
test('développement sans interpréteur : le refus nomme le remède', () => {
  const { appDir, resources } = installation()

  assert.throws(
    () => resolveSidecarLaunch({
      packaged: false,
      projectRoot: appDir,
      resourcesDir: resources,
      env: { PATH: path.join(appDir, 'bin-vide'), THOT_DEV_PYTHON: 'python-introuvable' }
    }),
    (erreur) => {
      assert.match(erreur.message, /THOT_DEV_PYTHON/)
      return true
    }
  )
})

// sidecar_launcher_honours_dev_python_override
test('THOT_DEV_PYTHON désigne l\'interpréteur du poste quand il le faut', () => {
  const { appDir, resources } = installation()
  const interpreteur = path.join(appDir, 'python-maison')
  fs.writeFileSync(interpreteur, '#!/bin/false\n')
  fs.chmodSync(interpreteur, 0o755)

  const lancement = resolveSidecarLaunch({
    packaged: false,
    projectRoot: appDir,
    resourcesDir: resources,
    env: { PATH: process.env.PATH, THOT_DEV_PYTHON: interpreteur }
  })

  assert.equal(lancement.command, interpreteur)
})

// distribution_manifest_is_read_from_the_application_roots
test('le manifeste se lit dans les racines de l\'application, jamais dans le cwd', () => {
  const { appDir, resources } = installation()
  const ailleurs = fs.mkdtempSync(path.join(os.tmpdir(), 'thot-ailleurs-'))
  fs.writeFileSync(path.join(ailleurs, 'distribution.json'), JSON.stringify({ sidecar: 'leurre' }))
  const cwd = process.cwd()
  process.chdir(ailleurs)
  try {
    assert.equal(readDistribution({ resourcesDir: resources, appRoot: appDir }).sidecar, SIDECAR)
  } finally {
    process.chdir(cwd)
  }
})