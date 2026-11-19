/// <reference types="vite/client" />
import { build } from "vite";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { it, expect } from "vitest";

// PORTE D'ARTEFACT — la seule qui voit RÉELLEMENT ce dont parle ce fichier.
//
// HISTORIQUE, parce qu'il faut le garder en tête à chaque modification :
// les blocs de code s'affichaient mais sans aucune couleur. La cause : le
// plugin chargeait les grammaires par `import(`…${nom}`)`. Vite ne sait pas
// analyser une expression dans un gabarit, il laissait le spécificateur NU
// dans le bundle, et le navigateur ne résout pas un nom de paquet.
//
// Trois raisons pour lesquelles les autres portes n'ont rien vu :
//   1. Le build de production n'émet AUCUN avertissement sur ce sujet.
//   2. vitest RÉSOUT le spécificateur nu via son pipeline Node. Le même code
//      passait 182 tests et produisait un app sans couleur.
//   3. Aucun test DOM ne peut le voir : le repli de streamdown écrit
//      `--sdm-c: inherit`, ce qui satisfait n'importe quelle assertion
//     asonable sur la présence de la variable.
//
// Donc on teste l'ARTEFACT : après un vrai build, un spécificateur nu ne doit
// pas exister, et une grammaire doit être présente. Pas de navigateur, pas
// d'Electron, pas de jsdom.
//
// Ce fichier est séparé de `markdown.test.tsx` volontairement : il lance un
// build de production, donc il lui faut un `logLevel: 'silent'` et un timeout
// long. Mélanger ça aux tests DOM qui rendent du HTML, on contaminerait les
// uns par les autres.

// La racine du frontend vient de `process.cwd()`, PAS de `import.meta.url`.
//
// Le premier ne marche pas ici : le serveur de dev de vitest réécrit
// `import.meta.url` en `http://localhost:3000/@fs/…` dès qu'il apparaît dans
// une expression, et `fileURLToPath` lève alors « The URL must be of scheme
// file ». Le second vaut `/…/frontend` — le `cwd` est déjà le racine vitest, et
// le script `npm test` part de là. Le garde ci-dessous évite qu'un lancement
// depuis un autre répertoire donne un build vide qui « passe ».
const RACINE = process.cwd();
if (!existsSync(join(RACINE, "vite.config.ts"))) {
  throw new Error(
    `lancez vitest depuis frontend/ — cwd vaut « ${RACINE} », sans vite.config.ts`,
  );
}

/**
 * Un spécificateur de paquet nu. `import("…")` littéral et analysable, c'est
 * Vite qui le réécrit en URL de chunk. Un `import(` suivi d'un gabarit ou
 * d'un nom de paquet, c'est un spécificateur non résolu — exactement le bug.
 *
 * Volontairement étroit : on cherche `import(` AVANT le nom de paquet. Une
 * garde large du type `chunk.includes("@shikijs/")` ferait échouer le build à
 * cause d'un message d'avertissement qui cite le paquet — voir le commentaire
 * du `console.warn` dans `codePlugin.ts`.
 */
const SPECIFIQUEUR_NU = /import\(\s*["'`]@shikijs\//;

/**
 * Marqueur de grammaire dans le bundle.
 *
 * `source.python` était le marqueur précédent — et il était VACUUS. Streamdown
 * embarque ses propres grammaires, donc `source.python` est dans le bundle
 * depuis le début, sans que notre plugin ait jamais chargé quoi que ce soit.
 * Vérifié sur le bundle réel d'avant le passage au registre : `def_stmt`,
 * `Python3` et `function_definition` y sont absents (shiki 4 n'emploie pas ces
 * noms — j'ai perdu du temps avec un `grep` là-dessus), et `source.python` y est
 * PRÉSENT. Une assertion qui passe sur le build cassé ne prouve rien.
 *
 * Règle : un marqueur d'artefact ne prouve que ce qu'il peut ÉCHOUER. Il faut
 * donc le choisir parmi ce qui est absent AVANT le changement. Les trois
 * ci-dessous sont absents du bundle d'avant et présents après : ils ne peuvent
 * venir que de notre registre.
 */
const GRAMMAIRES_DU_REGISTRE = /source\.(zig|wgsl|gleam)/;

it("le bundle n'a aucun spécificateur de paquet nu et contient bien les grammaires", async () => {
  const resultat = await build({
    root: RACINE,
    // Chemin ABSOLU et explicitement fourni. `configFile: false` supprimerait
    // silencieusement les plugins `react()` et `tailwindcss()` — on testerait
    // alors un bundle qui n'est pas celui de l'app.
    configFile: `${RACINE}/vite.config.ts`,
    // `write: false` : rien n'est écrit sur le disque. C'est aussi ce qui évite
    // de recopier les 91 Mo d'images du `public` à chaque exécution.
    build: { write: false, sourcemap: false },
    logLevel: "silent",
  });

  // `build()` renvoie une union à trois bras selon sa signature Vite :
  // `RolldownOutput | RolldownOutput[] | RolldownWatcher`. `.output` n'existe
  // que sur le premier. On rétrécit par prédicat car le type lui-même n'est pas
  // importable : vite ne ré-exporte PAS `RolldownOutput`, et `rolldown` n'est
  // pas une dépendance du projet.
  if (Array.isArray(resultat) || !("output" in resultat)) {
    throw new Error("build() n'a pas renvoyé de bundle");
  }

  const morceaux = Object.values(resultat.output).filter(
    (x): x is Extract<typeof x, { type: "chunk" }> => x.type === "chunk",
  );
  const code = morceaux.map((m) => m.code).join("\n");

  // Plancher : un build qui n'émet rien ne doit pas passer « par chance ».
  expect(morceaux.length).toBeGreaterThan(0);
  expect(code.length).toBeGreaterThan(1000);

  // La garde. Un jour, un `import(` par gabarit, et ce test tombe.
  expect(code.match(SPECIFIQUEUR_NU)).toBeNull();

  // Et l'autre moitié : le registre est bien déployé, et ses grammaires sont
  // donc dans le bundle. Sans ce second test, un correctif qui SUPPRIMERait le
  // chargement — au lieu de le corriger — passerait le premier.
  //
  // Pas de compte de chunks : il est couplé à la politique de découpage de
  // rolldown, il prouverait que le build a grandi, pas que le registre s'est
  // déployé.
  expect(code).toMatch(GRAMMAIRES_DU_REGISTRE);
}, 120_000);