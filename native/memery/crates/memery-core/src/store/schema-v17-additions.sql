-- Inactive S01 candidate. Explicit migration integration only; v16 remains authoritative.
-- Does not change user_version or replace the v16 typed-value trigger.
CREATE TABLE fm_v2_revision_metadata (
    owner_id TEXT NOT NULL,
    block_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    metadata_schema TEXT NOT NULL CHECK (metadata_schema = 'openclank.memory-revision-metadata.v1'),
    metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json) AND json_type(metadata_json) = 'object'),
    metadata_hash TEXT NOT NULL CHECK (length(metadata_hash) = 64 AND metadata_hash NOT GLOB '*[^0-9a-f]*'),
    actor_type TEXT NOT NULL CHECK (length(trim(actor_type)) > 0),
    actor_id TEXT NOT NULL CHECK (length(trim(actor_id)) > 0),
    change_set_id TEXT NOT NULL,
    source_event_id TEXT,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json) AND json_type(evidence_json) = 'array'),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    PRIMARY KEY (owner_id, block_id, revision),
    FOREIGN KEY (owner_id, block_id, revision) REFERENCES fm_v2_knowledge_revisions(owner_id, block_id, revision),
    FOREIGN KEY (owner_id, change_set_id) REFERENCES fm_v2_change_sets(owner_id, change_set_id)
);
CREATE TRIGGER fm_v2_revision_metadata_validate BEFORE INSERT ON fm_v2_revision_metadata
WHEN EXISTS (SELECT 1 FROM fm_v2_revision_metadata existing
    WHERE existing.owner_id=NEW.owner_id AND existing.block_id=NEW.block_id AND existing.revision=NEW.revision)
  OR NOT EXISTS (
    SELECT 1 FROM fm_v2_knowledge_revisions r
    JOIN fm_v2_change_sets cs ON cs.owner_id=r.owner_id AND cs.change_set_id=r.change_set_id
    WHERE cs.actor_type=NEW.actor_type AND cs.actor_id=NEW.actor_id
      AND r.owner_id=NEW.owner_id AND r.block_id=NEW.block_id AND r.revision=NEW.revision
      AND r.change_set_id=NEW.change_set_id AND r.actor_type=NEW.actor_type AND r.actor_id=NEW.actor_id
      AND r.source_event_id IS NEW.source_event_id AND r.evidence_json=NEW.evidence_json
) OR EXISTS (SELECT 1 FROM json_each(NEW.metadata_json) WHERE key NOT IN ('name','aliases','domain_refs','types','processing_role'))
  OR (json_type(NEW.metadata_json,'$.name') IS NOT NULL AND json_type(NEW.metadata_json,'$.name') <> 'text')
  OR (json_type(NEW.metadata_json,'$.processing_role') IS NOT NULL AND json_type(NEW.metadata_json,'$.processing_role') <> 'text')
  OR (json_type(NEW.metadata_json,'$.name') = 'text' AND (length(trim(json_extract(NEW.metadata_json,'$.name'))) = 0 OR json_extract(NEW.metadata_json,'$.name') <> trim(json_extract(NEW.metadata_json,'$.name'))))
  OR (json_type(NEW.metadata_json,'$.processing_role') = 'text' AND (length(trim(json_extract(NEW.metadata_json,'$.processing_role'))) = 0 OR json_extract(NEW.metadata_json,'$.processing_role') <> trim(json_extract(NEW.metadata_json,'$.processing_role'))))
  OR (json_type(NEW.metadata_json,'$.aliases') IS NOT NULL AND json_type(NEW.metadata_json,'$.aliases') <> 'array')
  OR (json_type(NEW.metadata_json,'$.domain_refs') IS NOT NULL AND json_type(NEW.metadata_json,'$.domain_refs') <> 'array')
  OR (json_type(NEW.metadata_json,'$.types') IS NOT NULL AND json_type(NEW.metadata_json,'$.types') <> 'array')
  OR EXISTS (SELECT 1 FROM json_each(NEW.metadata_json,'$.aliases') WHERE type <> 'text' OR length(trim(value))=0 OR value <> trim(value))
  OR EXISTS (
    SELECT 1 FROM json_each(NEW.metadata_json,'$.domain_refs') d WHERE CASE WHEN d.type <> 'object' THEN 1 ELSE (
      json_type(d.value,'$.block_id') IS NOT 'text'
      OR length(trim(json_extract(d.value,'$.block_id'), char(9)||char(10)||char(11)||char(12)||char(13)||char(28)||char(29)||char(30)||char(31)||char(32)||char(133)||char(160)||char(5760)||char(8192)||char(8193)||char(8194)||char(8195)||char(8196)||char(8197)||char(8198)||char(8199)||char(8200)||char(8201)||char(8202)||char(8232)||char(8233)||char(8239)||char(8287)||char(12288)))=0
      OR json_extract(d.value,'$.block_id') <> trim(json_extract(d.value,'$.block_id'), char(9)||char(10)||char(11)||char(12)||char(13)||char(28)||char(29)||char(30)||char(31)||char(32)||char(133)||char(160)||char(5760)||char(8192)||char(8193)||char(8194)||char(8195)||char(8196)||char(8197)||char(8198)||char(8199)||char(8200)||char(8201)||char(8202)||char(8232)||char(8233)||char(8239)||char(8287)||char(12288))
      OR json_type(d.value,'$.revision') IS NOT 'integer' OR json_extract(d.value,'$.revision') < 1
      OR (SELECT count(*) FROM json_each(d.value)) <> 2
      OR EXISTS (SELECT 1 FROM json_each(d.value) WHERE key NOT IN ('block_id','revision'))
      OR EXISTS (SELECT key FROM json_each(d.value) GROUP BY key HAVING count(*) > 1)
      OR NOT EXISTS (
        SELECT 1 FROM fm_v2_knowledge_blocks source
        JOIN fm_v2_knowledge_blocks target ON target.owner_id=source.owner_id
          AND target.block_id=json_extract(d.value,'$.block_id')
        JOIN fm_v2_knowledge_revisions ref ON ref.owner_id=target.owner_id AND ref.block_id=target.block_id
          AND ref.revision=json_extract(d.value,'$.revision')
        WHERE source.owner_id=NEW.owner_id AND source.block_id=NEW.block_id
          AND ((target.workspace_key='' AND target.project_key='') OR
            (source.workspace_key<>'' AND target.workspace_key=source.workspace_key AND
              (target.project_key='' OR (source.project_key<>'' AND target.project_key=source.project_key))))
      )
    ) END
  )
  OR EXISTS (SELECT 1 FROM json_each(NEW.metadata_json,'$.types') WHERE type <> 'text' OR length(trim(value))=0 OR value <> trim(value))
  OR EXISTS (SELECT key FROM json_each(NEW.metadata_json) GROUP BY key HAVING count(*) > 1)
  OR EXISTS (SELECT value FROM json_each(NEW.metadata_json,'$.aliases') GROUP BY value HAVING count(*) > 1)
  OR EXISTS (SELECT json_extract(value,'$.block_id'),json_extract(value,'$.revision') FROM json_each(NEW.metadata_json,'$.domain_refs') GROUP BY 1,2 HAVING count(*) > 1)
  OR EXISTS (SELECT value FROM json_each(NEW.metadata_json,'$.types') GROUP BY value HAVING count(*) > 1)
BEGIN SELECT RAISE(ABORT, 'invalid revision metadata'); END;
CREATE TRIGGER fm_v2_revision_metadata_no_update BEFORE UPDATE ON fm_v2_revision_metadata
BEGIN SELECT RAISE(ABORT, 'revision metadata is append-only'); END;
CREATE TRIGGER fm_v2_revision_metadata_no_delete BEFORE DELETE ON fm_v2_revision_metadata
WHEN NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=OLD.owner_id)
BEGIN SELECT RAISE(ABORT, 'revision metadata is append-only'); END;

-- Additional check only; the existing v16 whitelist still rejects knowledge_ref.
-- Coordinated v17 integration must explicitly replace that whitelist, retaining
-- every existing check, before the new variant can be written.
CREATE TRIGGER fm_v2_knowledge_ref_validate BEFORE INSERT ON fm_v2_knowledge_revisions
WHEN NEW.value_type='knowledge_ref' AND CASE WHEN json_valid(NEW.value_json)=0 THEN 1 ELSE (
    json_type(NEW.value_json) <> 'object' OR json_extract(NEW.value_json,'$.type') IS NOT 'knowledge_ref'
    OR json_type(NEW.value_json,'$.value') IS NOT 'object'
    OR (SELECT count(*) FROM json_each(NEW.value_json)) <> 2
    OR EXISTS (SELECT 1 FROM json_each(NEW.value_json) WHERE key NOT IN ('type','value'))
    OR EXISTS (SELECT 1 FROM json_each(NEW.value_json,'$.value') WHERE key NOT IN ('block_id','revision'))
    OR EXISTS (SELECT key FROM json_each(NEW.value_json) GROUP BY key HAVING count(*) > 1)
    OR EXISTS (SELECT key FROM json_each(NEW.value_json,'$.value') GROUP BY key HAVING count(*) > 1)
    OR json_type(NEW.value_json,'$.value.block_id') IS NOT 'text'
    OR length(trim(json_extract(NEW.value_json,'$.value.block_id'), char(9)||char(10)||char(11)||char(12)||char(13)||char(28)||char(29)||char(30)||char(31)||char(32)||char(133)||char(160)||char(5760)||char(8192)||char(8193)||char(8194)||char(8195)||char(8196)||char(8197)||char(8198)||char(8199)||char(8200)||char(8201)||char(8202)||char(8232)||char(8233)||char(8239)||char(8287)||char(12288))) = 0
    OR json_extract(NEW.value_json,'$.value.block_id') <> trim(json_extract(NEW.value_json,'$.value.block_id'), char(9)||char(10)||char(11)||char(12)||char(13)||char(28)||char(29)||char(30)||char(31)||char(32)||char(133)||char(160)||char(5760)||char(8192)||char(8193)||char(8194)||char(8195)||char(8196)||char(8197)||char(8198)||char(8199)||char(8200)||char(8201)||char(8202)||char(8232)||char(8233)||char(8239)||char(8287)||char(12288))
    OR (json_type(NEW.value_json,'$.value.revision') IS NOT NULL AND (
        json_type(NEW.value_json,'$.value.revision') <> 'integer' OR json_extract(NEW.value_json,'$.value.revision') < 1))
    OR NOT EXISTS (
        SELECT 1 FROM fm_v2_knowledge_blocks source
        JOIN fm_v2_knowledge_blocks target ON target.owner_id=source.owner_id
          AND target.block_id=json_extract(NEW.value_json,'$.value.block_id')
        WHERE source.owner_id=NEW.owner_id AND source.block_id=NEW.block_id
          AND ((target.workspace_key='' AND target.project_key='') OR
               (source.workspace_key<>'' AND target.workspace_key=source.workspace_key AND
                (target.project_key='' OR (source.project_key<>'' AND target.project_key=source.project_key))))
          AND EXISTS (SELECT 1 FROM fm_v2_knowledge_revisions ref
            WHERE ref.owner_id=target.owner_id AND ref.block_id=target.block_id
              AND ref.revision=COALESCE(json_extract(NEW.value_json,'$.value.revision'),target.current_revision))
    )
) END
BEGIN SELECT RAISE(ABORT, 'knowledge reference unavailable'); END;
