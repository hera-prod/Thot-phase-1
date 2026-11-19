# tools — Phase 1 + brouillon : anciens (compat) + search_info/find_files/ask_files (cible)
# Notes FR : séquentiel 1 tool/iter, hash+path jamais base64
from backend.tools.base import BaseTool, ToolContext, ToolResult
from backend.tools.vision_general import VisionGeneralTool
from backend.tools.vision_ocr import VisionOCRTool
from backend.tools.search_info import SearchInfoTool, SearchInfoArgs
from backend.tools.find_files import FindFilesTool, FindFilesArgs
from backend.tools.ask_files import AskFilesTool, AskFilesArgs
from backend.tools.errors import ToolErrorCode, ERROR_ACTIONS, error_text
from backend.tools.registry import ToolRegistry, build_default_registry

__all__ = ["BaseTool", "ToolContext", "ToolResult", "VisionGeneralTool", "VisionOCRTool", "SearchInfoTool", "SearchInfoArgs", "FindFilesTool", "FindFilesArgs", "AskFilesTool", "AskFilesArgs", "ToolErrorCode", "ERROR_ACTIONS", "error_text", "ToolRegistry", "build_default_registry"]
