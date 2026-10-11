"""Owner-set parameters of report delivery (ST-10.7), one nested `delivery:` block.

Nothing is sent unless `channels` lists a channel. Every connection detail is a `ref:NAME`
reference resolved only at send time; a literal value is rejected. Unknown keys are rejected
and no field name looks like a sensitive key.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nivesh_core.secrets import REF_RE

Channel = Literal["telegram", "slack", "email"]
CHANNELS: tuple[Channel, ...] = ("telegram", "slack", "email")


class DeliverySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channels: list[Channel] = []
    telegram_bot: str | None = None
    telegram_chat: str | None = None
    slack_hook: str | None = None
    smtp_host: str | None = None
    smtp_user: str | None = None
    smtp_auth: str | None = None
    mail_to: str | None = None
    max_attempts: int = Field(default=3, ge=1, le=5)
    timeout_s: int = Field(default=10, ge=1, le=60)

    @field_validator(
        "telegram_bot", "telegram_chat", "slack_hook", "smtp_host", "smtp_user", "smtp_auth",
        "mail_to",
    )  # fmt: skip
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        if v is not None and not REF_RE.match(v):
            raise ValueError("must be a reference such as 'ref:NAME'")
        return v
