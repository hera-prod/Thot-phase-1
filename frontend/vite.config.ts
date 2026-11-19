import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

/**
 * Refuse qu'un bundle contienne un spécificateur de paquet que le navigateur ne
 * saura pas résoudre.
 *
 * Le contexte, parce que la parade est discrète. Le plugin de coloration
 * chargeait ses grammaires par `import(`…${nom}`)`. Vite n'analyse pas une
 * expression dans un gabarit : il laissait le spécificateur NU dans le bundle,
 * le navigateur ne résout pas un nom de paquet, et les grammaires n'arrivaient
 * jamais. Résultat : les blocs de code s'affichaient, mais sans aucune couleur.
 *
 * Ce qui rend la faute invisible :
 *   - le build n'émet AUCUN avertissement dessus (vérifié) ;
 *   - les tests passaient, parce que vitest résout ce que Vite ne résout pas.
 *
 * D'où ce garde-fou : mieux vaut un build rouge qu une app en noir et blanc.
 * `tests/frontend/buildArtifact.test.ts` fait le même contrôle côté test, sur
 * un build `write: false`. Les deux sont complémentaires — celui-ci attrape le
 * build de production même si on saute les tests, celui-là tourne dans `npm test`.
 *
 * Deux pièges, tous deux rencontrés :
 *   1. N'itérer que les CHUNKS. `.code` n'existe pas sur un `OutputAsset`, et
 *      les sources `.map` contiennent légitimement des chemins résolus vers le
 *      paquet — le build casserait pour une raison sans rapport.
 *   2. Le motif doit être ÉTROIT : `import(` AVANT le nom de paquet. Un
 *      `chunk.includes("@shikijs/")` large ferait aussi tomber le build sur un
 *      simple message d'avertissement qui cite le paquet — c'est-à-dire sur le
 *      comportement CORRECT.
 */
function pasDeSpecifierNu(): Plugin {
  const motif = /import\(\s*["'`]@shikijs\//
  return {
    name: 'thot:pas-de-specifier-nu',
    apply: 'build',
    generateBundle(_options, bundle) {
      for (const [nom, morceau] of Object.entries(bundle)) {
        if (morceau.type !== 'chunk') continue
        if (motif.test(morceau.code)) {
          this.error(
            `${nom} contient un import() de paquet non analysable par Vite.\n` +
              `  Un navigateur ne résout pas un nom de paquet : ce code ne\n` +
              `  s'exécutera jamais. Écrire l'import en littéral pour que\n` +
              `  Vite le transforme en URL de chunk.\n` +
              `  (import() avec un gabarit est la forme fautive — voir\n` +
              `  le registre PAR_ID dans src/components/chat/codePlugin.ts)`,
          )
        }
      }
    },
  }
}

export default defineConfig({
  // Tailwind compile les classes utilitaires qu'émet streamdown (`block`,
  // `text-[var(--sdm-c)]`…). Le preflight est volontairement absent : voir
  // src/styles/tailwind.css.
  plugins: [react(), tailwindcss(), pasDeSpecifierNu()],
  publicDir: 'public',
  server: {
    host: '127.0.0.1',
    port: 1420,
    strictPort: true,
  },
  clearScreen: false,
})