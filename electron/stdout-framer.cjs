'use strict'

// Lecture des lignes JSON émises par le sidecar sur stdout.
//
// Une classe et pas un `split('\n')` : le sidecar écrit en `ensure_ascii=False`,
// donc le pipe peut couper une ligne au milieu d'une séquence UTF-8. Découper des
// chaînes perdrait ou corromprait des caractères. On accumule donc des OCTETS et
// on ne décode qu'une fois la ligne complète.
//
// La borne à 2 Mio et la resynchronisation existent pour qu'une ligne aberrante
// (le backend qui bavarde sur stdout) ne fasse pas croître le tampon sans limite :
// on abandonne la ligne trop longue et on se resynchronise sur la suivante.

// 32 Mio : même plafond que la ligne IPC entrante (INPUT_LIMITS.ipc_line_bytes).
// Un aperçu de 20 Mio voyage en base64 (~27 Mio) ; la borne précédente de 2 Mio
// faisait jeter toute réponse d'aperçu avant qu'elle n'atteigne le renderer.
// La resynchronisation reste identique : une ligne aberrante est abandonnée.
const MAX_STDOUT_LINE_BYTES = 32 * 1024 * 1024

/** Retire un éventuel `\r` final (tolérance CRLF) et valide une ligne. */
function parseSidecarLine (line) {
  const text = line.endsWith('\r') ? line.slice(0, -1) : line
  if (text.trim() === '') return { skip: true }
  let value
  try {
    value = JSON.parse(text)
  } catch {
    // JSON invalide = ligne PERDUE, et comptée : `dropped` est le seul
    // signal qui dise que l'information a été perdue.
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return { skip: false }
  if (typeof value.event !== 'string' || value.event === '') return { skip: false }
  return { value }
}

class StdoutFramer {
  constructor () {
    this.pending = []
    this.pendingBytes = 0
    this.discardingLine = false
    this.dropped = 0
  }

  recordDrop () {
    this.dropped = this.dropped + 1
  }

  clearPending () {
    this.pending = []
    this.pendingBytes = 0
    this.discardingLine = false
  }

  /**
   * Consomme un chunk de stdout et renvoie la liste des valeurs prêtes.
   * Un chunk peut contenir plusieurs lignes, une ligne plusieurs chunks.
   */
  push (chunk) {
    const values = []
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)

    let start = 0
    for (;;) {
      const index = bytes.indexOf(0x0a, start)
      if (index === -1) break

      const segment = bytes.subarray(start, index)
      if (this.discardingLine) {
        // Queue d'une ligne trop longue : on jette jusqu'au \n suivant.
        this.discardingLine = false
        this.clearPending()
      } else {
        this.pending.push(segment)
        this.pendingBytes += segment.length
        if (this.pendingBytes > MAX_STDOUT_LINE_BYTES) {
          this.clearPending()
          this.recordDrop()
        } else {
          const parsed = parseSidecarLine(Buffer.concat(this.pending).toString('utf8'))
          if (parsed.value !== undefined) values.push(parsed.value)
          else if (!parsed.skip) this.recordDrop()
        }
        this.clearPending()
      }
      start = index + 1
    }

    const rest = bytes.subarray(start)
    if (rest.length > 0) {
      if (this.discardingLine) return values
      this.pending.push(rest)
      this.pendingBytes += rest.length
      if (this.pendingBytes > MAX_STDOUT_LINE_BYTES) {
        this.recordDrop()
        this.clearPending()
        this.discardingLine = true
      }
    }
    return values
  }

  /** Appelé à la fermeture du flux : une ligne sans `\n` final est perdue. */
  finish () {
    if (this.pendingBytes > 0) this.recordDrop()
    this.clearPending()
  }
}

module.exports = { StdoutFramer, MAX_STDOUT_LINE_BYTES, parseSidecarLine }
