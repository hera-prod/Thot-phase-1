// Pont renderer → processus principal.
//
// Point d'entrée IPC du frontend. La coque est Electron : le renderer ne voit
// qu'une liste de verbes nommés exposés par le preload, jamais `ipcRenderer`.
 // Passer par cet adaptateur garde la surface IPC dans un seul fichier.
//
// Deux invariants de contrat, volontairement conservés depuis la migration :
//   1. les rejets sont des CHAÎNES NUES, pas des `Error`. `toIpcError` teste
//      `e.message` d'abord puis retombe sur `String(e)` ; changer ce contrat
//      modifierait le champ `retryable` de toute l'application.
//   2. les arguments entrants sont en camelCase (le pont historique appliquait
//      `rename_all = "camelCase"`), alors que les RETOURS et les PAYLOADS
//      D'ÉVÉNEMENTS restent en snake_case.

type ThotVerb = (args?: Record<string, unknown>) => Promise<unknown>

interface ThotBridge {
  /** Un verbe par commande ; le preload n'expose aucun canal générique. */
  subscribe(handler: (name: string, payload: unknown) => void): () => void
  pathForFile(file: File): string | null
  paths(): Promise<unknown>
}

declare global {
  interface Window {
    thot?: ThotBridge
  }
}

const MISSING_BRIDGE =
  "Le pont Electron est absent. Lancez l'application avec `npm start` " +
  '(le frontend ne doit pas être ouvert dans un navigateur seul).'

function bridge (): ThotBridge {
  const value = window.thot
  if (!value) throw new Error(MISSING_BRIDGE)
  return value
}

/** Exception dont le `message` est déjà la chaîne d'erreur du main process. */
class BridgeError extends Error {
  constructor (message: string) {
    super(message)
    this.name = 'BridgeError'
  }
}

/**
 * Équivalent d'`invoke` : rejette avec la chaîne nue pour que le
 * contrat d'erreur du frontend reste identique.
 */
export async function invoke<T> (
  command: string,
  args?: Record<string, unknown>
): Promise<T> {
  try {
    const api = bridge() as unknown as Record<string, ThotVerb | undefined>
    // Le preload expose un verbe NOMMÉ par commande : c'est lui qui fait
    // autorité, donc un nom absent est un refus explicite plutôt qu'un
    // `undefined is not a function` avalé par le `.catch` du frontend.
    const verb = api[command]
    if (typeof verb !== 'function') {
      throw new Error(`Commande inconnue du pont : ${command}`)
    }
    return (await verb.call(api, args)) as T
  } catch (error) {
    if (error instanceof BridgeError) throw error.message
    // Electron rejette avec une `Error` dont le message est la chaîne d'origine.
    if (error instanceof Error) throw error.message
    throw String(error)
  }
}

/** Équivalent de `listen` : résout la fonction de désabonnement. */
export function listen<T> (
  event: string,
  handler: (envelope: { payload: T }) => void
): Promise<() => void> {
  let unsubscribe: (() => void) | null = null
  const pending: Array<(fn: () => void) => void> = []

  const off = bridge().subscribe((name, payload) => {
    if (name !== event) return
    handler({ payload: payload as T })
  })
  unsubscribe = off

  for (const resolve of pending) resolve(off)
  pending.length = 0

  return Promise.resolve(off)
}

/** Chemin absolu d'un `File` déposé — `File.path` n'existe plus en Electron. */
export function pathForFile (file: File): string | null {
  try {
    return bridge().pathForFile(file)
  } catch {
    return null
  }
}

/** Chemins de données résolus par le processus principal. */
export async function dataPaths (): Promise<{
  dataDir: string
  dbPath: string
  tempDir: string
} | null> {
  return (await bridge().paths()) as {
    dataDir: string
    dbPath: string
    tempDir: string
  } | null
}
