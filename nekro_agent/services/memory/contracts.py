"""事实服务契约。调用身份由服务端适配器提供，不能从抽取文本构造。"""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue


class MemoryError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class FactStatus(StrEnum):
    ACTIVE = "active"
    PENDING = "pending_review"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    DELETED = "deleted"


class RequestContext(BaseModel):
    """只允许服务端调用点注入；is_admin 不能作为模型工具参数开放。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    workspace_id: int = Field(gt=0, strict=True)
    agent_id: str = Field(min_length=1, max_length=128)
    actor_key: str = Field(min_length=1, max_length=256)
    chat_key: str = Field(min_length=1, max_length=256)
    purpose: Literal["answer", "extract", "admin_review"] = "answer"
    is_admin: bool = False


class SourceRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["message", "admin_edit"]
    id: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=64)


class FactProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    subject_key: str = Field(min_length=1, max_length=256)
    predicate: str = Field(min_length=1, max_length=128)
    value: JsonValue
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    min_favor: int = Field(default=0, ge=0, le=100, strict=True)
    source: SourceRef
    valid_from: AwareDatetime | None = None


class ResolutionResult(BaseModel):
    assertion_id: UUID
    old_assertion_id: UUID | None = None
    status: FactStatus
    semantic_version: int
    row_revision: int
    action: Literal["created", "corrected", "supported", "pending_review"]


class EvidenceItem(BaseModel):
    assertion_id: UUID
    scope_id: UUID
    subject_key: str
    predicate: str
    value: JsonValue
    confidence: float
    min_favor: int = 0
    verification_status: str
    semantic_version: int
    row_revision: int
    valid_from: datetime
    source_refs: list[SourceRef]


class EvidenceBundle(BaseModel):
    items: list[EvidenceItem]
    policy_revisions: dict[str, int]


class ForgetReceipt(BaseModel):
    logical_block_committed: bool
    affected_assertions: int
    derived_cleanup_pending: bool


class FactRecord(BaseModel):
    """管理面单条事实的权威投影，含当前生效值与来源。"""

    assertion_id: UUID
    scope_id: UUID
    chat_key: str
    subject_key: str
    predicate: str
    value: JsonValue
    status: FactStatus
    confidence: float
    min_favor: int
    verification_status: str
    semantic_version: int
    row_revision: int
    valid_from: datetime
    valid_to: datetime | None = None
    supersedes_id: UUID | None = None
    recorded_at: datetime
    source_refs: list[SourceRef] = Field(default_factory=list)
    index_pending: bool = False


class FactPage(BaseModel):
    items: list[FactRecord]
    total: int
    policy_revisions: dict[str, int] = Field(default_factory=dict)


class FactDetail(BaseModel):
    current: FactRecord
    versions: list[FactRecord]


class ScopeGrantRecord(BaseModel):
    grant_id: UUID
    scope_id: UUID
    chat_key: str
    audience_chat_key: str
    purpose: str
    granted_by: str
    valid_until: datetime | None = None
    revoked_at: datetime | None = None
    active: bool
