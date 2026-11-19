"""Définition du tool generate_image pour le LLM (format OpenAI function calling)."""
import requests
import json

# === Définition du tool (UN SEUL tool : generate_image) ===
tools = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate an image from a text prompt using FLUX.2 Klein 9B. Supports text-to-image (T2I) and text+image-to-image (TI2I) with up to 4 reference images.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Text description of the image to generate. Be detailed and specific."
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"],
                        "description": "Aspect ratio of the image. Default is 1:1 (square, 1024x1024).",
                        "default": "1:1"
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Random seed for reproducibility. Use -1 for random.",
                        "default": 42
                    },
                    "ref_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of reference image paths for editing (max 4). Optional. If provided, the model will generate an image guided by these references.",
                        "maxItems": 4
                    }
                },
                "required": ["prompt"]
            }
        }
    }
]

# === Exécution du tool ===
def execute_tool(tool_name: str, arguments: dict) -> dict:
    """Exécute le tool en appelant l'API REST."""
    if tool_name != "generate_image":
        raise ValueError(f"Unknown tool: {tool_name}")

    # Appeler l'API (le serveur fait la conversion ratio → dimensions)
    response = requests.post(
        "http://localhost:8000/generate",
        json=arguments,
        timeout=60  # 60s pour la génération
    )
    response.raise_for_status()
    return response.json()

# === Test unitaire ===
if __name__ == "__main__":
    # Test de la définition
    print("Tool défini:")
    print(json.dumps(tools[0], indent=2))
