// Porte de fumée du rendu Markdown — AVANT que quoi que ce soit ne soit
// branché sur le chat.
//
// Un test « la page rend » passerait avec un tableau en texte brut, un fence
// affiche avec ses accents graves, et une coloration morte. Ces tests
// regardent donc la STRUCTURE, pas le nombre de pixels.
//
//   - `highlight()` renvoie `null` au premier appel et livre via callback.
//     D'ou `findBy*` partout.
//
// TROIS ECHECS REELS DE CETTE PORTE, pour qu'on ne la sur-grade pas :
//   1. « le style contient --sdm-c » passait alors que PERSONNE ne lisait la
//      variable : la classe qui la consomme, `text-[var(--sdm-c)]`, n'etait pas
//      compilee. Une variable posee n'est pas une couleur affichee.
//   2. Aucun test ne verifiait que les lignes de code s'affichaient sur des
//      lignes distinctes — `block` etait un utilitaire Tailwind non compile.
//   3. Meme en important global.css, vitest n'emet PAS les utilitaires
//      Tailwind : jsdom ne peut donc pas valider cette couche. D'ou les gardes
//      structurelles plus bas, qui verifient la CHAINe, pas le rendu.
/// <reference types="vite/client" />
import viteConfig from "../../frontend/vite.config.ts?raw";

// Tous les sources du front, en texte. `import.meta.glob` plutôt que `fs` :
// même transformation que le build, donc on lit exactement ce que Vite lit.
const SOURCES = import.meta.glob("../../frontend/src/**/*.{ts,tsx}", {
  eager: true,
  query: "?raw",
  import: "default",
}) as Record<string, string>;
import globalCss from "../../frontend/src/styles/global.css?raw";
import tailwindCss from "../../frontend/src/styles/tailwind.css?raw";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { Markdown } from "@/components/chat/Markdown";
import { createCodePlugin } from "@/components/chat/codePlugin";

const pluginCode = createCodePlugin();

afterEach(cleanup);

const TABLEAU = [
  "| Espace | Discussions | Depot |",
  "| ------- | ----------- | ----- |",
  "| Alpha   | 3           | 12    |",
  "| Beta    | 7           | 4     |",
].join("\n");

const FENCE = "```json\n{ \"workspace_id\": \"ws_1\" }\n```";

describe("Markdown — le bug signale", () => {
  it("rend un VRAI tableau, pas des barres verticales", async () => {
    const { container } = render(<Markdown>{TABLEAU}</Markdown>);
    await waitFor(() => expect(container.querySelector("table")).not.toBeNull());

    const table = container.querySelector("table")!;
    // Les lignes de separation `| --- |` ne doivent PAS devenir du contenu.
    expect(container.textContent).not.toContain("| ---");
    expect(container.textContent).not.toContain("|");
    expect(table.querySelectorAll("th")).toHaveLength(3);
    expect(table.querySelectorAll("tbody td")).toHaveLength(6);
    expect(screen.getByRole("columnheader", { name: "Espace" })).toBeTruthy();
    expect(screen.getByRole("cell", { name: "Beta" })).toBeTruthy();
  });

  it("rend un VRAI bloc de code, pas des accents graves", async () => {
    const { container } = render(<Markdown>{FENCE}</Markdown>);
    const bloc = await waitFor(() => {
      const el = container.querySelector('[data-streamdown="code-block"]');
      expect(el).not.toBeNull();
      return el!;
    });

    // Les accents graves sont la preuve que le Markdown n'a PAS ete parse.
    expect(container.textContent).not.toContain("```");
    expect(container.textContent).toContain("workspace_id");
    // Le nom de langage est transporte et lisible.
    expect(container.querySelector('[data-language="json"]')).not.toBeNull();
  });

  it("donne au code un conteneur defilable, pas une bulle elargie", async () => {
    const long = "```\n" + "x".repeat(600) + "\n```";
    const { container } = render(<Markdown>{long}</Markdown>);
    const bloc = await waitFor(() => {
      const el = container.querySelector('[data-streamdown="code-block"]');
      expect(el).not.toBeNull();
      return el!;
    });
    // `min-width: 0` sans quoi un parent flex refuse de retrecir : c'est lui
    // qui empeche la bulle de s'elargir au lieu de faire defiler.
    expect(getComputedStyle(bloc).minWidth).not.toBe("");
  });

  it("garde le code inline INLINE — pas transforme en bloc", async () => {
    const { container } = render(<Markdown>Appelez `parseIncompleteMarkdown` d'abord."</Markdown>);
    const inline = await waitFor(() => {
      const el = container.querySelector('[data-streamdown="inline-code"]')
        ?? container.querySelector("p code");
      expect(el).not.toBeNull();
      return el!;
    });
    expect(inline.textContent).toBe("parseIncompleteMarkdown");
    // Un code inline ne doit pas avoir cree de bloc de code.
    expect(container.querySelector('[data-streamdown="code-block"]')).toBeNull();
  });
});

describe("Markdown — streaming", () => {
  it("rend un tableau EN COURS de stream, incomplet", async () => {
    // Le cas qui a declenche la demande : un modele qui remplit un tableau
    // ligne par ligne ne doit pas clignoter en texte brut entre deux lignes.
    const partiel = "| Espace | Depot |\n| ------- | ----- |\n| Alpha | 12";
    const { container } = render(<Markdown enCours>{partiel}</Markdown>);
    const table = await waitFor(() => {
      const el = container.querySelector("table");
      expect(el).not.toBeNull();
      return el!;
    });
    expect(table.querySelectorAll("th")).toHaveLength(2);
    expect(container.textContent).toContain("Alpha");
  });

  it("rend un fence NON TERMINE au lieu de disparaitre", async () => {
    const ouvert = "Voici :\n\n```python\ndef f():\n    return 1\n";
    const { container } = render(<Markdown enCours>{ouvert}</Markdown>);
    await waitFor(() =>
      expect(container.querySelector('[data-streamdown="code-block"]')).not.toBeNull(),
    );
    expect(container.textContent).toContain("def f");
    expect(container.textContent).not.toContain("```");
  });

  it("passe le bloc en cours en mode streaming, l'historique en mode static", () => {
    const { rerender, container } = render(<Markdown>un tableau\n\n---\n\nb</Markdown>);
    rerender(<Markdown enCours>un tableau\n\n---\n\nb</Markdown>);
    // Le mode n'est pas observable dans le DOM : on verifie au moins que les
    // deux rendus montent sans erreur et produisent le meme contenu.
    expect(container.querySelectorAll("p").length).toBeGreaterThan(0);
  });
});

describe("Markdown — securite", () => {
  // Ces trois tests decrivent un comportement PLUS STRICT que ce que le plan
  // prevoyait. streamdown bloque deja les images `data:` a froid, et retire un
  // lien dangereux du DOM entier plutot que de le vider. Les assertions
  // disent ce qui est vrai, pas ce qui etait annonce.

  it("n'introduit AUCUN gestionnaire d'evenement", async () => {
    const { container } = render(
      <Markdown>{'<img src="x" onerror="alert(1)"> et <script>alert(2)</script> et <div onclick="a()">t</div>'}</Markdown>,
    );
    await waitFor(() => expect(container.querySelector(".mdBody")?.textContent).toBeTruthy());

    // rehype-sanitize garde `src` et retire `onerror`; ici streamdown bloque
    // l'image en amont. La verite utile : AUCUN attribut `on*` dans l'arbre.
    const avecHandler = [...container.querySelectorAll("*")].filter((el) =>
      [...el.attributes].some((a) => /^on/i.test(a.name)),
    );
    expect(avecHandler).toEqual([]);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("[onclick]")).toBeNull();
  });

  it("neutralise un lien javascript: — il ne devient meme pas une ancre", async () => {
    const { container } = render(<Markdown>[clic](javascript:alert(1))</Markdown>);
    await waitFor(() => expect(container.querySelector(".mdBody")?.textContent).toContain("clic"));
    const ancres = [...container.querySelectorAll("a")];
    expect(ancres).toEqual([]);
    expect(container.innerHTML).not.toContain("javascript:");
  });

  it("donne aux liens https ce qu'attend le handler d'Electron", async () => {
    // `electron/main.cjs` n'ouvre en externe QUE les `https://`, et rend
    // `{action:'deny'}` pour tout le reste. Sans `target="_blank"` + `rel`,
    // un lien du modele est simplement avale.
    const { container } = render(<Markdown>[site](https://exemple.test/page)</Markdown>);
    const a = await waitFor(() => {
      const el = container.querySelector("a");
      expect(el).not.toBeNull();
      return el!;
    });
    expect(a.getAttribute("target")).toBe("_blank");
    expect(a.getAttribute("rel")).toBe("noopener noreferrer");
    expect(a.getAttribute("href")).toContain("https://");
  });

  it("ne charge AUCUNE image — ni distante, ni meme en memoire", async () => {
    const pixel =
      "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
    const { container } = render(<Markdown>{`![p](https://exemple.test/x.png) ![p](${pixel})`}</Markdown>);
    await waitFor(() =>
      expect(container.textContent).toContain("Image distante non affichée"),
    );
    // Aucune requete sortante : c'etait la decision. streamdown bloque le
    // `data:` en amont, notre composant rattrape le distant.
    expect(container.querySelector("img")).toBeNull();
  });
});

describe("Markdown — degrades proprement", () => {
  it("ne rend pas vide une reponse en texte seul", async () => {
    const { container } = render(<Markdown>Une phrase, sans rien de special.</Markdown>);
    await waitFor(() => expect(container.textContent).toContain("Une phrase"));
    expect(container.querySelector("p")).not.toBeNull();
  });

  it("survit a du HTML brut, de l'echappement et des accents graves isoles", async () => {
    for (const source of [
      "<div onclick='x()'>html</div>",
      "5 < 6 et 7 > 2",
      "```",
      "``",
      "``` non ferme",
      "| | |",
      "[",
      "![",
    ]) {
      const { container, unmount } = render(<Markdown>{source}</Markdown>);
      await waitFor(() => expect(container.textContent.length).toBeGreaterThanOrEqual(0));
      expect(container.querySelector("[onclick]")).toBeNull();
      unmount();
    }
  });
});
describe("Markdown — la coloration est réellement branchée", () => {
  // Ces tests ne peuvent PAS vérifier une couleur affichée : vitest n'émet pas
  // les utilitaires Tailwind, donc jsdom ne connaît pas
  // `text-[var(--sdm-c)]`. Ils vérifient donc le CONTRAT dont cette classe
  // dépend — et les gardes structurelles plus bas prouvent que Tailwind la
  // compilera. Ensemble, les deuxivalent ce que la porte aval ne voyait pas.

  // LE test qui manquait, et qui change tout.
  //
  // Les versions précédentes vérifiaient que `--sdm-c` était POSÉ dans le style
  // du jeton. Or le repli de streamdown écrit `--sdm-c: inherit` — la variable
  // est là, l'assertion passe, et l'application n'affiche aucune couleur. 182
  // tests verts sur un build cassé : c'est ce que donnait cette porte.
  //
  // On vérifie donc la VALEUR : une vraie coloration produit plusieurs couleurs
  // hex distinctes, un repli n'en produit qu'une — et c'est la chaîne
  // `inherit`, qui n'est pas une couleur.
  //
  // `waitFor` est obligatoire : le premier `highlight()` renvoie `null` par
  // conception, les couleurs arrivent par callback après le chargement du
  // chunk de grammaire. Une lecture synchrone lirait le repli et échouerait à
  // chaque fois, pour la mauvaise raison.
  it("produit de VRAIES couleurs, pas un heritage", async () => {
    const { container } = render(
      <Markdown>{"```python\ndef greet(nom):\n    return 'Bonjour ' + nom\n```"}</Markdown>,
    );
    const bloc = await waitFor(() => {
      const el = container.querySelector('[data-streamdown="code-block"]');
      expect(el).not.toBeNull();
      return el!;
    });

    const couleurs = await waitFor(() => {
      // `--sdm-c` et RIEN D'AUTRE : `fg` et `bg` reviennent chez shiki en
      // chaînes composées du genre "transparent;--shiki-dark-bg:transparent",
      // que le parsing ci-dessous lirait comme une couleur.
      const trouvees = [...bloc.querySelectorAll("span")]
        .map((s) => /--sdm-c:\s*([^;"]+)/.exec(s.getAttribute("style") ?? "")?.[1])
        .filter((v): v is string => typeof v === "string")
        // Le hex est mis en minuscules par shiki : la regex doit être
        // insensible à la casse, sinon on ne trouve rien.
        .filter((v) => /^#[0-9a-f]{3,8}$/i.test(v.trim()));
      expect(trouvees.length).toBeGreaterThan(0);
      return [...new Set(trouvees.map((v) => v.trim().toLowerCase()))];
    });

    // Un mot-clé ET une chaîne : mesuré, ce snippet donne 4 couleurs. Un
    // snippet commentaire-seul ou chaîne-seule n'en donne qu'une, d'où
    // l'exigence de deux.
    expect(couleurs.length).toBeGreaterThanOrEqual(2);
    // Et surtout : jamais la chaîne `inherit`, qui est la signature du repli.
    expect(couleurs).not.toContain("inherit");

    // Le contrat de la classe consommatrice, qu'on vérifiait déjà. C'est elle
    // qui manquait à la première, avant Tailwind.
    const token = bloc.querySelector('span[style*="--sdm-c"]')!;
    expect(token.getAttribute("class")).toContain("text-[var(--sdm-c");

    expect(
      container.querySelector('[data-streamdown="code-block-header"]')!.textContent,
    ).toBe("python");
  });

  it("donne une classe display:block a CHAQUE ligne de code", async () => {
    const { container } = render(
      <Markdown>{"```python\ndef f():\n    return 1\nx = 2\n```"}</Markdown>,
    );
    const code = await waitFor(() => {
      const el = container.querySelector('[data-streamdown="code-block-body"] code');
      expect(el).not.toBeNull();
      return el!;
    });
    const lignes = [...code.children];
    // Trois lignes de code, donc trois spans. Shiki n'ajoute pas de span pour
    // le saut de ligne final.
    expect(lignes.length).toBe(3);
    // Sans `block`, ces spans sont des `inline` et les quatre lignes se
    // replient sur une seule. C'était exactement le symptôme « sauts de
    // lignes manquants ».
    for (const l of lignes) expect(l.className).toContain("block");
  });

  it("retombe en monospace SANS couleur pour une langue inconnue", async () => {
    // Une langue qu'on n'a pas n'est pas un défaut : c'est un `brainfuck` ou un
    // `forth` écrit par le modèle. Un manque, pas un mensonge.
    //
    // ⚠ PAS `kotlin` ici : depuis le passage au registre de shiki, `kotlin` EST
    // une grammaire disponible et se colore. J'avais écrit ce test avec
    // `kotlin`, il serait devenu faux sans que rien ne le dise. Les deux tags
    // ci-dessous sont vérifiés ABSENTS du registre.
    const { container } = render(
      <Markdown>{"```brainfuck\n+++++[>++++<-]\n```"}</Markdown>,
    );
    const bloc = await waitFor(() => {
      const el = container.querySelector('[data-streamdown="code-block"]');
      expect(el).not.toBeNull();
      return el!;
    });
    await waitFor(() => expect(bloc.textContent).toContain("+++++"));
    expect(bloc.textContent).toContain("brainfuck");

    // Et surtout, ça ne doit PAS crier. Il n'existe pas de grammaire `text`
    // dans le paquet : si l'exit early de `codePlugin.ts` n'était pas AVANT
    // le chargement, cet appel lèverait une TypeError et le `console.warn`
    // se déclencherait sur chaque fence inconnue. On l'attrape ici.
    const warns: unknown[][] = [];
    const avant = console.warn;
    console.warn = (...a: unknown[]) => void warns.push(a);
    try {
      for (const [tag, corps] of [
        ["forth", ": square dup * ;"],
        ["brainfuck", "+++++[>++++<-]"],
      ]) {
        const { unmount } = render(
          <Markdown>{"```" + tag + "\n" + corps + "\n```"}</Markdown>,
        );
        await new Promise((r) => setTimeout(r, 400));
        unmount();
      }
    } finally {
      console.warn = avant;
    }
    expect(warns).toEqual([]);
  });

  it("ne crie pas sur un tag qui est une cle de prototype", async () => {
    // BUG VÉCU, deux fois dans le même fichier.
    //
    // 1. `ALIAS["constructor"]` sur un OBJET LITTÉRAL renvoie le constructeur
    //    `Object` — truthy. Le test « cette langue est-elle inconnue ? »
    //    passait donc pour `constructor`, `__proto__`, `toString`, `valueOf`,
    //    et le chargement levait une `TypeError` suivie d'un `console.warn` À
    //    CHAQUE RENDU, sans fin.
    // 2. Même piège avec `in`, qui parcourt la chaîne de prototypes. Et
    //    `Object.keys(map)` rend `[]` — un contrat silencieusement faux.
    //
    // Il y a DEUX défenses, et ce test doit les couvrir toutes les deux :
    //   - les index sont des `Map` (`get`/`has` ne remontent pas au prototype) ;
    //   - `assurer` sort sur `if (!entree) return` si l'id n'est pas dans le
    //     registre, donc même un index cassé ne.productrait plus de TypeError.
    //
    // D'où les deux assertions : pas d'avertissement, ET pas un seul jeton
    // coloré. Un tag de prototype doit se comporter EXACTEMENT comme une
    // langue inconnue — c'est le contrat, pas l'absence de symptôme.
    const warns: unknown[][] = [];
    const avant = console.warn;
    console.warn = (...a: unknown[]) => void warns.push(a);
    try {
      for (const tag of ["constructor", "__proto__", "toString", "valueOf"]) {
        const { container, unmount } = render(
          <Markdown>{"```" + tag + "\na = 1\n```"}</Markdown>,
        );
        const bloc = await waitFor(() => {
          const el = container.querySelector('[data-streamdown="code-block"]');
          expect(el).not.toBeNull();
          return el!;
        });
        await new Promise((r) => setTimeout(r, 350));
        const hex = [...bloc.querySelectorAll("span")]
          .map((sp) => /--sdm-c:\s*([^;"]+)/.exec(sp.getAttribute("style") ?? "")?.[1])
          .filter((v): v is string => typeof v === "string")
          .filter((v) => /^#[0-9a-f]{3,8}$/i.test(v.trim()));
        expect(hex, `le tag « ${tag} » ne doit produire aucun jeton coloré`).toEqual([]);
        unmount();
      }
    } finally {
      console.warn = avant;
    }
    expect(warns).toEqual([]);
  });
});

describe("Markdown — la normalisation des langages", () => {
  // On n'affirme QUE les alias que nous posséditions nous-mêmes. Tester `c#` ou
  // `py` testerait le registre de shiki, pas notre code : ces assertions ne
  // peuvent pas échouer, donc elles ne prouvent rien.

  const couleurs = (bloc: Element) =>
    [...bloc.querySelectorAll("span")]
      .map((s) => /--sdm-c:\s*([^;"]+)/.exec(s.getAttribute("style") ?? "")?.[1])
      .filter((v): v is string => typeof v === "string")
      .filter((v) => /^#[0-9a-f]{3,8}$/i.test(v.trim()));

  it.each([
    ["golang", "go", "package main\nfunc main() {}"],
    ["obj-c", "objective-c", "@interface Foo\n@end"],
    ["rsx", "tsx", "const a = <div/>;"],
    ["svg", "xml", "<svg><rect/></svg>"],
  ])("resout %s vers %s et le colore", async (tag, _cible, corps) => {
    // `svg` est le plus intéressant : c'est le SEUL tag que notre ancienne
    // table résolvait et que le registre ne résout pas. Sans la ligne
    // complémentaire, tous les fences `svg` repasseraient en monospace, en
    // silence — une régression qu'aucun autre test n'aurait vue.
    const { container } = render(<Markdown>{"```" + tag + "\n" + corps + "\n```"}</Markdown>);
    const bloc = await waitFor(() => {
      const el = container.querySelector('[data-streamdown="code-block"]');
      expect(el).not.toBeNull();
      return el!;
    });
    const trouvees = await waitFor(() => {
      const c = couleurs(bloc);
      expect(c.length).toBeGreaterThan(0);
      return c;
    });
    expect(new Set(trouvees.map((c) => c.toLowerCase())).size).toBeGreaterThanOrEqual(2);
  });

  it("l'invariant de theme tient : le meme objet des deux cotes", () => {
    // shiki enregistre les thèmes par `name`, et `@shikijs/primitive`
    // reconstruit l'état par `Object.fromEntries(themedTokens.map(s =>
    // [s.theme, s.state?.…]))`. Deux thèmes de même nom partagent donc un seul
    // état, et la seconde passe est SILENCIEUSEMENT JETÉE : aucune erreur, la
    // mauvaise couleur. C'est pour ça que `creerChargeur` reçoit le même objet
    // deux fois, et pas deux fois le même thème.
    expect(pluginCode.getThemes()[0]).toBe(pluginCode.getThemes()[1]);
  });
});

describe("Markdown — la chaîne CSS ne peut pas être débranchée en silence", () => {
  // LE test qui manquait. Ni DOM ni CSS : on vérifie que la chaîne qui fait
  // fonctionner la coloration et les sauts de ligne est branchée. Si quelqu'un
  // retire le plugin Vite, l'import, ou une mapping de couleur, ça échoue ici —
  // alors que l'app, elle, continuerait de marcher jusqu'au premier
  // tableau ou premier bloc de code.

  // Lus via `?raw` : le texte exact que le build voit, sans supposer le cwd.

  it("vite enregistre le plugin Tailwind", () => {
    expect(viteConfig).toContain("@tailwindcss/vite");
  });

  it("global.css importe la feuille Tailwind", () => {
    expect(globalCss).toContain("./tailwind.css");
  });

  it("prend les utilitaires SANS le preflight", () => {
    const css = tailwindCss;
    // Le preflight réinitialiserait les 8 mois de CSS écrit à la main.
    expect(css).not.toMatch(/@import\s+["']tailwindcss["']/);
    expect(css).toContain("utilities.css");
  });

  it("n'utilise aucun import() de paquet par gabarit, dans TOUT le front", () => {
    // La faute était dans UN fichier. Mais une faute de cette classe dans un
    // fichier personne ne pense à ouvrir est invisible aussi longtemps qu'elle
    // reste hypothétique. D'où la largeur : tout `src`, pas `codePlugin.ts`.
    //
    // `vite.config.ts` porte le même contrôle sur le bundle lui-même, ce qui
    // est plus solide — ce test attrape le motif avant qu'il n'atteigne un
    // chunk, y compris pendant une session de dev.
    // On ne teste que le CODE, commentaires retirés. `codePlugin.ts` documente
    // le motif fautif dans son en-tête, et il faut pouvoir le faire : une garde
    // qui interdit d'écrire pourquoi on a corrigé quelque chose est une garde
    // qu'on supprime au bout d'une semaine.
    //
    // Limite assumée : retirer les commentaires à la main coupe aussi une URL
    // « https://… » en fin de ligne. Un `import(` par gabarit placé APRÈS une
    // URL sur la même ligne passerait donc. Assez improbable pour ne pas worth
    // un vrai parseur ; assez réel pour être écrit ici.
    const sansCommentaires = (code: string) =>
      code.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");

    const coupables: string[] = [];
    for (const [chemin, contenu] of Object.entries(SOURCES)) {
      for (const ligne of sansCommentaires(contenu).split("\n")) {
        if (/import\(\s*`/.test(ligne)) {
          coupables.push(`${chemin.replace(/.*frontend\//, "")}: ${ligne.trim()}`);
        }
      }
    }
    expect(coupables).toEqual([]);
  });

  it("scanne le dist de streamdown, sinon ses classes sont inconnues", () => {
    expect(tailwindCss).toContain("node_modules/streamdown/dist");
  });

  it("mappe TOUTES les couleurs shadcn que streamdown utilise", () => {
    // Vérifié dans le DOM réel de streamdown : bg-background, bg-muted,
    // bg-sidebar, border-border, divide-border, text-muted-foreground,
    // text-primary. Sans mapping, Tailwind n'émet AUCUN de ces utilitaires —
    // d'où un tableau sans filet et une citation sans barre.
    const utilisees = [
      "background", "muted", "sidebar", "border", "muted-foreground", "primary",
    ];
    const theme = tailwindCss;
    for (const c of utilisees) {
      expect(theme).toContain(`--color-${c}:`);
    }
    // Et chacune doit pointer sur un token Thot, pas sur une couleur en dur :
    // une couleur en dur在这里 est une couleur que le thème ne peut pas changer.
    expect(theme).not.toMatch(/--color-[a-z-]+:\s*#[0-9a-f]{3,8}/i);
  });
});
