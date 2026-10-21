"""长期事实唯一门面。增量实现先提供证据、版本和 SQL 复核，不自动接管线上管线。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import JsonValue
from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.expressions import Q
from tortoise.transactions import in_transaction

from nekro_agent.models.db_chat_channel import DBChatChannel
from nekro_agent.models.db_chat_message import DBChatMessage
from nekro_agent.models.db_mem_fact import (
    DBMemAssertion,
    DBMemAssertionEvidence,
    DBMemAuditEvent,
    DBMemDomainOutbox,
    DBMemIndexOutbox,
    DBMemObservation,
    DBMemScope,
    DBMemScopeGrant,
    DBMemWriteRequest,
)
from nekro_agent.services.memory.contracts import (
    EvidenceBundle,
    EvidenceItem,
    FactDetail,
    FactPage,
    FactProposal,
    FactRecord,
    FactStatus,
    ForgetReceipt,
    MemoryError,
    RequestContext,
    ResolutionResult,
    ScopeGrantRecord,
    SourceRef,
)


def message_revision(message: DBChatMessage) -> str:
    """源修订覆盖内容、归属和撤回状态，不能仅信任模型返回的消息编号。"""
    return _digest([
        message.chat_key, message.adapter_key, message.platform_userid,
        message.sender_id, message.send_timestamp, message.content_text, message.is_recalled,
    ])


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class MemoryService:
    """无隐式工作区回退；所有调用均携带服务端 RequestContext。

    M1 不自动运行抽取、不迁移旧库、不写关系数值。scope 行锁串行化同一
    来源频道的事实变更，写请求幂等槽使迟到重试不能覆盖后来的纠正。
    """

    _reserved_predicates = frozenset({
        "favorability", "relationship_score", "relationship_stage", "agent_persona",
        "system_prompt", "permissions", "scope_grant",
    })

    def __init__(self, *, multivalued_predicates: frozenset[str] = frozenset()):
        self.multivalued_predicates = multivalued_predicates

    async def resolve_scope(self, context: RequestContext) -> DBMemScope:
        if context.purpose == "admin_review" and not context.is_admin:
            raise MemoryError("SCOPE_DENIED")
        channel = await DBChatChannel.filter(chat_key=context.chat_key).first()
        if channel is None or channel.workspace_id != context.workspace_id:
            raise MemoryError("SCOPE_UNRESOLVED")
        scope_id = uuid5(NAMESPACE_URL, _digest([context.workspace_id, context.agent_id, context.chat_key]))
        scope, _ = await DBMemScope.get_or_create(
            id=scope_id,
            defaults={
                "workspace_id": context.workspace_id,
                "agent_id": context.agent_id,
                "origin_chat_key": context.chat_key,
            },
        )
        return scope

    async def _observation(
        self, context: RequestContext, scope: DBMemScope, source: SourceRef, conn: BaseDBAsyncClient,
    ) -> DBMemObservation:
        now = datetime.now(timezone.utc)
        speaker = context.actor_key
        if source.kind == "message":
            if not source.id.isdecimal():
                raise MemoryError("SOURCE_INVALID")
            message = await DBChatMessage.filter(id=int(source.id), chat_key=context.chat_key).using_db(conn).first()
            if message is None or message.is_recalled or message_revision(message) != source.revision:
                raise MemoryError("SOURCE_INVALID")
            speaker = f"{message.adapter_key}:{message.platform_userid}"
            if context.purpose != "extract" and not context.is_admin and speaker != context.actor_key:
                raise MemoryError("SCOPE_DENIED")
            now = datetime.fromtimestamp(message.send_timestamp, tz=timezone.utc)
        elif not context.is_admin:
            raise MemoryError("SCOPE_DENIED")
        observation, _ = await DBMemObservation.get_or_create(
            scope_id=scope.id, source_kind=source.kind, source_id=source.id, source_revision=source.revision,
            defaults={"speaker_key": speaker, "observed_at": now}, using_db=conn,
        )
        if observation.status != "valid":
            raise MemoryError("SOURCE_INVALID")
        return observation

    async def propose_fact(
        self, context: RequestContext, proposal: FactProposal, *, idempotency_key: str,
        expected_row_revision: int | None = None,
        expected_writer_revision: int | None = None,
    ) -> ResolutionResult:
        if not idempotency_key or len(idempotency_key) > 256:
            raise MemoryError("IDEMPOTENCY_KEY_INVALID")
        if proposal.predicate.casefold() in self._reserved_predicates:
            raise MemoryError("RESERVED_DOMAIN")
        scope = await self.resolve_scope(context)
        request_id = _digest([str(scope.id), context.actor_key, idempotency_key])
        fingerprint = _digest([proposal.model_dump(mode="json"), expected_row_revision])
        async with in_transaction() as conn:
            scope = await DBMemScope.filter(id=scope.id).using_db(conn).select_for_update().get()
            if expected_writer_revision is not None and (
                scope.writer_revision != expected_writer_revision or scope.pipeline_mode != "service"
            ):
                raise MemoryError("WRITER_FENCED")
            previous_request = await DBMemWriteRequest.filter(id=request_id).using_db(conn).first()
            if previous_request is not None:
                if previous_request.fingerprint != fingerprint:
                    raise MemoryError("IDEMPOTENCY_CONFLICT")
                result = ResolutionResult.model_validate(previous_request.result_json)
                current = await DBMemAssertion.filter(id=result.assertion_id).using_db(conn).get()
                return result.model_copy(update={"status": FactStatus(current.status), "row_revision": current.row_revision})

            source = await self._observation(context, scope, proposal.source, conn)
            logical_parts = [str(scope.id), proposal.subject_key, proposal.predicate]
            if proposal.predicate in self.multivalued_predicates:
                logical_parts.append(_digest(proposal.value))
            logical_key = _digest(logical_parts)
            versions = DBMemAssertion.filter(scope_id=scope.id, logical_key=logical_key).using_db(conn)
            deleted_ids = await versions.filter(status=FactStatus.DELETED).values_list("id", flat=True)
            if deleted_ids and await DBMemAssertionEvidence.filter(
                assertion_id__in=list(deleted_ids), observation_id=source.id,
            ).using_db(conn).exists():
                raise MemoryError("SOURCE_INVALID")
            latest = await versions.order_by("-semantic_version").first()
            current = await versions.filter(status=FactStatus.ACTIVE).first()
            if expected_row_revision is not None and (current is None or current.row_revision != expected_row_revision):
                raise MemoryError("VERSION_CONFLICT")
            now = datetime.now(timezone.utc)
            valid_from = proposal.valid_from or source.observed_at
            manual = proposal.source.kind == "admin_edit" and context.is_admin
            newer_source_exists = False
            if current is not None and not manual:
                current_source_ids = await DBMemAssertionEvidence.filter(
                    assertion_id=current.id,
                ).using_db(conn).values_list("observation_id", flat=True)
                current_sources = await DBMemObservation.filter(id__in=list(current_source_ids)).using_db(conn)
                for known in current_sources:
                    if known.observed_at > source.observed_at or (
                        known.observed_at == source.observed_at and known.source_kind == source.source_kind == "message"
                        and known.source_id.isdecimal() and source.source_id.isdecimal()
                        and int(known.source_id) > int(source.source_id)
                    ):
                        newer_source_exists = True
                        break
            pending = (
                not manual and proposal.subject_key != source.speaker_key
                or not manual and current is not None and current.verification_status == "manual_admin"
                or valid_from > now
                or current is not None and valid_from < current.valid_from
                or newer_source_exists
            )
            same_value = current is not None and _digest(current.value_json["value"]) == _digest(proposal.value)
            if same_value and not pending:
                assertion = current
                action = "supported"
            else:
                assertion = await DBMemAssertion.create(
                    scope_id=scope.id, logical_key=logical_key, subject_key=proposal.subject_key,
                    # Tortoise JSONField 将裸字符串解释为已编码 JSON；使用信封保留标量类型。
                    predicate=proposal.predicate, value_json={"value": proposal.value},
                    semantic_version=(latest.semantic_version + 1 if latest else 1),
                    status=FactStatus.PENDING if pending else FactStatus.ACTIVE,
                    confidence=proposal.confidence,
                    min_favor=proposal.min_favor if manual or current is None else max(current.min_favor, proposal.min_favor),
                    verification_status=(
                        "manual_admin" if manual else "user_declared"
                        if proposal.subject_key == source.speaker_key else "third_party"
                    ),
                    valid_from=valid_from, supersedes_id=current.id if current and not pending else None,
                    using_db=conn,
                )
                action = "pending_review" if pending else "corrected" if current else "created"
                if current is not None and not pending:
                    await DBMemAssertion.filter(id=current.id).using_db(conn).update(
                        status=FactStatus.SUPERSEDED, valid_to=valid_from, row_revision=current.row_revision + 1,
                    )
                    await self._emit(scope, context, current.id, current.row_revision + 1, "superseded", conn)
            await DBMemAssertionEvidence.get_or_create(
                assertion_id=assertion.id, observation_id=source.id, using_db=conn,
            )
            # 内容相同也有新证据和审计，但不从旧值继承置信度。
            if action == "supported":
                assertion.row_revision += 1
                if manual:
                    assertion.verification_status = "manual_admin"
                assertion.min_favor = proposal.min_favor if manual else max(assertion.min_favor, proposal.min_favor)
                await assertion.save(using_db=conn, update_fields=["row_revision", "verification_status", "min_favor"])
            await DBMemScope.filter(id=scope.id).using_db(conn).update(revision=scope.revision + 1)
            await self._emit(scope, context, assertion.id, assertion.row_revision, action, conn)
            result = ResolutionResult(
                assertion_id=assertion.id, old_assertion_id=current.id if current and action == "corrected" else None,
                status=FactStatus(assertion.status), semantic_version=assertion.semantic_version,
                row_revision=assertion.row_revision, action=action,
            )
            await DBMemWriteRequest.create(
                id=request_id, scope_id=scope.id, fingerprint=fingerprint, result_json=result.model_dump(mode="json"),
                using_db=conn,
            )
            return result

    async def _emit(
        self, scope: DBMemScope, context: RequestContext, assertion_id: UUID, revision: int,
        operation: str, conn: BaseDBAsyncClient,
    ) -> None:
        await DBMemAuditEvent.create(
            scope_id=scope.id, actor_key=context.actor_key, operation=operation,
            target_id=assertion_id, reason_code="fact_service", using_db=conn,
        )
        await DBMemIndexOutbox.create(
            assertion_id=assertion_id, assertion_version=revision, operation=operation, using_db=conn,
        )
        # 交接只带 ID，消费者按当前权威状态取证据，避免旧正文在删除后继续流转。
        await DBMemDomainOutbox.create(
            scope_id=scope.id, event_type=f"fact.{operation}", aggregate_id=assertion_id,
            aggregate_revision=revision, payload_json={"schema_version": 1}, using_db=conn,
        )

    async def _visible_scopes(self, context: RequestContext) -> list[DBMemScope]:
        local = await self.resolve_scope(context)
        scopes = DBMemScope.filter(workspace_id=context.workspace_id, agent_id=context.agent_id)
        if context.is_admin and context.purpose == "admin_review":
            return await scopes
        grants = await DBMemScopeGrant.filter(
            Q(valid_until=None) | Q(valid_until__gt=datetime.now(timezone.utc)),
            audience_chat_key=context.chat_key, purpose=context.purpose, revoked_at=None,
        ).values_list("scope_id", flat=True)
        return await scopes.filter(Q(id=local.id) | Q(id__in=list(grants)))

    async def _valid_sources(self, assertion_id: UUID, scope: DBMemScope) -> list[SourceRef]:
        ids = await DBMemAssertionEvidence.filter(assertion_id=assertion_id).values_list("observation_id", flat=True)
        observations = await DBMemObservation.filter(id__in=list(ids), scope_id=scope.id, status="valid")
        refs: list[SourceRef] = []
        for observation in observations:
            if observation.source_kind == "message":
                if not observation.source_id.isdecimal():
                    continue
                message = await DBChatMessage.filter(
                    id=int(observation.source_id), chat_key=scope.origin_chat_key, is_recalled=False,
                ).first()
                if message is None or message_revision(message) != observation.source_revision:
                    continue
            elif observation.source_kind != "admin_edit":
                continue
            refs.append(SourceRef(kind=observation.source_kind, id=observation.source_id, revision=observation.source_revision))
        return refs

    async def recall(
        self, context: RequestContext, *, subject_key: str | None = None, predicate: str | None = None,
        query: str = "", limit: int = 20,
        expression_ceiling: int = 0,
    ) -> EvidenceBundle:
        if limit < 1 or limit > 100:
            raise MemoryError("LIMIT_INVALID")
        if isinstance(expression_ceiling, bool) or not isinstance(expression_ceiling, int) or not 0 <= expression_ceiling <= 100:
            raise MemoryError("EXPRESSION_GATE_INVALID")
        scopes = {scope.id: scope for scope in await self._visible_scopes(context)}
        now = datetime.now(timezone.utc)
        rows = DBMemAssertion.filter(
            Q(valid_to=None) | Q(valid_to__gt=now), scope_id__in=list(scopes),
            status=FactStatus.ACTIVE, valid_from__lte=now,
        )
        if not (context.is_admin and context.purpose == "admin_review"):
            rows = rows.filter(min_favor__lte=expression_ceiling)
        if subject_key is not None:
            rows = rows.filter(subject_key=subject_key)
        if predicate is not None:
            rows = rows.filter(predicate=predicate)
        from nekro_agent.services.memory.fact_retrieval import rank_facts

        items: list[EvidenceItem] = []
        async for row in rows.order_by("-recorded_at"):
            scope = scopes[row.scope_id]
            sources = await self._valid_sources(row.id, scope)
            if not sources:
                continue
            items.append(EvidenceItem(
                assertion_id=row.id, scope_id=row.scope_id, subject_key=row.subject_key,
                predicate=row.predicate, value=row.value_json["value"], confidence=row.confidence,
                min_favor=row.min_favor,
                verification_status=row.verification_status, semantic_version=row.semantic_version,
                row_revision=row.row_revision, valid_from=row.valid_from, source_refs=sources,
            ))
            if not query and len(items) >= limit:
                break
        if query:
            items = await rank_facts(query, items, limit=limit)
        bundle = EvidenceBundle(items=items, policy_revisions={str(s.id): s.revision for s in scopes.values()})
        await self.validate_bundle(context, bundle)
        return bundle

    async def validate_bundle(self, context: RequestContext, bundle: EvidenceBundle) -> None:
        """装配／发送前必须复核；不把旧 EvidenceBundle 当作永久授权。"""
        scopes = {str(scope.id): scope for scope in await self._visible_scopes(context)}
        for scope_id, revision in bundle.policy_revisions.items():
            if scope_id not in scopes or scopes[scope_id].revision != revision:
                raise MemoryError("POLICY_CHANGED")
        for item in bundle.items:
            row = await DBMemAssertion.filter(id=item.assertion_id, status=FactStatus.ACTIVE).first()
            if row is None or row.row_revision != item.row_revision or str(row.scope_id) not in scopes:
                raise MemoryError("POLICY_CHANGED")
            now = datetime.now(timezone.utc)
            if row.valid_from > now or row.valid_to is not None and row.valid_to <= now:
                raise MemoryError("POLICY_CHANGED")
            if not await self._valid_sources(row.id, scopes[str(row.scope_id)]):
                raise MemoryError("SOURCE_INVALID")

    async def forget(self, context: RequestContext, assertion_id: UUID) -> ForgetReceipt:
        scope = await self.resolve_scope(context)
        async with in_transaction() as conn:
            scope = await DBMemScope.filter(id=scope.id).using_db(conn).select_for_update().get()
            target = await DBMemAssertion.filter(id=assertion_id, scope_id=scope.id).using_db(conn).first()
            if target is None or not context.is_admin and target.subject_key != context.actor_key:
                raise MemoryError("SCOPE_DENIED")
            # 删除逻辑键的全部版本，历史查询不能重新找回被遗忘正文。
            versions = await DBMemAssertion.filter(
                scope_id=scope.id, logical_key=target.logical_key,
            ).exclude(status=FactStatus.DELETED).using_db(conn)
            for version in versions:
                await DBMemAssertion.filter(id=version.id).using_db(conn).update(
                    status=FactStatus.DELETED, row_revision=version.row_revision + 1,
                )
                await self._emit(scope, context, version.id, version.row_revision + 1, "deleted", conn)
            if versions:
                await DBMemScope.filter(id=scope.id).using_db(conn).update(revision=scope.revision + 1)
            return ForgetReceipt(
                logical_block_committed=True, affected_assertions=len(versions), derived_cleanup_pending=bool(versions),
            )

    async def grant_scope(
        self, context: RequestContext, *, audience_chat_key: str, valid_until: datetime | None = None,
    ) -> UUID:
        if not context.is_admin or context.purpose != "admin_review":
            raise MemoryError("SCOPE_DENIED")
        scope = await self.resolve_scope(context)
        audience = await DBChatChannel.filter(chat_key=audience_chat_key, workspace_id=context.workspace_id).first()
        if audience is None or valid_until is not None and (valid_until.tzinfo is None or valid_until <= datetime.now(timezone.utc)):
            raise MemoryError("SCOPE_DENIED")
        async with in_transaction() as conn:
            scope = await DBMemScope.filter(id=scope.id).using_db(conn).select_for_update().get()
            grant = await DBMemScopeGrant.create(
                scope_id=scope.id, audience_chat_key=audience_chat_key, purpose="answer",
                granted_by=context.actor_key, valid_until=valid_until, using_db=conn,
            )
            await DBMemScope.filter(id=scope.id).using_db(conn).update(revision=scope.revision + 1)
            await self._control_event(scope, context, grant.id, "scope.granted", conn)
            return grant.id

    async def revoke_grant(self, context: RequestContext, grant_id: UUID) -> None:
        if not context.is_admin or context.purpose != "admin_review":
            raise MemoryError("SCOPE_DENIED")
        scope = await self.resolve_scope(context)
        async with in_transaction() as conn:
            scope = await DBMemScope.filter(id=scope.id).using_db(conn).select_for_update().get()
            grant = await DBMemScopeGrant.filter(id=grant_id, scope_id=scope.id).using_db(conn).first()
            if grant is None:
                raise MemoryError("SCOPE_DENIED")
            if grant.revoked_at is None:
                await DBMemScopeGrant.filter(id=grant.id).using_db(conn).update(revoked_at=datetime.now(timezone.utc))
                await DBMemScope.filter(id=scope.id).using_db(conn).update(revision=scope.revision + 1)
                await self._control_event(scope, context, grant.id, "scope.revoked", conn)

    async def list_facts(
        self, context: RequestContext, *, subject_key: str | None = None, predicate: str | None = None,
        status: FactStatus | None = FactStatus.ACTIVE, limit: int = 50, offset: int = 0,
    ) -> FactPage:
        """管理面列表；默认只列当前生效版本，历史版本需要显式查询。"""
        if not 1 <= limit <= 200 or offset < 0:
            raise MemoryError("LIMIT_INVALID")
        scopes = {scope.id: scope for scope in await self._visible_scopes(context)}
        rows = DBMemAssertion.filter(scope_id__in=list(scopes))
        if status is not None:
            rows = rows.filter(status=status)
        if subject_key is not None:
            rows = rows.filter(subject_key=subject_key)
        if predicate is not None:
            rows = rows.filter(predicate=predicate)
        total = await rows.count()
        items: list[FactRecord] = []
        async for row in rows.order_by("-recorded_at").offset(offset).limit(limit):
            items.append(await self._fact_record(row, scopes[row.scope_id]))
        return FactPage(
            items=items, total=total, policy_revisions={str(s.id): s.revision for s in scopes.values()},
        )

    async def get_fact(self, context: RequestContext, assertion_id: UUID) -> FactDetail:
        """详情含同逻辑键全部版本，从旧到新，便于展示纠正链路。"""
        scopes = {scope.id: scope for scope in await self._visible_scopes(context)}
        target = await DBMemAssertion.filter(id=assertion_id).first()
        if target is None or target.scope_id not in scopes:
            raise MemoryError("SCOPE_DENIED")
        scope = scopes[target.scope_id]
        versions = [
            await self._fact_record(row, scope)
            async for row in DBMemAssertion.filter(
                scope_id=scope.id, logical_key=target.logical_key,
            ).order_by("semantic_version")
        ]
        current_row = await DBMemAssertion.filter(
            scope_id=scope.id, logical_key=target.logical_key, status=FactStatus.ACTIVE,
        ).order_by("-semantic_version").first()
        current = await self._fact_record(current_row or target, scope)
        return FactDetail(current=current, versions=versions)

    async def correct_fact(
        self, context: RequestContext, assertion_id: UUID, *, value: JsonValue,
        confidence: float | None = None, min_favor: int | None = None,
        expected_row_revision: int | None = None, reason: str = "",
    ) -> ResolutionResult:
        """管理员纠正走同一事实服务，保留旧版本并写审计；不做静默覆盖。"""
        if not context.is_admin or context.purpose != "admin_review":
            raise MemoryError("SCOPE_DENIED")
        scopes = {scope.id: scope for scope in await self._visible_scopes(context)}
        target = await DBMemAssertion.filter(id=assertion_id).first()
        if target is None or target.scope_id not in scopes:
            raise MemoryError("SCOPE_DENIED")
        target_scope = scopes[target.scope_id]
        current = await DBMemAssertion.filter(
            scope_id=target.scope_id, logical_key=target.logical_key, status=FactStatus.ACTIVE,
        ).order_by("-semantic_version").first()
        # 管理纠正只能作用于当前生效版本；历史版本要先看详情再定位到当前值。
        if current is not None and current.id != target.id:
            raise MemoryError("VERSION_CONFLICT")
        if expected_row_revision is not None and target.row_revision != expected_row_revision:
            raise MemoryError("VERSION_CONFLICT")
        proposal = FactProposal(
            subject_key=target.subject_key, predicate=target.predicate, value=value,
            confidence=target.confidence if confidence is None else confidence,
            min_favor=target.min_favor if min_favor is None else min_favor,
            source=SourceRef(
                kind="admin_edit",
                id=f"admin:{_digest([context.actor_key, str(assertion_id)])[:32]}",
                revision=_digest([str(assertion_id), value, expected_row_revision])[:32],
            ),
        )
        # 目标事实可能属于别的会话 scope；写入身份仍固定为管理员审核用途。
        scope_context = context.model_copy(update={"chat_key": target_scope.origin_chat_key})
        result = await self.propose_fact(
            scope_context, proposal,
            idempotency_key=f"admin_edit:{assertion_id}:{expected_row_revision}:{_digest(value)[:16]}",
            expected_row_revision=expected_row_revision,
        )
        await DBMemAuditEvent.create(
            scope_id=target.scope_id, actor_key=context.actor_key, operation="fact.admin_edited",
            target_id=result.assertion_id, reason_code=(reason or "admin_edit")[:64],
        )
        return result

    async def list_grants(self, context: RequestContext) -> list[ScopeGrantRecord]:
        """管理员查看当前工作区的跨会话授权。"""
        if not context.is_admin or context.purpose != "admin_review":
            raise MemoryError("SCOPE_DENIED")
        scopes = {scope.id: scope for scope in await self._visible_scopes(context)}
        now = datetime.now(timezone.utc)
        records: list[ScopeGrantRecord] = []
        async for grant in DBMemScopeGrant.filter(scope_id__in=list(scopes)).order_by("audience_chat_key"):
            scope = scopes[grant.scope_id]
            records.append(ScopeGrantRecord(
                grant_id=grant.id, scope_id=scope.id, chat_key=scope.origin_chat_key,
                audience_chat_key=grant.audience_chat_key, purpose=grant.purpose,
                granted_by=grant.granted_by, valid_until=grant.valid_until, revoked_at=grant.revoked_at,
                active=grant.revoked_at is None and (grant.valid_until is None or grant.valid_until > now),
            ))
        return records

    async def _fact_record(self, row: DBMemAssertion, scope: DBMemScope) -> FactRecord:
        return FactRecord(
            assertion_id=row.id, scope_id=row.scope_id, chat_key=scope.origin_chat_key,
            subject_key=row.subject_key, predicate=row.predicate, value=row.value_json["value"],
            status=FactStatus(row.status), confidence=row.confidence, min_favor=row.min_favor,
            verification_status=row.verification_status, semantic_version=row.semantic_version,
            row_revision=row.row_revision, valid_from=row.valid_from, valid_to=row.valid_to,
            supersedes_id=row.supersedes_id, recorded_at=row.recorded_at,
            source_refs=await self._valid_sources(row.id, scope),
            index_pending=await DBMemIndexOutbox.filter(assertion_id=row.id).exclude(state="done").exists(),
        )

    async def _control_event(
        self, scope: DBMemScope, context: RequestContext, target: UUID, operation: str, conn: BaseDBAsyncClient,
    ) -> None:
        await DBMemAuditEvent.create(
            scope_id=scope.id, actor_key=context.actor_key, operation=operation,
            target_id=target, reason_code="admin_policy", using_db=conn,
        )
        await DBMemDomainOutbox.create(
            scope_id=scope.id, event_type=operation, aggregate_id=target,
            aggregate_revision=scope.revision + 1, payload_json={"schema_version": 1}, using_db=conn,
        )
