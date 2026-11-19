// Plugin de coloration syntaxique pour streamdown.
//
// POURQUOI CE FICHIER EXISTE
// `@streamdown/code` fait son travail, mais il embarque les 190 grammaires de
// shiki dans le build. On garde Shiki — c'est lui qui donne la qualité de
// coloration — mais on ne tient plus la liste des langages.
//
// Ce que ça coûte : les 242 grammaires pèsent ~8 Mo au total, réparties en
// chunks chargés à la demande. Le chunk principal ne bouge que de ~29 Ko (le
// registre). Rien n'est chargé au démarrage.
//
// À ne pas confondre avec les 94 Mo du `dist/assets`, qui sont des POLICES.
//
// `streamdown` exige un tuple de DEUX thèmes (light, dark) et tokenise en deux
// passes. Le projet n'a pas de thème sombre — voir l'invariant sur `themes`
// plus bas — donc on passe deux fois le même : coût ×2 assumé et mesuré.
//
// Moteur JAVASCRIPT et non WASM (oniguruma) : ce dernier est souvent
// indisponible sous CSP, et on ne veut pas un `.wasm` à charger.

import { createHighlighterCore, type HighlighterCore } from "shiki/core";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";
import type { ThemeRegistrationAny, TokensResult } from "shiki";
import { bundledLanguagesInfo, type BundledLanguage } from "shiki/langs";

import githubLight from "@shikijs/themes/github-light";

// ---------------------------------------------------------------------------
// LE REGISTRE, et pourquoi on ne s'en tient pas à nos 24.
//
// La version précédente listait 24 langages à la main, dans un fichier voisin.
// Trois problèmes, dont deux mesurés :
//
//   1. C'ÉTAIT UNE VERSION DÉGRADÉE DE CELLE DE SHIKI. Shiki déclare 104 alias
//      (`c#`, `cs`, `c++`, `ps1`, `pwsh`, `gql`, `kt`, `hs`…). Nous en
//      avions 38, et un seul d'entre eux — `svg` — que le registre ne résout
//      pas. Notre table était une copie à maintenir, et elle était déjà fausse.
//
//   2. ELLE POURRIT. Une liste de langages est une promesse de mise à jour. Le
//      registre est la source : impossible d'être en retard.
//
//   3. NOTRE PROPRE VERSION À 24 AVAIT UN BUG. `ALIAS["constructor"]` sur un
//      objet littéral renvoie le CONSTRUCTEUR `Object` — truthy. Le test
//      « cette langue est-elle inconnue ? » passait donc pour `constructor`,
//      `__proto__`, `toString`, `valueOf`… et la tentative de chargement
//      levait une TypeError, avec un `console.warn` À CHAQUE RENDU. Reproduit,
//      pas déduit.
//
// Le registre de shiki porte `import: () => import("@shikijs/langs/…")` : un
// LITTÉRAL, donc Vite sait l'analyser et produit un chunk par grammaire sans
// qu'on ait à écrire une seule ligne. `import.meta.glob` donnerait 361 chunks
// ; ici on en a 242, un par langue réellement enregistrée.
//
// D'où les `Map` PARTOUT, y compris pour `supportsLanguage` et
// `getSupportedLanguages` :
//   × `in` parcourt la chaîne de prototypes — une fuite.
//   × `Object.keys(map)` rend `[]` — un contrat silencieusement faux.
//   √ `Map.get` / `Map.has` : la clé est la clé, rien d'autre.

const PAR_ID = new Map<BundledLanguage, (typeof bundledLanguagesInfo)[number]>();
const PAR_ALIAS = new Map<string, BundledLanguage>();

for (const info of bundledLanguagesInfo) {
  const id = info.id as BundledLanguage;
  PAR_ID.set(id, info);
  for (const alias of info.aliases ?? []) PAR_ALIAS.set(alias, id);
}

// Les tags de fence que shiki ne déclare pas. Six, et six seulement : chacune
// a été vérifiée ABSENTE du registre, sinon elle n'a rien à faire ici.
//
// `svg` est le plus important de la liste : c'est le seul tag que notre
// ancienne table résolvait et que le registre ne résout pas. Sans cette ligne,
// tous les fences `svg` du monde repasseraient en monospace, en silence.
//
// À NOTER : `vue-html` n'est PAS dans cette liste. C'est un VRAI identifiant
// de shiki — le mapper vers `vue` serait une régression, pas un alias.
// `shell` et `sh` non plus : shiki les déclare déjà vers `shellscript`.
//
// Le type est `BundledLanguage`, une union littérale de 346 membres. Le type
// `id` de `bundledLanguagesInfo` est un simple `string` : l'annoter avec ça
// rendrait toute clé acceptable et `tsc` ne dirait rien.
const COMPLEMENT: Record<string, BundledLanguage> = {
  golang: "go",
  "obj-c": "objective-c",
  objectivec: "objective-c",
  rsx: "tsx",
  "c-sharp": "csharp",
  svg: "xml",
};

const COMPLEMENT_PAR_ID = new Map<string, BundledLanguage>(
  Object.entries(COMPLEMENT),
);

/**
 * Le tag de fence tel que shiki le verra. `null` = aucune grammaire, donc
 * aucune couleur — et ce n'est pas une erreur.
 */
function normalizeLanguage(language: string): BundledLanguage | null {
  const tag = language.trim().toLowerCase();
  if (!tag) return null;

  const complement = COMPLEMENT_PAR_ID.get(tag);
  if (complement) return complement;

  const alias = PAR_ALIAS.get(tag);
  if (alias) return alias;

  return PAR_ID.has(tag as BundledLanguage) ? (tag as BundledLanguage) : null;
}

// Le fond est forcé transparent : celui du bloc est le nôtre
// (`--thot-canvas`). Un thème avec son propre fond poserait un rectangle
// d'une autre teinte au milieu de la bulle ivoire.
const BASE = githubLight as ThemeRegistrationAny;

const CLAIR: ThemeRegistrationAny = {
  ...BASE,
  bg: "transparent",
  colors: { ...BASE.colors, "editor.background": "transparent" },
};

// Bornes de tokenisation. ELLES VONTENT ENSEMBLE, et le plan doit le dire.
//
// `tokenizeTimeLimit` est une limite MURALE, en millisecondes : au-delà,
// vscode-textmate abandonne la ligne en cours et émet la suite SANS COULEUR.
// Le défaut est 500, donc le résultat dépend de la charge de la machine — la
// même ligne peut rendre différente d'un rendu à l'autre. Le mettre à 0
// désactive le contrôle (`if (timeLimit !== 0)`), donc on retire la seule
// protection temporelle de shiki.
//
// C'est précisément pourquoi on pose `tokenizeMaxLineLength` : une borne sur la
// LONGUEUR de la source, pas sur l'horloge. La sortie dépend alors de l'entrée,
// jamais de l'hôte. C'est le défaut de VS Code pour la même raison — un JSON
// minifié ou une data URI ne doit pas figer l'app.
//
// Conséquence assumée : une ligne de 20 000 caractères ou plus rend en
// monospace. Si un jour un usage légitime dépasse ça, c'est UN SEUL nombre.
const LIMITES_TOKENISATION = {
  tokenizeTimeLimit: 0,
  tokenizeMaxLineLength: 20_000,
} as const;

function creerChargeur(themes: [ThemeInput, ThemeInput]): Promise<HighlighterCore> {
  // INVARIANT : `themes[0]` et `themes[1]` doivent être LE MÊME OBJET.
  //
  // Ce n'est pas une question de clef de cache. shiki enregistre les thèmes par
  // `name`, et `@shikijs/primitive` reconstruit l'état par
  // `Object.fromEntries(themedTokens.map(s => [s.theme, s.state?.…]))`. Deux
  // thèmes de même nom partagent donc un seul état, et la seconde passe est
  // SILENCIEUSEMENT JETÉE : aucune erreur, la mauvaise couleur.
  //
  // Aujourd'hui c'est vrai par construction — `CLAIR` passé deux fois, la même
  // référence. Si un vrai thème sombre arrive un jour, lui donner un `name`
  // DISTINCT, sinon la moitié de la coloration part dans le vide.
  return createHighlighterCore({
    themes,
    langs: [],
    engine: createJavaScriptRegexEngine({ forgiving: true }),
  });
}

export type ThemeInput = ThemeRegistrationAny;

export interface PluginOptions {
  themes?: [ThemeInput, ThemeInput];
}

export function createCodePlugin(options: PluginOptions = {}) {
  const themes: [ThemeInput, ThemeInput] = options.themes ?? [CLAIR, CLAIR];

  let chargeur: HighlighterCore | null = null;
  let chargement: Promise<HighlighterCore> | null = null;
  const enAttente = new Set<(h: HighlighterCore) => void>();

  function obtenir(): Promise<HighlighterCore> {
    if (chargeur) return Promise.resolve(chargeur);
    chargement ??= creerChargeur(themes).then((h) => {
      chargeur = h;
      enAttente.forEach((r) => r(h));
      enAttente.clear();
      chargement = null;
      return h;
    });
    return chargement;
  }

  const chargees = new Set<BundledLanguage>();

  async function assurer(h: HighlighterCore, nom: BundledLanguage) {
    if (chargees.has(nom)) return;
    const entree = PAR_ID.get(nom);
    // Un alias du registre peut ne pas être un id : c'est le cas de rien pour
    // l'instant, mais le garde rend la chose explicite plutôt que contingente.
    if (!entree) return;
    try {
      const mod = await entree.import();
      // `.default` est un TABLEAU de grammaires ; `loadLanguage` accepte les
      // deux formes. C'est lui qui déclenche le chargement du chunk.
      await h.loadLanguage(mod.default);
      // APRÈS le chargement, jamais avant. Marquer d'abord rendait un échec
      // définitif : le `Set` n'était jamais élagué, donc une grammaire qui
      // n'aurait pas chargé une fois ne réessayait plus jamais, pour toute la
      // session. Ici l'échec reste récupérable — au rendu suivant, ça repart.
      chargees.add(nom);
    } catch (e) {
      // Le seul chemin qui mène ici est un chunk qui ne se récupère pas — le
      // protocole `app://` d'Electron est le suspect évident — ou un registre
      // incohérent. C'est un DÉFAUT, pas une langue absente : on le dit, et
      // sans condition.
      //
      // Le message ne doit surtout pas contenir le nom du paquet : il serait
      // minifié dans le bundle, et la garde de build le prendrait pour un
      // spécificateur nu. `tests/frontend/buildArtifact.test.ts` verrait un
      // bundle « cassé » alors qu'il est sain.
      console.warn(`[thot] grammaire « ${nom} » non chargée`, e);
    }
  }

  function tokeniser(
    h: HighlighterCore,
    code: string,
    nom: BundledLanguage,
  ): TokensResult | null {
    try {
      return h.codeToTokens(code, {
        lang: nom,
        themes: { light: themes[0], dark: themes[1] } as never,
        ...LIMITES_TOKENISATION,
      });
    } catch {
      // Silencieux, et c'est voulu : le cas normal est « la grammaire charge en
      // ce moment même ». `assurer` signale le seul échec qui compte, et
      // `traiter` rappelera juste après. Une grammaire qui refuse un fragment
      // rend le texte brut, jamais ne fait tomber le message entier.
      return null;
    }
  }

  return {
    name: "shiki" as const,
    type: "code-highlighter" as const,

    getThemes: () => themes,
    getSupportedLanguages: () => [
      ...new Set([...PAR_ID.keys(), ...PAR_ALIAS.keys(), ...COMPLEMENT_PAR_ID.keys()]),
    ],
    supportsLanguage: (language: string) => normalizeLanguage(language) !== null,

    /**
     * Renvoie `null` tant que la grammaire n'est pas prête — c'est le contrat
     * de streamdown, qui attend le résultat asynchrone via `callback`. On ne
     * rend donc jamais une couleur approchée en attendant : le bloc est sans
     * couleur, puis se colore.
     *
     * `isIncomplete` : un fence encore en cours sera supersédé au token
     * suivant ; on ne le signale donc pas comme définitif.
     */
    highlight(
      args: {
        code: string;
        language: string;
        themes: [ThemeInput, ThemeInput];
        isIncomplete?: boolean;
      },
      callback?: (r: TokensResult) => void,
    ): TokensResult | null {
      const nom = normalizeLanguage(args.language);

      // EXIT EARLY : une langue qu'on ne connaît pas n'est pas une erreur,
      // c'est un `brainfuck` ou un `forth` écrit par le modèle. On rend le
      // texte brut en monospace — un manque, pas un mensonge.
      //
      // Ce retour doit rester AVANT `assurer()` : il n'existe pas de grammaire
      // `text` dans le paquet, donc tenter de la charger leverait une
      // `TypeError` à chaque fence inconnue, et le `console.warn` de `assurer`
      // se mettrait à crier sur une grammaire qu'on ne demandait pas. D'ailleurs
      // shiki court-circuite de son côté les langages plains (`text`, `txt`,
      // `plain`…) sans grammatical : rien à charger.
      //
      // streamdown garde alors son repli : un jeton par ligne, sans couleur.
      if (!nom) return null;

      const traiter = (h: HighlighterCore) => {
        const r = tokeniser(h, args.code, nom);
        if (r) callback?.(r);
      };

      if (chargeur) {
        void assurer(chargeur, nom).then(() => traiter(chargeur!));
        return tokeniser(chargeur, args.code, nom);
      }

      enAttente.add((h) => void assurer(h, nom).then(() => traiter(h)));
      void obtenir();
      return null;
    },
  };
}