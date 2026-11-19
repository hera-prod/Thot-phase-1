// Markdown des messages de chat — le seul endroit où du texte écrit par un
// modèle devient du HTML.
//
// Pourquoi `streamdown` et pas `react-markdown` : le streaming concatente le
// texte TOKEN PAR TOKEN (`onAnswer`, ChatConversation.tsx). react-markdown
// reparse le message entier à chaque token. streamdown est écrit pour ça :
// `mode="streaming"` découpe en blocs et `parseIncompleteMarkdown` répare un
// bloc encore ouvert (un fence ``` non refermé) au lieu d'attendre.
import { Component, type ErrorInfo, type ReactNode, memo } from "react";
import { Block, Streamdown, type BlockProps } from "streamdown";
import { createCodePlugin } from "./codePlugin";
import "./markdown.css";

// Deux thèmes sont obligatoires : le tuple est (light, dark) et le plugin
// tokenise en DEUX passes. Le projet n'a pas de thème sombre — le défaut est
// donc le thème clair, appliqué deux fois. C'est un coût ×2 assumé, mesuré en
// 5.2, et non un oubli.
//
// On passe par `createCodePlugin` et non par la prop `shikiTheme` : le dist de
// streamdown fait `shikiTheme = prop ?? plugins.code?.getThemes()`, donc le
// plugin reste la source de vérité du thème.
const pluginCode = createCodePlugin();
const PLUGINS = { code: pluginCode };

// Une URL d'image écrite par le modèle est une requête réseau. Cette app est
// locale : on ne garde que ce qui est déjà en mémoire. `src` ABSENT plutôt que
// vide — une `src=""` ferait recharger la page elle-même.
function srcDImage(src: string | undefined): string | undefined {
  if (!src) return undefined;
  if (src.startsWith("blob:")) return src;
  if (src.startsWith("data:image/")) return src;
  return undefined;
}

const COMPOSANTS = {
  img({ src, alt, ...rest }: { src?: string; alt?: string }) {
    const sur = srcDImage(src);
    // Sans `src` le navigateur affiche une image cassée : on le dit en texte,
    // sinon la réponse Mentit sur son propre contenu.
    if (!sur) {
      return (
        <span className="mdImageRetiree" title={src}>
          Image distante non affichée
        </span>
      );
    }
    return <img src={sur} alt={alt ?? ""} loading="lazy" {...rest} />;
  },
} as const;

// `Component` avec état d'erreur : un bloc qui plante ne doit pas emporter le
// message entier. Le repli est le SOURCE du bloc, en texte — le lecteur garde
// le contenu, ce qui est la seule chose qui compte ici.
class FrontiereParBloc extends Component<
  { children: ReactNode; contenu: string },
  { casse: boolean }
> {
  state = { casse: false };

  static getDerivedStateFromError() {
    return { casse: true };
  }

  componentDidCatch(_e: Error, _i: ErrorInfo) {
    // Volontairement sans retry : React comme le module map du navigateur
    // memorisent un import en echec, reessayer ne rendrait rien.
  }

  render() {
    if (this.state.casse) {
      return <pre className="mdBlocEnPanne">{this.props.contenu}</pre>;
    }
    return this.props.children;
  }
}

const BlocEnveloppe = memo(function BlocEnveloppe(props: BlockProps) {
  return (
    <FrontiereParBloc contenu={props.content}>
      {/* `Block` et NON `<Streamdown>` : c'est le composant qui rend UN bloc.
          Passer `<Streamdown {...props} />` ici rend un conteneur vide en mode
          streaming — vérifié, 3584 car. de HTML sans, 0 avec. La référence
          unsloth fait de même (`<Block {...blockProps} />`). */}
      <Block {...props} />
    </FrontiereParBloc>
  );
});

export interface MarkdownProps {
  children: string;
  /**
   * `true` pendant la generation du message. Decide `"streaming"` contre
   * `"static"` — nom explicite parce que le defaut de la bibliotheque est
   * `"streaming"`, et qu'un historique rerenderise en mode live bouffe pour
   * rien.
   */
  enCours?: boolean;
}

export const Markdown = memo(function Markdown({
  children,
  enCours = false,
}: MarkdownProps) {
  return (
    <Streamdown
      // `mdBody` sert de crochet de PORTEE pour la prose et les tableaux.
      // Jamais d'y mettre une classe portante : streamdown passe la chaine
      // dans `tailwind-merge`, qui peut macher un token qui ressemble a un
      // utilitaire. Tout ce qui est critique est regle sur `[data-streamdown]`.
      className="mdBody"
      mode={enCours ? "streaming" : "static"}
      plugins={PLUGINS}
      BlockComponent={BlocEnveloppe}
      // Cinq defauts qu'on veut etre certain d'avoir eteints. `controls` et
      // `codeBlockMaxHeight` sont les deux qui casseraient l'ecran : le premier
      // ajoute un second bouton "Copier" sur chaque bloc, le second impose un
      // scroll vertical interne a 400px. `caret` n'a aucun defaut : ne pas le
      // passer, et surtout pas `caret={false}` qui n'est pas un `keyof carets`.
      controls={false}
      lineNumbers={false}
      linkSafety={{ enabled: false }}
      codeBlockMaxHeight={0}
      tableMaxHeight={0}
      // `caret` n'a aucun defaut : ne pas le passer.
      components={COMPOSANTS}
    >
      {children}
    </Streamdown>
  );
});