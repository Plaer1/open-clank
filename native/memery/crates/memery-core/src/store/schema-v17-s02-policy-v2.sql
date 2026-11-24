-- Optional offline-only S02 v2; retain unchanged v1 base.
DROP TRIGGER fm_v2_revision_metadata_validate;
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
) OR EXISTS (SELECT 1 FROM json_each(NEW.metadata_json) WHERE key NOT IN ('name','aliases','domain_refs','types','processing_role','policy_overrides'))
  OR (json_type(NEW.metadata_json,'$.policy_overrides') IS NOT NULL AND json_type(NEW.metadata_json,'$.policy_overrides') <> 'object')
  OR EXISTS (SELECT 1 FROM json_each(NEW.metadata_json,'$.policy_overrides') WHERE key NOT IN ('importance','connections','consolidation') OR (key='importance' AND (type NOT IN ('null','integer','real') OR (type IN ('integer','real') AND (value<0 OR value>1)))) OR (key IN ('connections','consolidation') AND type NOT IN ('null','true','false')))
  OR EXISTS (SELECT key FROM json_each(NEW.metadata_json,'$.policy_overrides') GROUP BY key HAVING count(*)>1)
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
CREATE TRIGGER s02_override_metadata_guard BEFORE INSERT ON fm_v2_revision_metadata WHEN json_type(NEW.metadata_json,'$.policy_overrides') IS NOT NULL AND fm_s02_policy_authorized() IS NOT 1 BEGIN SELECT RAISE(ABORT,'S02 override requires guarded writer'); END;
CREATE TRIGGER s02_override_revision_guard BEFORE INSERT ON fm_v2_knowledge_revisions WHEN EXISTS (SELECT 1 FROM fm_v2_revision_metadata m WHERE m.owner_id=NEW.owner_id AND m.block_id=NEW.block_id AND json_type(m.metadata_json,'$.policy_overrides') IS NOT NULL) AND fm_s02_policy_authorized() IS NOT 1 BEGIN SELECT RAISE(ABORT,'S02 override successor requires guarded writer'); END;
