from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FreshsalesRecordType(StrEnum):
    lead = "lead"
    deal = "deal"


class FreshsalesStatus(StrEnum):
    cc_qualified = "CC Qualified"
    future_prospect = "Future Prospect"
    dropped = "Dropped"
    won = "Won"


class FreshsalesWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_type: FreshsalesRecordType
    record_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    status: FreshsalesStatus
    occurred_at: datetime
    event_id: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z0-9_.:-]+$")


class MetaLeadgenEntryChangeValue(BaseModel):
    model_config = ConfigDict(extra="allow")

    leadgen_id: str = Field(min_length=1, max_length=80, pattern=r"^[0-9A-Za-z_-]+$")


class MetaLeadgenEntryChange(BaseModel):
    model_config = ConfigDict(extra="allow")

    field: str = Field(max_length=80)
    value: MetaLeadgenEntryChangeValue


class MetaLeadgenEntry(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=80)
    time: int = Field(ge=0)
    changes: list[MetaLeadgenEntryChange] = Field(min_length=1, max_length=20)


class MetaLeadgenWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    object: str = Field(max_length=80)
    entry: list[MetaLeadgenEntry] = Field(min_length=1, max_length=20)

    @field_validator("object")
    @classmethod
    def object_must_be_page(cls, value: str) -> str:
        if value != "page":
            raise ValueError("unsupported Meta object")
        return value
