"""Intégration de flux2-klein-mini avec Qwen3.5-9B via Tool Calling."""
from typing import Protocol, cast

from transformers import AutoTokenizer, AutoModelForCausalLM
import torch
import json
from tools import tools, execute_tool


class _FunctionCall(Protocol):
    name: str
    arguments: str


class _ToolCall(Protocol):
    function: _FunctionCall
    id: str


class _Chat(Protocol):
    def __call__(self, *args: object, **kwargs: object) -> object: ...


def _get_chat(model: object) -> _Chat:
    chat = getattr(model, "chat", None)
    if not callable(chat):
        raise RuntimeError("The loaded Qwen model does not expose a callable chat method.")
    return cast(_Chat, chat)


# Chargement de Qwen
tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen3.5-9B")
model = AutoModelForCausalLM.from_pretrained(
    "Qwen/Qwen3.5-9B",
    torch_dtype=torch.bfloat16,
    device_map="auto"
)

def chat_with_image_generation(user_message: str):
    """Chat avec Qwen + génération d'image si nécessaire."""

    messages = [
        {
            "role": "system",
            "content": """You are a helpful assistant with access to image generation tools.
You can generate images from text descriptions (T2I) or edit/generate images using reference images (TI2I).
When the user asks to generate or create an image, use the generate_image tool with appropriate parameters.
Be detailed in your prompt description.
Available aspect ratios: 1:1 (square), 16:9 (landscape), 9:16 (portrait), 4:3, 3:4, 3:2, 2:3.
You can use up to 4 reference images for guided generation.
You CANNOT do inpainting or local edits - only full image generation."""
        },
        {"role": "user", "content": user_message}
    ]

    # Appel à Qwen avec tools
    chat = _get_chat(model)
    response = chat(
        tokenizer,
        messages,
        tools=tools,
        tool_choice="auto"
    )

    # Si Qwen appelle un tool
    tool_calls = getattr(response, "tool_calls", None)
    if tool_calls:
        tool_call = cast(_ToolCall, tool_calls[0])
        tool_name = tool_call.function.name
        tool_args = json.loads(tool_call.function.arguments)

        print(f"\n🔧 LLM appelle: {tool_name}")
        print(f"📝 Arguments: {json.dumps(tool_args, indent=2)}")

        # Exécuter le tool
        try:
            result = execute_tool(tool_name, tool_args)
            print(f"\n✅ Résultat: {result}")

            # Retourner le résultat à Qwen
            messages.append({
                "role": "tool",
                "content": json.dumps(result),
                "tool_call_id": tool_call.id
            })

            # Qwen génère une réponse finale
            final_response = chat(tokenizer, messages)
            return final_response

        except Exception as e:
            print(f"\n❌ Erreur: {e}")
            return f"Désolé, une erreur est survenue: {e}"

    return response

# === Test interactif ===
if __name__ == "__main__":
    print("=== Chat avec génération d'image (T2I + TI2I uniquement) ===")
    print("Tape 'quit' pour quitter\n")

    while True:
        user_input = input("👤 You: ")
        if user_input.lower() in ("quit", "exit"):
            break

        response = chat_with_image_generation(user_input)
        print(f"\n🤖 Assistant: {response}\n")
