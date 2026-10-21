"""统一事实管理路由。

事实的列表、详情、纠正、遗忘与跨会话授权全部经由 MemoryService 处理；
路由只负责管理员鉴权、参数校验与错误映射，不直接读写事实表。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, List, Literal, Optional, TypeVar
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, JsonValue

from nekro_agent.models.db_user import DBUser
from nekro_agent.schemas.errors import (
    AppError,
    MemoryConflictError,
    NotFoundError,
    OperationFailedError,
    PermissionDeniedError,
    ValidationError,
)
from nekro_agent.services.memory.contracts import (
    FactDetail,
    FactPage,
    FactStatus,
    ForgetReceipt,
    MemoryError,
    RequestContext,
    ResolutionResult,
    ScopeGrantRecord,
)
from nekro_agent.services.memory.dead_letter import DeadLetterAdmin, DeadLetterPage
from nekro_agent.services.memory.fact_index import (
    FactCollectionInfo,
    list_fact_collections,
    purge_stale_fact_collections,
)
from nekro_agent.services.memory.feature_flags import MemoryOperation, ensure_memory_system_enabled
from nekro_agent.services.memory.rollout import RolloutReport, RolloutService
from nekro_agent.services.memory.service import MemoryService
from nekro_agent.services.user.deps import get_current_active_user
from nekro_agent.services.user.perm import Role, require_role

router = APIRouter(prefix="/memory_facts", tags=["Memory Facts"])

StatusFilter = Literal["active", "pending_review", "superseded", "retracted", "deleted", "all"]

T = TypeVar("T")

# 错误码到 HTTP 语义的固定映射，避免通过状态码推断记录是否存在。
_MEMORY_ERROR_MAP: dict[str, Any] = {
    "SCOPE_DENIED": lambda code: PermissionDeniedError(),
    "SCOPE_UNRESOLVED": lambda code: PermissionDeniedError(),
    "SOURCE_INVALID": lambda code: MemoryConflictError(reason=code),
    "POLICY_CHANGED": lambda code: MemoryConflictError(reason=code),
    "VERSION_CONFLICT": lambda code: MemoryConflictError(reason=code),
    "IDEMPOTENCY_CONFLICT": lambda code: MemoryConflictError(reason=code),
    "LIMIT_INVALID": lambda code: ValidationError(reason=code),
    "EXPRESSION_GATE_INVALID": lambda code: ValidationError(reason=code),
    "IDEMPOTENCY_KEY_INVALID": lambda code: ValidationError(reason=code),
    "RESERVED_DOMAIN": lambda code: ValidationError(reason=code),
    "KIND_INVALID": lambda code: ValidationError(reason=code),
    "PIPELINE_MODE_INVALID": lambda code: ValidationError(reason=code),
    "ROLLOUT_BUSY": lambda code: MemoryConflictError(reason=code),
    "WRITER_FENCED": lambda code: MemoryConflictError(reason=code),
}


class _CorrectBody(BaseModel):
    workspace_id: int = Field(gt=0)
    chat_key: str = Field(min_length=1, max_length=256)
    value: JsonValue
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    min_favor: Optional[int] = Field(default=None, ge=0, le=100)
    expected_row_revision: Optional[int] = Field(default=None, ge=1)
    reason: str = Field(default="", max_length=64)


class _ForgetBody(BaseModel):
    workspace_id: int = Field(gt=0)
    chat_key: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="", max_length=64)


class _GrantBody(BaseModel):
    workspace_id: int = Field(gt=0)
    chat_key: str = Field(min_length=1, max_length=256)
    audience_chat_key: str = Field(min_length=1, max_length=256)
    valid_until: Optional[datetime] = None


class _GrantCreated(BaseModel):
    grant_id: UUID


class _ActionOk(BaseModel):
    ok: bool = True


class _DeadLetterBody(BaseModel):
    workspace_id: int = Field(gt=0)
    chat_key: str = Field(min_length=1, max_length=256)
    kind: Literal["index", "ingest"]


class _PurgeBody(BaseModel):
    workspace_id: int = Field(gt=0)
    chat_key: str = Field(min_length=1, max_length=256)
    limit: int = Field(default=50, ge=1, le=500)


class _PurgeResult(BaseModel):
    purged: int


class _RetentionOverview(BaseModel):
    deleted_assertions: int
    purged_assertions: int
    pending_purge: int
    due_for_purge: int
    index_pending: int
    index_dead_letter: int
    purge_enabled: bool
    collections: list[FactCollectionInfo]


class _CollectionPurgeResult(BaseModel):
    removed: list[str]


class _RolloutBody(BaseModel):
    workspace_id: int = Field(gt=0)
    chat_key: str = Field(min_length=1, max_length=256)
    mode: Literal["legacy_guarded", "shadow", "service"]
    expected_writer_revision: int = Field(ge=1)
    isolate_leases: bool = False
    reason: str = Field(default="", max_length=64)


def _admin_context(*, workspace_id: int, chat_key: str, username: str) -> RequestContext:
    """管理端上下文：管理员身份与操作者由服务端注入，不接受客户端传入。"""
    return RequestContext(
        workspace_id=int(workspace_id),
        agent_id="nekro-agent",
        actor_key=f"web:{username}",
        chat_key=chat_key,
        purpose="admin_review",
        is_admin=True,
    )


def _to_app_error(exc: MemoryError) -> AppError:
    code = getattr(exc, "code", "") or str(exc)
    factory = _MEMORY_ERROR_MAP.get(code)
    if factory is not None:
        return factory(code)
    return OperationFailedError(operation="记忆操作")


def _dead_letter_admin(context: RequestContext) -> DeadLetterAdmin:
    try:
        return DeadLetterAdmin(context=context)
    except MemoryError as exc:
        raise _to_app_error(exc) from exc


async def _call(awaitable: Awaitable[T]) -> T:
    try:
        return await awaitable
    except MemoryError as exc:
        raise _to_app_error(exc) from exc


def _status_value(status: str) -> Optional[FactStatus]:
    if status == "all":
        return None
    try:
        return FactStatus(status)
    except ValueError as exc:
        raise ValidationError(reason=f"不支持的记忆状态: {status}") from exc


@router.get("", summary="列出当前可见的长期事实", response_model=FactPage)
@require_role(Role.Admin)
async def list_memory_facts(
    workspace_id: int = Query(..., gt=0),
    chat_key: str = Query(..., min_length=1, max_length=256),
    subject_key: Optional[str] = Query(default=None, max_length=256),
    predicate: Optional[str] = Query(default=None, max_length=128),
    status: StatusFilter = Query(default="active"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _current_user: DBUser = Depends(get_current_active_user),
) -> FactPage:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=workspace_id, chat_key=chat_key, username=_current_user.username)
    return await _call(
        MemoryService().list_facts(
            context,
            subject_key=subject_key,
            predicate=predicate,
            status=_status_value(status),
            limit=limit,
            offset=offset,
        ),
    )


@router.get("/grants", summary="列出跨会话读取授权", response_model=List[ScopeGrantRecord])
@require_role(Role.Admin)
async def list_memory_grants(
    workspace_id: int = Query(..., gt=0),
    chat_key: str = Query(..., min_length=1, max_length=256),
    _current_user: DBUser = Depends(get_current_active_user),
) -> List[ScopeGrantRecord]:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=workspace_id, chat_key=chat_key, username=_current_user.username)
    return await _call(MemoryService().list_grants(context))


@router.post("/grants", summary="授予跨会话读取", response_model=_GrantCreated)
@require_role(Role.Admin)
async def create_memory_grant(
    body: _GrantBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> _GrantCreated:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=body.workspace_id, chat_key=body.chat_key, username=_current_user.username)
    grant_id = await _call(
        MemoryService().grant_scope(context, audience_chat_key=body.audience_chat_key, valid_until=body.valid_until),
    )
    return _GrantCreated(grant_id=grant_id)


@router.delete("/grants/{grant_id}", summary="撤销跨会话读取", response_model=_ActionOk)
@require_role(Role.Admin)
async def revoke_memory_grant(
    grant_id: UUID,
    workspace_id: int = Query(..., gt=0),
    chat_key: str = Query(..., min_length=1, max_length=256),
    _current_user: DBUser = Depends(get_current_active_user),
) -> _ActionOk:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=workspace_id, chat_key=chat_key, username=_current_user.username)
    await _call(MemoryService().revoke_grant(context, grant_id))
    return _ActionOk()


@router.get("/dead_letters", summary="查看索引与抽取队列的死信", response_model=DeadLetterPage)
@require_role(Role.Admin)
async def list_memory_dead_letters(
    workspace_id: int = Query(..., gt=0),
    chat_key: str = Query(..., min_length=1, max_length=256),
    kind: Literal["index", "ingest"] = Query(default="index"),
    state: str = Query(default="dead_letter", max_length=24),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _current_user: DBUser = Depends(get_current_active_user),
) -> DeadLetterPage:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=workspace_id, chat_key=chat_key, username=_current_user.username)
    admin = _dead_letter_admin(context)
    return await _call(admin.list(kind=kind, state=state, limit=limit, offset=offset))


@router.post("/dead_letters/{item_id}/retry", summary="重试死信任务", response_model=_ActionOk)
@require_role(Role.Admin)
async def retry_memory_dead_letter(
    item_id: UUID,
    body: _DeadLetterBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> _ActionOk:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=body.workspace_id, chat_key=body.chat_key, username=_current_user.username)
    admin = _dead_letter_admin(context)
    if not await _call(admin.retry(kind=body.kind, item_id=item_id)):
        raise NotFoundError()
    return _ActionOk()


@router.post("/dead_letters/{item_id}/ignore", summary="确认忽略死信任务", response_model=_ActionOk)
@require_role(Role.Admin)
async def ignore_memory_dead_letter(
    item_id: UUID,
    body: _DeadLetterBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> _ActionOk:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=body.workspace_id, chat_key=body.chat_key, username=_current_user.username)
    admin = _dead_letter_admin(context)
    if not await _call(admin.ignore(kind=body.kind, item_id=item_id)):
        raise NotFoundError()
    return _ActionOk()


@router.post("/purge", summary="按保留期清理已遗忘事实的正文", response_model=_PurgeResult)
@require_role(Role.Admin)
async def purge_memory_facts(
    body: _PurgeBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> _PurgeResult:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=body.workspace_id, chat_key=body.chat_key, username=_current_user.username)
    purged = await _call(MemoryService().purge_forgotten(context, limit=body.limit))
    return _PurgeResult(purged=purged)


@router.get("/retention", summary="查看遗忘清理进度与事实索引 collection", response_model=_RetentionOverview)
@require_role(Role.Admin)
async def read_retention_overview(
    workspace_id: int = Query(..., gt=0),
    chat_key: str = Query(..., min_length=1, max_length=256),
    _current_user: DBUser = Depends(get_current_active_user),
) -> _RetentionOverview:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=workspace_id, chat_key=chat_key, username=_current_user.username)
    status = await _call(MemoryService().retention_status(context))
    collections = await list_fact_collections()
    return _RetentionOverview(
        **status,
        purge_enabled=MemoryService._purge_enabled(),
        collections=collections,
    )


@router.post(
    "/collections/purge",
    summary="删除配置变更后遗留的事实索引 collection",
    response_model=_CollectionPurgeResult,
)
@require_role(Role.Admin)
async def purge_stale_collections(
    body: _PurgeBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> _CollectionPurgeResult:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    # 鉴权与逐个 scope 无关，但要确认调用者对该会话有管理权限。
    _admin_context(workspace_id=body.workspace_id, chat_key=body.chat_key, username=_current_user.username)
    removed = await purge_stale_fact_collections()
    return _CollectionPurgeResult(removed=removed)


@router.get("/rollout", summary="查看频道灰度预检结果", response_model=RolloutReport)
@require_role(Role.Admin)
async def read_rollout_report(
    workspace_id: int = Query(..., gt=0),
    chat_key: str = Query(..., min_length=1, max_length=256),
    _current_user: DBUser = Depends(get_current_active_user),
) -> RolloutReport:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=workspace_id, chat_key=chat_key, username=_current_user.username)
    return await _call(RolloutService(MemoryService()).report(context))


@router.post("/rollout", summary="切换频道的事实所有权（灰度与回滚共用）", response_model=RolloutReport)
@require_role(Role.Admin)
async def switch_channel_mode(
    body: _RolloutBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> RolloutReport:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=body.workspace_id, chat_key=body.chat_key, username=_current_user.username)
    return await _call(
        RolloutService(MemoryService()).switch(
            context,
            mode=body.mode,
            expected_writer_revision=body.expected_writer_revision,
            isolate_leases=body.isolate_leases,
            reason=body.reason,
        ),
    )


@router.get("/{assertion_id}", summary="查看事实详情与版本链", response_model=FactDetail)
@require_role(Role.Admin)
async def get_memory_fact(
    assertion_id: UUID,
    workspace_id: int = Query(..., gt=0),
    chat_key: str = Query(..., min_length=1, max_length=256),
    _current_user: DBUser = Depends(get_current_active_user),
) -> FactDetail:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=workspace_id, chat_key=chat_key, username=_current_user.username)
    return await _call(MemoryService().get_fact(context, assertion_id))


@router.post("/{assertion_id}/correct", summary="纠正事实当前值", response_model=ResolutionResult)
@require_role(Role.Admin)
async def correct_memory_fact(
    assertion_id: UUID,
    body: _CorrectBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ResolutionResult:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=body.workspace_id, chat_key=body.chat_key, username=_current_user.username)
    return await _call(
        MemoryService().correct_fact(
            context,
            assertion_id,
            value=body.value,
            confidence=body.confidence,
            min_favor=body.min_favor,
            expected_row_revision=body.expected_row_revision,
            reason=body.reason,
        ),
    )


@router.post("/{assertion_id}/forget", summary="遗忘事实的全部版本", response_model=ForgetReceipt)
@require_role(Role.Admin)
async def forget_memory_fact(
    assertion_id: UUID,
    body: _ForgetBody,
    _current_user: DBUser = Depends(get_current_active_user),
) -> ForgetReceipt:
    ensure_memory_system_enabled(MemoryOperation.MANAGEMENT)
    context = _admin_context(workspace_id=body.workspace_id, chat_key=body.chat_key, username=_current_user.username)
    return await _call(MemoryService().forget(context, assertion_id))
