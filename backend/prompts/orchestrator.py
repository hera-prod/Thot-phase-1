# Prompt Orchestrateur — prépare la demande pour le générateur (Phase 1, 2 outils vision)
# LFM2.5-8B, 128K — sait qu'il peut appeler vision/doc_ocr, pas besoin de dire qu'il ne voit pas
# But : analyser la demande, appeler les outils si besoin, produire un brief pour le générateur

SYSTEM_ORCHESTRATOR = """Tu es Thot Orchestrateur.
Tu analyses la demande de l'utilisateur et décides si tu as besoin d'outils.
Outils disponibles : vision, doc_ocr, get_source, read_text. Vision/doc_ocr analysent image/pdf, get_source retrouve une source historique, read_text lit txt/md.
Des fichiers peuvent être disponibles dans cette conversation (voir SOURCES DISPONIBLES). Si répondre correctement nécessite un détail visuel précis — qu'il s'agisse d'un fichier tout juste envoyé (first_seen=current) avec une question ouverte comme "Qu'en penses-tu ?", "Décris", ou d'un fichier plus ancien référencé par "là", "sa peau", "tatouage" — appelle get_source si nécessaire puis vision/doc_ocr avant de répondre, même si tu as déjà décrit ce fichier. Ne considère jamais une description antérieure comme preuve à jour.
Réponds dans la langue de l'utilisateur, de manière claire et concise."""

# Grammaire stricte pour forcer le JSON du brief — DÉSACTIVÉE (ticket llama.cpp #20345)
# La grammaire est désactivée quand le thinking est actif sur certains templates LFM2.5.
# Activer response_format + thinking = génération libre malgré la grammaire → faux sentiment de sécurité.
# On garde BRIEF_GRAMMAR en référence mais ORCHESTRATOR_CONFIG.json_schema=None et loop.py fait du texte libre + sanitizer backend.
# Si besoin futur : désactiver explicitement thinking pour l'appel brief ou brancher json_schema.
BRIEF_GRAMMAR = """root ::= "{" '"mode":' mode "," '"brief":' string "," '"tool_calls":' tool_calls "}"
mode ::= '"DIRECT_ANSWER"' | '"USE_FINDINGS"'
tool_calls ::= "[" (tool_call ("," tool_call)*)? "]"
tool_call ::= "{" '"tool":' string "," '"args":' object "}"
"""  # inactive — voir #20345

from dataclasses import dataclass

@dataclass(frozen=True)
class PromptConfig:
    """Config figée d'un prompt (température, json_schema...)."""
    system: str
    temperature: float = 0.2
    top_p: float = 0.9
    max_tokens: int = 2048
    json_schema: dict | None = None

# Config chat direct — pas de format JSON imposé, génération libre
ORCHESTRATOR_CONFIG = PromptConfig(
    system=SYSTEM_ORCHESTRATOR,
    temperature=0.2, top_p=0.9, max_tokens=2048,
    json_schema=None,
)
