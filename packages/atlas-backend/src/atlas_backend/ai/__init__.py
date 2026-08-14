"""The AI layer.

Deliberately thin: it turns natural language into *proposals*, and hands them to
the deterministic parts of the system. Nothing here can authorise an action.

Lives inside ``atlas-backend`` rather than a separate package because the API
key must never leave the backend, and the orchestrator is inseparable from the
dispatcher and Policy Engine that already live here.
"""

from atlas_backend.ai.gemini import GeminiProvider, to_function_declaration
from atlas_backend.ai.orchestrator import (
    Assistant,
    RejectedProposal,
    StopReason,
    TurnResult,
)
from atlas_backend.ai.provider import (
    AIProvider,
    AIProviderError,
    AIRequest,
    AIResponse,
    AITimeoutError,
    FinishReason,
    MalformedResponseError,
    MessageSegment,
    ProposedToolCall,
    Provenance,
    Role,
)
from atlas_backend.ai.redaction import redact_arguments, redact_text
from atlas_backend.ai.scripted import ScriptedProvider, text_reply, tool_reply

__all__ = [
    "AIProvider",
    "AIProviderError",
    "AIRequest",
    "AIResponse",
    "AITimeoutError",
    "Assistant",
    "FinishReason",
    "GeminiProvider",
    "MalformedResponseError",
    "MessageSegment",
    "ProposedToolCall",
    "Provenance",
    "RejectedProposal",
    "Role",
    "ScriptedProvider",
    "StopReason",
    "TurnResult",
    "redact_arguments",
    "redact_text",
    "text_reply",
    "to_function_declaration",
    "tool_reply",
]
