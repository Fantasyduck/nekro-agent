"""统一事实领域的增量模型；不接管天使关系状态。"""

from uuid import uuid4

from tortoise import fields
from tortoise.models import Model


class DBMemScope(Model):
    id = fields.UUIDField(pk=True)
    workspace_id = fields.IntField(index=True)
    agent_id = fields.CharField(max_length=128)
    origin_chat_key = fields.CharField(max_length=256)
    revision = fields.IntField(default=1)
    pipeline_mode = fields.CharField(max_length=24, default="legacy_guarded")
    writer_revision = fields.IntField(default=1)

    class Meta:
        table = "mem_scope"
        unique_together = (("workspace_id", "agent_id", "origin_chat_key"),)


class DBMemObservation(Model):
    id = fields.UUIDField(pk=True, default=uuid4)
    scope_id = fields.UUIDField(index=True)
    source_kind = fields.CharField(max_length=32)
    source_id = fields.CharField(max_length=256)
    source_revision = fields.CharField(max_length=64)
    speaker_key = fields.CharField(max_length=256)
    observed_at = fields.DatetimeField()
    status = fields.CharField(max_length=24, default="valid")
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mem_observation"
        unique_together = (("scope_id", "source_kind", "source_id", "source_revision"),)


class DBMemAssertion(Model):
    id = fields.UUIDField(pk=True, default=uuid4)
    scope_id = fields.UUIDField(index=True)
    logical_key = fields.CharField(max_length=64, index=True)
    subject_key = fields.CharField(max_length=256, index=True)
    predicate = fields.CharField(max_length=128)
    value_json = fields.JSONField()
    semantic_version = fields.IntField()
    row_revision = fields.IntField(default=1)
    status = fields.CharField(max_length=24, index=True)
    confidence = fields.FloatField()
    verification_status = fields.CharField(max_length=32)
    valid_from = fields.DatetimeField()
    valid_to = fields.DatetimeField(null=True)
    supersedes_id = fields.UUIDField(null=True)
    recorded_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mem_assertion"
        unique_together = (("scope_id", "logical_key", "semantic_version"),)


class DBMemAssertionEvidence(Model):
    id = fields.IntField(pk=True)
    assertion_id = fields.UUIDField(index=True)
    observation_id = fields.UUIDField(index=True)

    class Meta:
        table = "mem_assertion_evidence"
        unique_together = (("assertion_id", "observation_id"),)


class DBMemScopeGrant(Model):
    id = fields.UUIDField(pk=True, default=uuid4)
    scope_id = fields.UUIDField(index=True)
    audience_chat_key = fields.CharField(max_length=256)
    purpose = fields.CharField(max_length=24)
    granted_by = fields.CharField(max_length=256)
    valid_until = fields.DatetimeField(null=True)
    revoked_at = fields.DatetimeField(null=True)

    class Meta:
        table = "mem_scope_grant"


class DBMemWriteRequest(Model):
    """事实命令幂等槽，阻止迟到的重试重新发布旧值。"""

    id = fields.CharField(pk=True, max_length=64)
    scope_id = fields.UUIDField(index=True)
    fingerprint = fields.CharField(max_length=64)
    result_json = fields.JSONField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mem_write_request"


class DBMemWriteJob(Model):
    id = fields.UUIDField(pk=True, default=uuid4)
    scope_id = fields.UUIDField(index=True)
    observation_id = fields.UUIDField(index=True)
    pipeline_version = fields.CharField(max_length=64)
    writer_revision = fields.IntField()
    mode = fields.CharField(max_length=24)
    state = fields.CharField(max_length=24, default="pending", index=True)
    lease_token = fields.UUIDField(null=True)
    lease_until = fields.DatetimeField(null=True)
    attempts = fields.IntField(default=0)
    next_retry_at = fields.DatetimeField(null=True)
    error_class = fields.CharField(max_length=64, null=True)
    shadow_result = fields.JSONField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mem_write_job"
        unique_together = (("scope_id", "observation_id", "pipeline_version", "writer_revision"),)


class DBMemIndexOutbox(Model):
    id = fields.UUIDField(pk=True, default=uuid4)
    assertion_id = fields.UUIDField(index=True)
    assertion_version = fields.IntField()
    operation = fields.CharField(max_length=24)
    state = fields.CharField(max_length=24, default="pending", index=True)
    attempts = fields.IntField(default=0)
    next_retry_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mem_index_outbox"


class DBMemDomainOutbox(Model):
    id = fields.UUIDField(pk=True, default=uuid4)
    scope_id = fields.UUIDField(index=True)
    event_type = fields.CharField(max_length=40)
    aggregate_id = fields.UUIDField()
    aggregate_revision = fields.IntField()
    payload_json = fields.JSONField()
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mem_domain_outbox"


class DBMemEventDelivery(Model):
    id = fields.UUIDField(pk=True, default=uuid4)
    event_id = fields.UUIDField(index=True)
    consumer_id = fields.CharField(max_length=128)
    state = fields.CharField(max_length=24, default="pending", index=True)
    lease_token = fields.UUIDField(null=True)
    lease_until = fields.DatetimeField(null=True)
    attempts = fields.IntField(default=0)
    next_retry_at = fields.DatetimeField(null=True)

    class Meta:
        table = "mem_event_delivery"
        unique_together = (("event_id", "consumer_id"),)


class DBMemAuditEvent(Model):
    id = fields.UUIDField(pk=True, default=uuid4)
    scope_id = fields.UUIDField(index=True)
    actor_key = fields.CharField(max_length=256)
    operation = fields.CharField(max_length=40)
    target_id = fields.UUIDField()
    reason_code = fields.CharField(max_length=64)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "mem_audit_event"


class DBMemLegacyMap(Model):
    id = fields.UUIDField(pk=True, default=uuid4)
    legacy_store = fields.CharField(max_length=128)
    legacy_table = fields.CharField(max_length=64)
    legacy_id = fields.CharField(max_length=256)
    assertion_id = fields.UUIDField(null=True)
    checksum = fields.CharField(max_length=64)
    import_status = fields.CharField(max_length=24)

    class Meta:
        table = "mem_legacy_map"
        unique_together = (("legacy_store", "legacy_table", "legacy_id"),)
