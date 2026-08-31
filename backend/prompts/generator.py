# Prompt Générateur — reçoit le brief de l'orchestrateur + résultats d'outils (Phase 1)
# LFM2.5-8B, 128K — NE PEUT JAMAIS appeler d'outils, même en cas d'erreur
# But : produire la réponse finale en streaming à partir du brief et des résultats

SYSTEM_GENERATOR = """Tu es Thot Générateur.
Tu reçois un brief de l'orchestrateur et, le cas échéant, des résultats d'outils vision/doc_ocr.
Produis la réponse finale pour l'utilisateur à partir de ces éléments.
Réponds dans la langue de l'utilisateur, de manière claire, structurée et concise."""

from dataclasses import dataclass

@dataclass(frozen=True)
class PromptConfig:
    system: str
    temperature: float = 0.2
    max_tokens: int = 8192

GENERATOR_CONFIG = PromptConfig(system=SYSTEM_GENERATOR, temperature=0.2, max_tokens=8192)
