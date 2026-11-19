'use strict'

// Ces trois cas sont la SPECIFICATION du framer : UTF-8 fractionne avec CRLF,
// lignes malformees et fin de flux sans saut de ligne, borne de taille et
// resynchronisation. Les deux derniers sont les plus utiles en pratique :
// c'est eux qui empechent le backend de bavarder sur stdout sans rien signaler.
//
// Executables : `node --test tests/electron/`

const test = require('node:test')
const assert = require('node:assert/strict')
const { StdoutFramer, MAX_STDOUT_LINE_BYTES } = require('../../electron/stdout-framer.cjs')

// stdout_framer_handles_fractionated_utf8_json_and_crlf
test('fragmente un JSON UTF-8 avec CRLF et le restitue intact', () => {
  const framer = new StdoutFramer()
  const line = Buffer.from('{"event":"image_status","payload":{"text":"é"}}\r\n', 'utf8')
  const values = []
  for (const chunk of [line.subarray(0, 3), line.subarray(3, 7), line.subarray(7)]) {
    values.push(...framer.push(chunk))
  }
  assert.equal(values.length, 1)
  assert.equal(values[0].event, 'image_status')
  assert.equal(values[0].payload.text, 'é')
  assert.equal(framer.dropped, 0)
})

// stdout_framer_drops_malformed_lines_and_incomplete_eof
test('ignore les lignes malformées et compte une ligne finale incomplète', () => {
  const framer = new StdoutFramer()
  assert.deepEqual(framer.push(Buffer.from('{not-json}\n')), [])
  assert.equal(framer.dropped, 1)

  assert.deepEqual(framer.push(Buffer.from([0xff, 0x0a])), [])
  assert.equal(framer.dropped, 2)

  // Un objet sans `event` exploitable est rejeté comme une ligne malformée.
  assert.deepEqual(framer.push(Buffer.from('{"payload":{}}\n')), [])
  assert.equal(framer.dropped, 3)

  // Ligne sans `\n` final : perdue à la fermeture du flux.
  framer.push(Buffer.from('{"event":"answer","payload":{}}'))
  framer.finish()
  assert.equal(framer.dropped, 4)
})

test('fige le plafond de ligne stdout à 32 Mio', () => {
  // Valeur littérale, pas `MAX_STDOUT_LINE_BYTES` : un retour à 2 Mio doit
  // échouer ici, même si les autres tests s'adaptaient à la constante.
  assert.equal(MAX_STDOUT_LINE_BYTES, 32 * 1024 * 1024)
})

// stdout_framer_bounds_and_resynchronizes
test('borne la taille de ligne et se resynchronise sur la suivante', () => {
  const framer = new StdoutFramer()
  const oversized = Buffer.alloc(32 * 1024 * 1024 + 1, 0x61)
  framer.push(Buffer.concat([Buffer.from('{"event":"thinking","payload":{"text":"'), oversized, Buffer.from('"}}\n')]))
  assert.equal(framer.dropped, 1)

  // Après une ligne trop longue, la ligne suivante doit être lue normalement.
  const values = framer.push(Buffer.from('{"event":"answer","payload":{"text":"ok"}}\n'))
  assert.equal(values.length, 1)
  assert.equal(values[0].event, 'answer')
})

test('accepte une ligne d’aperçu de ~27 Mio (20 Mio en base64)', () => {
  // Un aperçu de 20 Mio voyage en base64 : ~27 Mio de charge utile. C'est la
  // ligne réelle qui était jetée par l'ancienne borne de 2 Mio.
  const framer = new StdoutFramer()
  const preview = Buffer.alloc(27 * 1024 * 1024, 0x61)
  const line = Buffer.concat([
    Buffer.from('{"event":"get_staged_preview_bytes","payload":{"data":"'),
    preview,
    Buffer.from('"}}\n')
  ])

  const values = framer.push(line)
  assert.equal(framer.dropped, 0)
  assert.equal(values.length, 1)
  assert.equal(values[0].event, 'get_staged_preview_bytes')
  assert.equal(values[0].payload.data.length, preview.length)
})

test('traite plusieurs lignes complètes dans un seul chunk', () => {
  const framer = new StdoutFramer()
  const values = framer.push(Buffer.from(
    '{"event":"run_started","payload":{"request_id":"a"}}\n' +
    '{"event":"message_started","payload":{"request_id":"a"}}\n' +
    '{"event":"answer","payload":{"text":"hi"}}\n'
  ))
  assert.deepEqual(values.map((v) => v.event), ['run_started', 'message_started', 'answer'])
  assert.equal(framer.dropped, 0)
})

test('ignore les lignes blanches sans les compter comme perte', () => {
  const framer = new StdoutFramer()
  const values = framer.push(Buffer.from('\n   \n{"event":"answer","payload":{}}\n'))
  assert.equal(values.length, 1)
  assert.equal(framer.dropped, 0)
})
