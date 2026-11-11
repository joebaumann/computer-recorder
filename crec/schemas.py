# Modified for the recorder-only fork: removed AI processing schemas.
from typing import Literal

from pydantic import BaseModel, Field


class Update(BaseModel):
    content: str = Field(..., description="The content of the update")
    content_type: Literal["input_text", "input_image"] = Field(
        ..., description="The type of the update"
    )
