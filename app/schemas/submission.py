"""POST /api/submissions のリクエスト/レスポンススキーマ。

想定外のpayloadキーは各Payloadモデルの extra="forbid" で拒否する。
store_id の型不正・payload形式不正はここでの ValueError により
RequestValidationError(422) となり、main.py のハンドラで 400 に変換される。

出典: hakken-f11 納品物（F11・おかむー）app/schemas/submission.py を無改変で移植。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.services.gmaps_url import is_valid_google_maps_url

SubmissionType = Literal["new_store", "info_edit", "closure_report"]


class NewStorePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gmaps_url: str
    comment: str | None = None

    @field_validator("gmaps_url")
    @classmethod
    def _validate_gmaps_url(cls, v: str) -> str:
        if not is_valid_google_maps_url(v):
            raise ValueError("gmaps_url is not an allowed Google Maps URL")
        return v


class InfoEditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str

    @field_validator("comment")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("comment must not be blank")
        return v


class ClosureReportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason must not be blank")
        return v


class SubmissionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: SubmissionType
    store_id: int | None = None
    payload: dict

    @model_validator(mode="after")
    def _validate_payload_by_type(self) -> SubmissionIn:
        if self.type == "new_store":
            if self.store_id is not None:
                raise ValueError("store_id must be null for type=new_store")
            NewStorePayload.model_validate(self.payload)
        elif self.type == "info_edit":
            if self.store_id is None:
                raise ValueError("store_id is required for type=info_edit")
            InfoEditPayload.model_validate(self.payload)
        elif self.type == "closure_report":
            if self.store_id is None:
                raise ValueError("store_id is required for type=closure_report")
            ClosureReportPayload.model_validate(self.payload)
        return self


class SubmissionOut(BaseModel):
    submission_id: int
