"""Owner-set parameters of report checking (ST-10.6), one nested `report:` block.

`max_tolerance_digits` bounds how many displayed decimals the citation check holds a number to;
the window is always half a unit of the last displayed digit and is never widened to hide a
miss. Unknown keys are rejected and no field name looks like a sensitive key.
"""

from pydantic import BaseModel, ConfigDict, Field


class ReportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_tolerance_digits: int = Field(default=6, ge=0, le=10)
