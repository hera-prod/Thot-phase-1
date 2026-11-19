'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')

const { mapImageGenerateArgs } = require('../../electron/main.cjs')

test('image_generate mappe sourceTransform/sourceSize camelCase vers snake_case', () => {
  const args = {
    prompt: 'un chat',
    aspectRatio: '16:9',
    sourceTransform: { x: 437.5, y: 0, width: 405, height: 720 },
    sourceSize: { width: 720, height: 1280 }
  }
  const out = mapImageGenerateArgs(args)
  assert.deepEqual(out.source_transform, args.sourceTransform)
  assert.deepEqual(out.source_size, args.sourceSize)
})

test('image_generate accepte la variante snake_case source_transform/source_size', () => {
  const args = {
    prompt: 'un chat',
    source_transform: { x: 1, y: 2, width: 3, height: 4 },
    source_size: { width: 5, height: 6 }
  }
  const out = mapImageGenerateArgs(args)
  assert.deepEqual(out.source_transform, args.source_transform)
  assert.deepEqual(out.source_size, args.source_size)
})

test('image_generate met source_transform et source_size à null en absence', () => {
  const out = mapImageGenerateArgs({ prompt: 'un chat' })
  assert.equal(out.source_transform, null)
  assert.equal(out.source_size, null)
})

test('image_generate mappe maskKind aussi bien camelCase que snake_case', () => {
  assert.equal(mapImageGenerateArgs({ prompt: 'p', maskKind: 'rectangle' }).mask_kind, 'rectangle')
  assert.equal(mapImageGenerateArgs({ prompt: 'p', mask_kind: 'freehand' }).mask_kind, 'freehand')
})

test('image_generate mappe workToken aussi bien camelCase que snake_case', () => {
  assert.equal(mapImageGenerateArgs({ prompt: 'p', workToken: 'tok-camel' }).work_token, 'tok-camel')
  assert.equal(mapImageGenerateArgs({ prompt: 'p', work_token: 'tok-snake' }).work_token, 'tok-snake')
})

test('image_generate préfère la valeur camelCase quand les deux variantes sont présentes', () => {
  const out = mapImageGenerateArgs({
    prompt: 'p',
    sourceTransform: { x: 10 },
    source_transform: { x: 20 },
    sourceSize: { width: 30 },
    source_size: { width: 40 }
  })
  assert.deepEqual(out.source_transform, { x: 10 })
  assert.deepEqual(out.source_size, { width: 30 })
})