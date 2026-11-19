// Régression du défaut « l'image générée n'apparaît jamais dans la bulle assistant » et de
// l'absence de progression pendant une génération de plusieurs minutes.
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import {
  configurerPartage,
  configurerTitreConversation,
  conversationTurns,
  deferAttachmentBytes,
  deferConversationTitle,
  deferHistory,
  failHistory,
  failConversationRename,
  fireChatEvent,
  invocationsOf,
  pendingByteRequests,
  pendingHistoryRequests,
  pendingConversationRequests,
  renderChatConversation,
  renderChatConversationWithNavigation,
  resetChat,
  resolvePendingBytes,
  resolvePendingHistory,
  resolvePendingConversation,
  toastsEmis,
  turnIndexOf,
  waitForChatListeners,
  type ChatHistoryMessage,
} from './chatHarness';

const HISTORY: ChatHistoryMessage[] = [
  { id: 'u1', role: 'user', content: 'Dessine un phare breton' },
  { id: 'a1', role: 'assistant', content: 'Voici le phare.' },
];

const PROGRESS_LABEL = 'Progression de la génération';

beforeEach(() => {
  resetChat();
});

afterEach(() => {
  cleanup();
});

describe('Conversation — image générée dans la bulle assistant', () => {
  it('affiche dans le document l’image publiée par artifact_created', async () => {
    const { container } = renderChatConversation({ history: HISTORY });
    await screen.findByText('Voici le phare.');

    fireChatEvent('artifact_created', { request_id: 'run-1', run_id: 'run-1', artifact_id: 'art_phare_1' });

    const image = await screen.findByRole('img', { name: 'Image générée art_phare_1' });
    expect(image).toHaveAttribute('src', expect.stringContaining('blob:thot-'));
    expect(screen.getByText('art_phare_1')).toBeTruthy();
    const turns = conversationTurns(container);
    expect(turns[1]).toContainElement(image);
    expect(turns[0]).not.toContainElement(image);
  });

  it('recharge la même image au retour dans la conversation', async () => {
    renderChatConversation({
      history: [
        HISTORY[0],
        { id: 'a1', role: 'assistant', content: 'Voici le phare.', attachments: [{ id: 'art_phare_1', kind: 'image', name: 'phare.png', mime: 'image/png' }] },
      ],
    });

    const image = await screen.findByRole('img', { name: 'Image générée art_phare_1' });
    expect(image).toHaveAttribute('src', expect.stringContaining('blob:thot-'));
    expect(screen.getByText('art_phare_1')).toBeTruthy();
  });

  it('rattache l’image au tour capturé quand un nouveau tour démarre pendant le chargement', async () => {
    deferAttachmentBytes();
    const { container } = renderChatConversation({ history: HISTORY });
    await screen.findByText('Voici le phare.');

    fireChatEvent('artifact_created', { request_id: 'run-1', run_id: 'run-1', artifact_id: 'art_phare_1' });
    await waitFor(() => expect(pendingByteRequests()).toBe(1));

    fireEvent.change(screen.getByPlaceholderText('Ecrivez votre message...'), { target: { value: 'Et un second essai ?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));
    await screen.findByText('Et un second essai ?');
    expect(conversationTurns(container)).toHaveLength(4);

    await resolvePendingBytes();

    const image = await screen.findByRole('img', { name: 'Image générée art_phare_1' });
    expect(turnIndexOf(container, image)).toBe(1);
  });

  it('n’insère rien quand le message ne porte aucune image', async () => {
    const { container } = renderChatConversation({ history: HISTORY });
    await screen.findByText('Voici le phare.');
    expect(container.querySelector('.genimg')).toBeNull();
  });
});

describe('Conversation — chargement différé de l’historique', () => {
  it('conserve un message envoyé pendant le chargement', async () => {
    deferHistory();
    renderChatConversation();

    await screen.findByText('Chargement de la discussion…');
    expect(pendingHistoryRequests()).toBe(1);

    fireEvent.change(screen.getByPlaceholderText('Ecrivez votre message...'), { target: { value: 'Message pendant le chargement' } });
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));
    await screen.findByText('Message pendant le chargement');

    await resolvePendingHistory([]);

    await waitFor(() => expect(screen.queryByText('Chargement de la discussion…')).toBeNull());
    expect(screen.getByText('Message pendant le chargement')).toBeTruthy();
  });
});

describe('Conversation — historique illisible', () => {
  it('désactive « Réessayer » pendant un run', async () => {
    failHistory('base verrouillée');
    renderChatConversation({ history: HISTORY });

    await screen.findByRole('alert');
    await waitForChatListeners('run_started');
    fireChatEvent('run_started', {
      request_id: 'run-en-cours',
      run_id: 'run-en-cours',
      conversation_id: 'conv-thot-1',
    });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Réessayer' })).toHaveProperty('disabled', true);
    });
  });

  it('affiche un bandeau et Réessayer ; le second appel réussit et montre les messages', async () => {
    failHistory('base verrouillée');
    renderChatConversation({ history: HISTORY });

    await screen.findByRole('alert');
    expect(screen.getByText(/L’historique de cette discussion n’a pas pu être chargé/)).toBeTruthy();
    expect(screen.getByText('base verrouillée')).toBeTruthy();
    // Surtout pas l'invitation à démarrer une conversation : les messages existent.
    expect(screen.queryByText(/Commencez une nouvelle conversation/)).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Réessayer' }));

    await screen.findByText('Voici le phare.');
    expect(screen.getByText('Dessine un phare breton')).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });
});

describe('Conversation — verser l’image générée au dépôt de l’espace', () => {
  it('appelle la promotion avec la discussion ET l’artefact, puis confirme', async () => {
    const { container } = renderChatConversation({ history: HISTORY });
    await screen.findByText('Voici le phare.');
    fireChatEvent('artifact_created', { request_id: 'run-1', run_id: 'run-1', artifact_id: 'art_phare_1' });
    await screen.findByRole('img', { name: 'Image générée art_phare_1' });
    configurerPartage({ ok: true, link_id: 'lnk_1', created: true, artifact_id: 'art_phare_1' });

    fireEvent.click(screen.getByRole('button', { name: 'Verser au dépôt' }));
    // La cible n'est PAS transmise : le serveur la déduit de la discussion.
    await waitFor(() => {
      expect(invocationsOf('espace_conversation_partager')).toHaveLength(1);
    });
    expect(invocationsOf('espace_conversation_partager')[0]?.args).toEqual({
      conversationId: 'conv-thot-1', artifactId: 'art_phare_1',
    });
    // La confirmation vient du SERVICE, jamais d'une supposition : le
    // toast s'appuie sur la lecture réelle de la réponse.
    expect(toastsEmis()).toContainEqual(expect.objectContaining({
      type: 'success', title: 'Versé au dépôt',
    }));
    // Un lien déjà posé ne se repose pas : le bouton marque l'état, il ne
    // relance pas une écriture en vain.
    expect((screen.getByRole('button', { name: 'Au dépôt' }) as HTMLButtonElement).disabled).toBe(true);
    expect(container.querySelector('.genimg')).toBeTruthy();
  });

  it('présente le refus du serveur au lieu de le fabriquer', async () => {
    renderChatConversation({ history: HISTORY, locationState: null });
    await screen.findByText('Voici le phare.');
    fireChatEvent('artifact_created', { request_id: 'run-1', run_id: 'run-1', artifact_id: 'art_phare_1' });
    await screen.findByRole('img', { name: 'Image générée art_phare_1' });
    // Une discussion hors de tout espace : le serveur refuse, nommé.
    configurerPartage(Promise.reject(
      new Error('[CONVERSATION_SANS_ESPACE] Cette discussion n\'est rattachée à aucun espace.'),
    ));

    fireEvent.click(screen.getByRole('button', { name: 'Verser au dépôt' }));
    await waitFor(() => expect(invocationsOf('espace_conversation_partager')).toHaveLength(1));
    expect(toastsEmis()).toContainEqual(expect.objectContaining({
      type: 'error', title: 'Versement impossible',
      message: expect.stringMatching(/CONVERSATION_SANS_ESPACE/),
    }));
  });

  it('ne propose aucun versement tant que la réponse n\'a pas dit vrai', async () => {
    // Rien n'est configuré : le pont répond sans payload, et la carte refuse
    // d'annoncer « versé » sur un silence.
    renderChatConversation({ history: HISTORY });
    await screen.findByText('Voici le phare.');
    fireChatEvent('artifact_created', { request_id: 'run-1', run_id: 'run-1', artifact_id: 'art_phare_1' });
    await screen.findByRole('img', { name: 'Image générée art_phare_1' });

    fireEvent.click(screen.getByRole('button', { name: 'Verser au dépôt' }));
    await waitFor(() => expect(invocationsOf('espace_conversation_partager')).toHaveLength(1));
    expect(toastsEmis()).toContainEqual(expect.objectContaining({
      type: 'error', title: 'Versement impossible',
      message: expect.stringMatching(/n'a pas confirmé le versement/),
    }));
  });
});

describe('Conversation — progression du run image', () => {
  beforeEach(async () => {
    renderChatConversation();
    await waitForChatListeners('run_stage', 'message_completed', 'error');
  });

  /** Envoie un message et rend le request_id réellement émis : sans lui, la corrélation est inopérante. */
  async function startTurn(prompt = 'Dessine un phare breton'): Promise<string> {
    fireEvent.change(screen.getByPlaceholderText('Ecrivez votre message...'), { target: { value: prompt } });
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));
    const sends = invocationsOf('chat_send');
    return String((sends[sends.length - 1]?.args as { requestId: string }).requestId);
  }

  it('montre les étapes de diffusion puis retire la carte à la fin du tour', async () => {
    const requestId = await startTurn();

    fireChatEvent('run_stage', { request_id: requestId, run_id: requestId, stage: 'diffusion', status: 'Diffusion — pas 1/4', step: 1, total: 4 });
    const card = await screen.findByLabelText(PROGRESS_LABEL);
    expect(card).toHaveTextContent('Génération en cours');
    expect(card).toHaveTextContent('Diffusion — pas 1/4');
    expect(card.querySelectorAll('.img-segments i.fill')).toHaveLength(1);

    fireChatEvent('run_stage', { request_id: requestId, run_id: requestId, stage: 'publication', status: 'Enregistrement local de l’image' });
    await waitFor(() => expect(screen.getByLabelText(PROGRESS_LABEL)).toHaveTextContent('Enregistrement local de l’image'));

    fireChatEvent('message_completed', { request_id: requestId });
    await waitFor(() => expect(screen.queryByLabelText(PROGRESS_LABEL)).toBeNull());
  });

  it('retire la carte sur une erreur et sur un arrêt du tour', async () => {
    const requestId = await startTurn();
    fireChatEvent('run_stage', { request_id: requestId, run_id: requestId, stage: 'vae-chargement', status: 'Chargement du VAE' });
    await screen.findByLabelText(PROGRESS_LABEL);
    fireChatEvent('error', { request_id: requestId, code: 'IMAGE_FAILED', message: 'moteur arrêté' });
    await waitFor(() => expect(screen.queryByLabelText(PROGRESS_LABEL)).toBeNull());

    const nextRequestId = await startTurn('Et un second essai ?');
    fireChatEvent('run_stage', { request_id: nextRequestId, run_id: nextRequestId, stage: 'vae-chargement', status: 'Chargement du VAE' });
    await screen.findByLabelText(PROGRESS_LABEL);
    fireEvent.click(screen.getByRole('button', { name: 'Arrêter la génération' }));
    await waitFor(() => expect(screen.queryByLabelText(PROGRESS_LABEL)).toBeNull());
  });

  it('ignore la progression d’un autre request_id', async () => {
    const requestId = await startTurn();
    fireChatEvent('run_stage', { request_id: 'autre-run', run_id: 'autre-run', stage: 'diffusion', status: 'Diffusion' });
    expect(screen.queryByLabelText(PROGRESS_LABEL)).toBeNull();
    fireChatEvent('run_stage', { request_id: requestId, run_id: requestId, stage: 'diffusion', status: 'Diffusion' });
    await screen.findByLabelText(PROGRESS_LABEL);
  });

  it('traite llm_error comme un avertissement après run_started puis se libère sur run_failed', async () => {
    const requestId = await startTurn();

    fireChatEvent('run_started', { request_id: requestId, run_id: requestId, conversation_id: 'conv-thot-1' });
    fireChatEvent('error', { request_id: requestId, code: 'llm_error', message: 'générateur instable, reprise' });
    expect(screen.getByRole('button', { name: 'Arrêter la génération' })).toBeTruthy();

    fireChatEvent('run_failed', { request_id: requestId, run_id: requestId, code: 'empty_response', message: 'réponse vide' });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Arrêter la génération' })).toBeNull());
  });

  it('ignore le terminal d’un autre run sans libérer le tour local', async () => {
    await startTurn();

    fireChatEvent('run_completed', { request_id: 'autre-run', run_id: 'autre-run' });
    fireChatEvent('run_failed', { request_id: 'autre-run', run_id: 'autre-run', code: 'empty_response' });
    expect(screen.getByRole('button', { name: 'Arrêter la génération' })).toBeTruthy();
  });
});

describe('Conversation — sérialisation des inférences', () => {
  it('refuse l’envoi pendant une génération d’image et le libère à la fin', async () => {
    renderChatConversation();
    await waitForChatListeners('run_started', 'run_completed');

    fireChatEvent('run_started', { request_id: 'run-img', run_id: 'run-img', kind: 'production' });

    const attente = await screen.findByText('Une génération d’image est en cours. Attendez sa fin avant d’envoyer un message.');
    expect(attente).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText('Ecrivez votre message...'), { target: { value: 'Message pendant l’image' } });
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));
    expect(invocationsOf('chat_send')).toHaveLength(0);

    fireChatEvent('run_completed', { request_id: 'run-img', run_id: 'run-img' });
    await waitFor(() => expect(screen.queryByText('Une génération d’image est en cours. Attendez sa fin avant d’envoyer un message.')).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));
    await waitFor(() => expect(invocationsOf('chat_send').length).toBeGreaterThan(0));
  });

  it('bloque l’envoi pendant un entretien mémoire et le libère à la fin', async () => {
    renderChatConversation();
    await waitForChatListeners('memory_started', 'memory_finished');

    fireChatEvent('memory_started', { workspace_id: 'ws_1', kind: 'memory', mode: 'auto' });

    const attente = await screen.findByText('Un entretien mémoire est en cours. Attendez sa fin avant d’envoyer un message.');
    expect(attente).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText('Ecrivez votre message...'), { target: { value: 'Message pendant l’entretien' } });
    expect((screen.getByRole('button', { name: 'Envoyer' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));
    expect(invocationsOf('chat_send')).toHaveLength(0);

    fireChatEvent('memory_finished', { workspace_id: 'ws_1', statut: 'effectue' });
    await waitFor(() => expect(screen.queryByText('Un entretien mémoire est en cours. Attendez sa fin avant d’envoyer un message.')).toBeNull());
    expect((screen.getByRole('button', { name: 'Envoyer' }) as HTMLButtonElement).disabled).toBe(false);
  });

  it('garde l’arrêt du run courant visible malgré un entretien différé', async () => {
    renderChatConversation();
    await waitForChatListeners('run_started', 'memory_deferred');

    fireChatEvent('run_started', {
      request_id: 'run-chat-memoire',
      run_id: 'run-chat-memoire',
      conversation_id: 'conv-thot-1',
    });
    await screen.findByRole('button', { name: 'Arrêter la génération' });

    fireChatEvent('memory_deferred', { workspace_id: 'ws_1', statut: 'differe', raison: 'inférence en cours' });

    const arret = screen.getByRole('button', { name: 'Arrêter la génération' });
    expect((arret as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByText(/entretien mémoire est en cours/i)).toBeNull();
  });
});

describe('Conversation — adoption d’un run orphelin', () => {
  it('adopte le run chat de sa conversation au retour et permet l’arrêt', async () => {
    renderChatConversation();
    await waitForChatListeners('run_started', 'run_cancelled');

    fireChatEvent('run_started', {
      request_id: 'run-orphelin',
      run_id: 'run-orphelin',
      conversation_id: 'conv-thot-1',
    });

    await screen.findByRole('button', { name: 'Arrêter la génération' });

    fireChatEvent('run_cancelled', { request_id: 'run-orphelin', run_id: 'run-orphelin' });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Arrêter la génération' })).toBeNull());
  });

  it('adopte puis traite une erreur non terminale sans libérer le run', async () => {
    renderChatConversation();
    await waitForChatListeners('run_started', 'error', 'run_completed');

    fireChatEvent('run_started', {
      request_id: 'req-adopte',
      run_id: 'req-adopte',
      conversation_id: 'conv-thot-1',
    });

    await screen.findByRole('button', { name: 'Arrêter la génération' });

    fireChatEvent('error', { request_id: 'req-adopte', code: 'llm_error', message: 'générateur instable, reprise' });

    expect(screen.getByRole('button', { name: 'Arrêter la génération' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Envoyer' })).toBeNull();

    fireChatEvent('run_completed', { request_id: 'req-adopte', run_id: 'req-adopte' });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Arrêter la génération' })).toBeNull());
  });
});

describe('Conversation — changement d’identifiant', () => {
  it('détache le run de A en passant à B et envoie avec le conv_id de B', async () => {
    renderChatConversationWithNavigation();

    await waitForChatListeners('run_started', 'run_cancelled');

    fireChatEvent('run_started', {
      request_id: 'run-a',
      run_id: 'run-a',
      conversation_id: 'conv-a',
    });
    await screen.findByRole('button', { name: 'Arrêter la génération' });

    fireEvent.click(screen.getByRole('button', { name: 'Aller à B' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Arrêter la génération' })).toBeNull());

    fireChatEvent('run_cancelled', { request_id: 'run-a', run_id: 'run-a' });

    fireEvent.change(screen.getByPlaceholderText('Ecrivez votre message...'), { target: { value: 'Bonjour B' } });
    fireEvent.click(screen.getByRole('button', { name: 'Envoyer' }));
    await waitFor(() => expect(invocationsOf('chat_send').length).toBeGreaterThan(0));

    const sends = invocationsOf('chat_send');
    expect((sends[sends.length - 1]?.args as { convId?: string }).convId).toBe('conv-b');
  });
});

describe('Conversation — panneau Détails rabattable', () => {
  it('affiche le panneau par défaut et masque le bouton de réouverture', async () => {
    renderChatConversation();
    await screen.findByText('Détails');
    expect(screen.getByText('Détails')).toBeTruthy();
    expect(screen.queryByLabelText('Afficher les détails')).toBeNull();
  });

  it('rabat le panneau au clic sur la flèche', async () => {
    renderChatConversation();
    await screen.findByText('Détails');

    fireEvent.click(screen.getByRole('button', { name: 'Rabattre les détails' }));

    await waitFor(() => expect(screen.queryByText('Détails')).toBeNull());
    expect(screen.getByRole('button', { name: 'Afficher les détails' })).toBeTruthy();
  });

  it('rouvre le panneau au clic sur la flèche inverse', async () => {
    renderChatConversation();
    await screen.findByText('Détails');
    fireEvent.click(screen.getByRole('button', { name: 'Rabattre les détails' }));
    await waitFor(() => expect(screen.queryByText('Détails')).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Afficher les détails' }));

    await screen.findByText('Détails');
    expect(screen.queryByRole('button', { name: 'Afficher les détails' })).toBeNull();
  });

  it('nettoie l’en-tête des boutons inertes', async () => {
    renderChatConversation();
    await screen.findByText('Détails');
    expect(screen.queryByLabelText('Rechercher')).toBeNull();
    expect(screen.queryByLabelText('Menu')).toBeNull();
  });
});

describe('Conversation — reprise d’un run vivant', () => {
  it('restaure la bulle partielle au retour sans nouvel événement', async () => {
    renderChatConversationWithNavigation();
    await waitForChatListeners('run_started', 'thinking', 'answer', 'run_cancelled');

    fireChatEvent('run_started', { request_id: 'run-vivant', run_id: 'run-vivant', conversation_id: 'conv-a' });
    fireChatEvent('thinking', { request_id: 'run-vivant', text: 'Je réfléchis…', iteration: 99 });
    fireChatEvent('answer', { request_id: 'run-vivant', text: 'Bonjour', content: 'Bonjour' });
    await screen.findByText(/Bonjour/);

    fireEvent.click(screen.getByRole('button', { name: 'Aller à B' }));
    await waitFor(() => expect(screen.queryByText(/Bonjour/)).toBeNull());

    fireEvent.click(screen.getByRole('button', { name: 'Aller à A' }));
    await screen.findByText(/Bonjour/);
    expect(screen.getByRole('button', { name: 'Arrêter la génération' })).toBeTruthy();

    fireChatEvent('run_cancelled', { request_id: 'run-vivant', run_id: 'run-vivant' });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Arrêter la génération' })).toBeNull());
  });
});


describe('Conversation — pas de doublon après un terminal', () => {
  it('ne duplique pas la bulle assistant après un rechargement réel post-terminal', async () => {
    // Le harnais ne fournit pas de bouton de rechargement accessible dans
    // l'état prêt ; on provoque un vrai retour de route (A -> B -> A) pour
    // recharger l'historique sans remonter le provider d'activité, puis on
    // résout les `list_messages` via le harnais différé. Le run reste ainsi
    // terminal dans le provider et sa bulle synthétique (live-*) doit disparaître.
    deferHistory();
    const { container } = renderChatConversationWithNavigation({
      firstId: 'conv-thot-1',
      secondId: 'conv-reload',
    });
    await resolvePendingHistory([
      { id: 'u1', role: 'user', content: 'Dessine un phare breton' },
      { id: 'a1', role: 'assistant', content: 'Bonjour' },
    ]);
    await screen.findByText('Bonjour');
    await waitForChatListeners('run_started', 'answer', 'run_completed');

    fireChatEvent('run_started', { request_id: 'run-dup', run_id: 'run-dup', conversation_id: 'conv-thot-1' });
    await screen.findByRole('button', { name: 'Arrêter la génération' });
    fireChatEvent('answer', { request_id: 'run-dup', text: 'Bonjour', content: 'Bonjour' });
    fireChatEvent('run_completed', { request_id: 'run-dup', run_id: 'run-dup' });
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Arrêter la génération' })).toBeNull());

    // Rechargement réel : passage par une autre conversation puis retour.
    deferHistory();
    fireEvent.click(screen.getByRole('button', { name: 'Aller à B' }));
    await resolvePendingHistory([]);

    deferHistory();
    fireEvent.click(screen.getByRole('button', { name: 'Aller à A' }));
    await resolvePendingHistory([
      { id: 'u1', role: 'user', content: 'Dessine un phare breton' },
      { id: 'a1', role: 'assistant', content: 'Bonjour' },
    ]);
    await screen.findByText('Bonjour');

    // Une bulle assistant pour la réponse persistée, pas de bulle synthétique.
    await waitFor(() => {
      const turns = conversationTurns(container);
      expect(turns).toHaveLength(2);
      const assistantBonjour = turns.filter((turn) => (turn.textContent ?? '').includes('Bonjour'));
      expect(assistantBonjour).toHaveLength(1);
      expect(container.querySelector('[id^="live-"]')).toBeNull();
    });
  });
});

describe('Conversation — renommage depuis l’en-tête', () => {
  it('renomme la conversation et met à jour le titre affiché', async () => {
    configurerTitreConversation('Titre initial');
    renderChatConversation({ history: HISTORY });
    await screen.findByText('Titre initial');

    fireEvent.click(screen.getByRole('button', { name: 'Modifier le titre' }));

    const input = screen.getByRole('textbox', { name: 'Titre de la conversation' });
    expect(input).toHaveValue('Titre initial');

    fireEvent.change(input, { target: { value: 'Nouveau titre' } });
    fireEvent.submit(input.closest('form')!);

    await waitFor(() => expect(invocationsOf('rename_conversation')).toHaveLength(1));
    expect(invocationsOf('rename_conversation')[0].args).toEqual({ conversationId: 'conv-thot-1', title: 'Nouveau titre' });

    await screen.findByText('Nouveau titre');
    expect(toastsEmis()).toContainEqual(expect.objectContaining({ type: 'success', title: 'Renommé' }));
  });

  it('affiche une erreur et laisse le champ ouvert si le renommage échoue', async () => {
    configurerTitreConversation('Titre initial');
    failConversationRename();
    renderChatConversation({ history: HISTORY });
    await screen.findByText('Titre initial');

    fireEvent.click(screen.getByRole('button', { name: 'Modifier le titre' }));
    const input = screen.getByRole('textbox', { name: 'Titre de la conversation' });
    expect(input).toHaveValue('Titre initial');

    fireEvent.change(input, { target: { value: 'Nouveau titre' } });
    fireEvent.submit(input.closest('form')!);

    await waitFor(() => expect(toastsEmis().some((t) => t.type === 'error')).toBe(true));

    expect(screen.getByRole('textbox', { name: 'Titre de la conversation' })).toHaveValue('Nouveau titre');
    expect(screen.queryByText('Nouveau titre')).toBeNull();
  });

  it('annule le renommage sans appel IPC', async () => {
    configurerTitreConversation('Titre initial');
    renderChatConversation({ history: HISTORY });
    await screen.findByText('Titre initial');

    fireEvent.click(screen.getByRole('button', { name: 'Modifier le titre' }));
    const input = screen.getByRole('textbox', { name: 'Titre de la conversation' });
    fireEvent.change(input, { target: { value: 'Titre modifié' } });
    fireEvent.click(screen.getByRole('button', { name: 'Annuler le renommage' }));

    await screen.findByText('Titre initial');
    expect(invocationsOf('rename_conversation')).toHaveLength(0);
  });

  it('refuse un titre vide et referme le champ', async () => {
    configurerTitreConversation('Titre initial');
    renderChatConversation({ history: HISTORY });
    await screen.findByText('Titre initial');

    fireEvent.click(screen.getByRole('button', { name: 'Modifier le titre' }));
    const input = screen.getByRole('textbox', { name: 'Titre de la conversation' });
    fireEvent.change(input, { target: { value: '' } });
    fireEvent.submit(input.closest('form')!);

    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'Titre de la conversation' })).toBeNull());
    expect(invocationsOf('rename_conversation')).toHaveLength(0);
    expect(screen.getByText('Titre initial')).toBeTruthy();
  });

  it('ouvre un champ vide quand le titre est un titre de repli', async () => {
    configurerTitreConversation(null);
    renderChatConversation({ history: HISTORY });
    await screen.findByText('Conversation conv-tho');

    fireEvent.click(screen.getByRole('button', { name: 'Modifier le titre' }));
    const input = screen.getByRole('textbox', { name: 'Titre de la conversation' });
    expect(input).toHaveValue('');
  });

  it('une réponse tardive de get_conversation n’écrase pas un titre renommé', async () => {
    deferConversationTitle();
    renderChatConversation({ history: HISTORY });
    await screen.findByText('Voici le phare.');

    fireEvent.click(screen.getByRole('button', { name: 'Modifier le titre' }));
    const input = screen.getByRole('textbox', { name: 'Titre de la conversation' });
    fireEvent.change(input, { target: { value: 'Nouveau titre' } });
    fireEvent.submit(input.closest('form')!);

    await waitFor(() => expect(invocationsOf('rename_conversation')).toHaveLength(1));
    await screen.findByText('Nouveau titre');

    await resolvePendingConversation('Ancien titre');

    expect(screen.queryByText('Ancien titre')).toBeNull();
    expect(screen.getByText('Nouveau titre')).toBeTruthy();
  });
});
