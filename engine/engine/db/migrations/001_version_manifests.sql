-- The full configuration behind each opaque agent_version hash, so two versions can be
-- diffed field-by-field (the investigation agent's "what changed?" step). The eval engine
-- owns this upsert: it has the full config via the agent contract + compute_agent_version +
-- git_sha, while the ingest consumer only ever sees the opaque hash on spans.
-- (Design doc §2.1, data-model §3.6.)

CREATE TABLE IF NOT EXISTS version_manifests (
    agent_id      text        NOT NULL,
    agent_version text        NOT NULL,   -- the content hash
    git_sha       text        NOT NULL,
    model         text        NOT NULL,
    params        jsonb       NOT NULL,   -- decoding params
    prompts       jsonb       NOT NULL,   -- prompt templates {name -> text}
    tools         jsonb       NOT NULL,   -- tool/function definitions
    code_ref      jsonb,                  -- optional: file hashes, entrypoint, deps
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, agent_version)
);
