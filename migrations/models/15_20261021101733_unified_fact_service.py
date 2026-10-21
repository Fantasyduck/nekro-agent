from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "mem_assertion" (
    "id" UUID NOT NULL PRIMARY KEY,
    "scope_id" UUID NOT NULL,
    "logical_key" VARCHAR(64) NOT NULL,
    "subject_key" VARCHAR(256) NOT NULL,
    "predicate" VARCHAR(128) NOT NULL,
    "value_json" JSONB NOT NULL,
    "semantic_version" INT NOT NULL,
    "row_revision" INT NOT NULL DEFAULT 1,
    "status" VARCHAR(24) NOT NULL,
    "confidence" DOUBLE PRECISION NOT NULL,
    "verification_status" VARCHAR(32) NOT NULL,
    "valid_from" TIMESTAMPTZ NOT NULL,
    "valid_to" TIMESTAMPTZ,
    "supersedes_id" UUID,
    "recorded_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_mem_asserti_scope_i_c35017" UNIQUE ("scope_id", "logical_key", "semantic_version")
);
CREATE INDEX IF NOT EXISTS "idx_mem_asserti_scope_i_060218" ON "mem_assertion" ("scope_id");
CREATE INDEX IF NOT EXISTS "idx_mem_asserti_logical_032ea3" ON "mem_assertion" ("logical_key");
CREATE INDEX IF NOT EXISTS "idx_mem_asserti_subject_971a21" ON "mem_assertion" ("subject_key");
CREATE INDEX IF NOT EXISTS "idx_mem_asserti_status_c4af9b" ON "mem_assertion" ("status");
        CREATE TABLE IF NOT EXISTS "mem_assertion_evidence" (
    "id" SERIAL NOT NULL PRIMARY KEY,
    "assertion_id" UUID NOT NULL,
    "observation_id" UUID NOT NULL,
    CONSTRAINT "uid_mem_asserti_asserti_1fa340" UNIQUE ("assertion_id", "observation_id")
);
CREATE INDEX IF NOT EXISTS "idx_mem_asserti_asserti_33b77c" ON "mem_assertion_evidence" ("assertion_id");
CREATE INDEX IF NOT EXISTS "idx_mem_asserti_observa_8f1138" ON "mem_assertion_evidence" ("observation_id");
        CREATE TABLE IF NOT EXISTS "mem_audit_event" (
    "id" UUID NOT NULL PRIMARY KEY,
    "scope_id" UUID NOT NULL,
    "actor_key" VARCHAR(256) NOT NULL,
    "operation" VARCHAR(40) NOT NULL,
    "target_id" UUID NOT NULL,
    "reason_code" VARCHAR(64) NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_mem_audit_e_scope_i_c50249" ON "mem_audit_event" ("scope_id");
        CREATE TABLE IF NOT EXISTS "mem_domain_outbox" (
    "id" UUID NOT NULL PRIMARY KEY,
    "scope_id" UUID NOT NULL,
    "event_type" VARCHAR(40) NOT NULL,
    "aggregate_id" UUID NOT NULL,
    "aggregate_revision" INT NOT NULL,
    "payload_json" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_mem_domain__scope_i_ce53de" ON "mem_domain_outbox" ("scope_id");
        CREATE TABLE IF NOT EXISTS "mem_event_delivery" (
    "id" UUID NOT NULL PRIMARY KEY,
    "event_id" UUID NOT NULL,
    "consumer_id" VARCHAR(128) NOT NULL,
    "state" VARCHAR(24) NOT NULL DEFAULT 'pending',
    "lease_token" UUID,
    "lease_until" TIMESTAMPTZ,
    "attempts" INT NOT NULL DEFAULT 0,
    "next_retry_at" TIMESTAMPTZ,
    CONSTRAINT "uid_mem_event_d_event_i_ff116c" UNIQUE ("event_id", "consumer_id")
);
CREATE INDEX IF NOT EXISTS "idx_mem_event_d_event_i_d9a979" ON "mem_event_delivery" ("event_id");
CREATE INDEX IF NOT EXISTS "idx_mem_event_d_state_5bc25c" ON "mem_event_delivery" ("state");
        CREATE TABLE IF NOT EXISTS "mem_index_outbox" (
    "id" UUID NOT NULL PRIMARY KEY,
    "assertion_id" UUID NOT NULL,
    "assertion_version" INT NOT NULL,
    "operation" VARCHAR(24) NOT NULL,
    "state" VARCHAR(24) NOT NULL DEFAULT 'pending',
    "attempts" INT NOT NULL DEFAULT 0,
    "next_retry_at" TIMESTAMPTZ,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_mem_index_o_asserti_354362" ON "mem_index_outbox" ("assertion_id");
CREATE INDEX IF NOT EXISTS "idx_mem_index_o_state_58b871" ON "mem_index_outbox" ("state");
        CREATE TABLE IF NOT EXISTS "mem_legacy_map" (
    "id" UUID NOT NULL PRIMARY KEY,
    "legacy_store" VARCHAR(128) NOT NULL,
    "legacy_table" VARCHAR(64) NOT NULL,
    "legacy_id" VARCHAR(256) NOT NULL,
    "assertion_id" UUID,
    "checksum" VARCHAR(64) NOT NULL,
    "import_status" VARCHAR(24) NOT NULL,
    CONSTRAINT "uid_mem_legacy__legacy__7b54ac" UNIQUE ("legacy_store", "legacy_table", "legacy_id")
);
        CREATE TABLE IF NOT EXISTS "mem_observation" (
    "id" UUID NOT NULL PRIMARY KEY,
    "scope_id" UUID NOT NULL,
    "source_kind" VARCHAR(32) NOT NULL,
    "source_id" VARCHAR(256) NOT NULL,
    "source_revision" VARCHAR(64) NOT NULL,
    "speaker_key" VARCHAR(256) NOT NULL,
    "observed_at" TIMESTAMPTZ NOT NULL,
    "status" VARCHAR(24) NOT NULL DEFAULT 'valid',
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "uid_mem_observa_scope_i_614b1c" UNIQUE ("scope_id", "source_kind", "source_id", "source_revision")
);
CREATE INDEX IF NOT EXISTS "idx_mem_observa_scope_i_454881" ON "mem_observation" ("scope_id");
        CREATE TABLE IF NOT EXISTS "mem_scope" (
    "id" UUID NOT NULL PRIMARY KEY,
    "workspace_id" INT NOT NULL,
    "agent_id" VARCHAR(128) NOT NULL,
    "origin_chat_key" VARCHAR(256) NOT NULL,
    "revision" INT NOT NULL DEFAULT 1,
    CONSTRAINT "uid_mem_scope_workspa_d1bbfc" UNIQUE ("workspace_id", "agent_id", "origin_chat_key")
);
CREATE INDEX IF NOT EXISTS "idx_mem_scope_workspa_734c8f" ON "mem_scope" ("workspace_id");
        CREATE TABLE IF NOT EXISTS "mem_scope_grant" (
    "id" UUID NOT NULL PRIMARY KEY,
    "scope_id" UUID NOT NULL,
    "audience_chat_key" VARCHAR(256) NOT NULL,
    "purpose" VARCHAR(24) NOT NULL,
    "granted_by" VARCHAR(256) NOT NULL,
    "valid_until" TIMESTAMPTZ,
    "revoked_at" TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS "idx_mem_scope_g_scope_i_1af5cc" ON "mem_scope_grant" ("scope_id");
        CREATE TABLE IF NOT EXISTS "mem_write_request" (
    "id" VARCHAR(64) NOT NULL PRIMARY KEY,
    "scope_id" UUID NOT NULL,
    "fingerprint" VARCHAR(64) NOT NULL,
    "result_json" JSONB NOT NULL,
    "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS "idx_mem_write_r_scope_i_b562dc" ON "mem_write_request" ("scope_id");
COMMENT ON TABLE "mem_write_request" IS '事实命令幂等槽，阻止迟到的重试重新发布旧值。';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "mem_audit_event";
        DROP TABLE IF EXISTS "mem_scope";
        DROP TABLE IF EXISTS "mem_legacy_map";
        DROP TABLE IF EXISTS "mem_index_outbox";
        DROP TABLE IF EXISTS "mem_assertion";
        DROP TABLE IF EXISTS "mem_assertion_evidence";
        DROP TABLE IF EXISTS "mem_write_request";
        DROP TABLE IF EXISTS "mem_event_delivery";
        DROP TABLE IF EXISTS "mem_scope_grant";
        DROP TABLE IF EXISTS "mem_observation";
        DROP TABLE IF EXISTS "mem_domain_outbox";"""


MODELS_STATE = (
    "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"
)
