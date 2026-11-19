'use strict'

// Le preload doit rester autonome : avec `sandbox: true` il ne peut require()
// que 'electron'. Il duplique donc la liste de commands.cjs, et ce test est le
// garde-fou qui empeche les deux listes de diverger en silence — ce qui
// produirait exactement le symptôme « pont absent », sans la moindre erreur
// console.

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')

const { COMMANDS: REGISTRY } = require('../../electron/commands.cjs')

const preloadSource = fs.readFileSync(path.join(__dirname, '../../electron/preload.cjs'), 'utf8')

test('le preload ne require que electron (contrainte du bac a sable)', () => {
  // On retire les commentaires : le fichier *parle* de `require('./commands.cjs')`
  // pour expliquer pourquoi il ne le fait pas, et le naif le lirait comme un appel.
  const code = preloadSource.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/.*$/gm, '')
  const requires = [...code.matchAll(/require\('([^']+)'\)/g)].map((m) => m[1])
  assert.deepEqual(requires, ['electron'])
})

test('la liste du preload est identique au registre du main process', () => {
  const listed = preloadSource.match(/const COMMANDS = Object\.freeze\(\[([\s\S]*?)\]\)/)
  assert.ok(listed, 'liste COMMANDS introuvable dans le preload')
  const fromPreload = [...listed[1].matchAll(/'([a-z_]+)'/g)].map((m) => m[1]).sort()
  assert.deepEqual(fromPreload, [...REGISTRY].sort())
})

test('chaque commande a un routage de methode dans le registre, sauf les locales', () => {
  const { SIDECAR_METHODS } = require('../../electron/commands.cjs')
  const LOCAL = new Set(['image_stage_work'])
  for (const command of REGISTRY) {
    if (LOCAL.has(command)) continue
    assert.ok(SIDECAR_METHODS[command], `commande sans routage : ${command}`)
  }
})

test('le registre ne contient aucune commande hors liste blanche', () => {
  // Garde-fou : un nom de methode libre (du type 'shell.exec') ne doit jamais
  // pouvoir entrer dans la table de routage.
  const { SIDECAR_METHODS, ALLOWED } = require('../../electron/commands.cjs')
  for (const command of Object.keys(SIDECAR_METHODS)) {
    assert.ok(ALLOWED.has(command), `routage hors registre : ${command}`)
  }
})

test('le depot d\'un espace est un canal autorise', () => {
  const { ALLOWED } = require('../../electron/commands.cjs')
  assert.ok(ALLOWED.has('espace_depot'))
  assert.ok(ALLOWED.has('espace_depot_retirer'))
})

test('les canaux de depot routent vers les methodes du sidecar', () => {
  const { SIDECAR_METHODS } = require('../../electron/commands.cjs')
  assert.equal(SIDECAR_METHODS.espace_depot, 'espace.depot')
  assert.equal(SIDECAR_METHODS.espace_depot_retirer, 'espace.depot_retirer')
})

test('le deplacement d une conversation est un canal autorise des quatre cotes', () => {
  const { SIDECAR_METHODS, ALLOWED } = require('../../electron/commands.cjs')
  assert.ok(ALLOWED.has('espace_conversation_deplacer'))
  assert.equal(SIDECAR_METHODS.espace_conversation_deplacer, 'espace.conversation_deplacer')
})

test('le renommage d un document du depot est un canal autorise des quatre cotes', () => {
  // depot.md §5.2 : renommer modifie le nom affiché, rien d'autre. Le canal
  // suit la même route que les autres verbes d'espace ; l'égalité
  // preload/main est vérifiée par le test 'la liste du preload est
  // identique au registre du main process'.
  const { SIDECAR_METHODS, ALLOWED } = require('../../electron/commands.cjs')
  assert.ok(ALLOWED.has('espace_depot_renommer'))
  assert.equal(SIDECAR_METHODS.espace_depot_renommer, 'espace.depot_renommer')
})

test('la relance d extraction est un canal autorise des quatre cotes', () => {
  // Phase 6 : le bouton « Réessayer » du dépôt passe par un verbe nommé, jamais
  // par un chemin ni un choix de modèle fourni par le renderer.
  const { SIDECAR_METHODS, ALLOWED } = require('../../electron/commands.cjs')
  assert.ok(ALLOWED.has('artifact_ingestion_retry'))
  assert.equal(SIDECAR_METHODS.artifact_ingestion_retry, 'artifact.ingestion_retry')
})

test('la promotion vers le depot est un canal autorise des quatre cotes', () => {
  // espaces.md §6, promotion d'un fichier de discussion vers le dépôt : la
  // cible est déduite côté serveur, jamais transmise par le renderer.
  const { SIDECAR_METHODS, ALLOWED } = require('../../electron/commands.cjs')
  assert.ok(ALLOWED.has('espace_conversation_partager'))
  assert.equal(SIDECAR_METHODS.espace_conversation_partager, 'espace.conversation_partager')
})
