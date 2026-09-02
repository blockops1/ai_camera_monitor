"""vehicle_identifier — crops + prompt → structured vision response.

No matching. No Telegram. No position concerns.
"""

from .identifier import (
    IdentifierResult,
    identify_from_crops,
)
from .prompt_template import (
    VEHICLE_CROP_PROMPT_TEMPLATE,
    VEHICLE_CROP_SCHEMA,
    render_crop_prompt,
)
from .signature import extract_signature, is_empty_signature
from .vision_client import (
    VisionError,
    VisionResult,
    call_vision,
    is_vision_error,
)

__all__ = [
    "VEHICLE_CROP_PROMPT_TEMPLATE",
    "VEHICLE_CROP_SCHEMA",
    "IdentifierResult",
    "VisionError",
    "VisionResult",
    "call_vision",
    "extract_signature",
    "identify_from_crops",
    "is_empty_signature",
    "is_vision_error",
    "render_crop_prompt",
]
