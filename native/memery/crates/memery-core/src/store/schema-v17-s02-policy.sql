-- Optional offline-only S02 profile. Existing FM knowledge blocks are the sole definition and target identities.
CREATE TABLE fm_v2_s02_generation (
 owner_id TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK(generation>=1)
);
CREATE TABLE fm_v2_s02_definition_policy (
 owner_id TEXT NOT NULL, block_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 definition_kind TEXT NOT NULL CHECK(definition_kind IN ('type','tag')),
 policies_json TEXT NOT NULL CHECK(json_valid(policies_json) AND json_type(policies_json)='array'),
 policies_sha256 TEXT NOT NULL CHECK(length(policies_sha256)=64 AND policies_sha256 NOT GLOB '*[^0-9a-f]*'),
 change_set_id TEXT NOT NULL,
 PRIMARY KEY(owner_id,block_id,revision),
 FOREIGN KEY(owner_id,block_id,revision) REFERENCES fm_v2_knowledge_revisions(owner_id,block_id,revision),
 FOREIGN KEY(owner_id,change_set_id) REFERENCES fm_v2_change_sets(owner_id,change_set_id)
);
CREATE TABLE fm_v2_s02_edge_heads (
 owner_id TEXT NOT NULL, edge_id TEXT NOT NULL, current_revision INTEGER NOT NULL CHECK(current_revision>0),
 PRIMARY KEY(owner_id,edge_id)
);
CREATE TABLE fm_v2_s02_edge_revisions (
 owner_id TEXT NOT NULL, edge_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 child_block_id TEXT NOT NULL, parent_block_id TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)),
 change_set_id TEXT NOT NULL, previous_revision INTEGER,
 PRIMARY KEY(owner_id,edge_id,revision),
 FOREIGN KEY(owner_id,edge_id) REFERENCES fm_v2_s02_edge_heads(owner_id,edge_id),
 FOREIGN KEY(owner_id,child_block_id) REFERENCES fm_v2_knowledge_blocks(owner_id,block_id),
 FOREIGN KEY(owner_id,parent_block_id) REFERENCES fm_v2_knowledge_blocks(owner_id,block_id),
 FOREIGN KEY(owner_id,change_set_id) REFERENCES fm_v2_change_sets(owner_id,change_set_id),
 CHECK(child_block_id<>parent_block_id), CHECK(previous_revision IS NULL OR previous_revision=revision-1)
);
CREATE INDEX s02_edge_child ON fm_v2_s02_edge_revisions(owner_id,child_block_id,edge_id,revision);
CREATE TABLE fm_v2_s02_assignment_heads (
 owner_id TEXT NOT NULL, assignment_id TEXT NOT NULL, current_revision INTEGER NOT NULL CHECK(current_revision>0),
 PRIMARY KEY(owner_id,assignment_id)
);
CREATE TABLE fm_v2_s02_assignment_revisions (
 owner_id TEXT NOT NULL, assignment_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 target_block_id TEXT NOT NULL, definition_block_id TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)),
 change_set_id TEXT NOT NULL, previous_revision INTEGER,
 PRIMARY KEY(owner_id,assignment_id,revision),
 FOREIGN KEY(owner_id,assignment_id) REFERENCES fm_v2_s02_assignment_heads(owner_id,assignment_id),
 FOREIGN KEY(owner_id,target_block_id) REFERENCES fm_v2_knowledge_blocks(owner_id,block_id),
 FOREIGN KEY(owner_id,definition_block_id) REFERENCES fm_v2_knowledge_blocks(owner_id,block_id),
 FOREIGN KEY(owner_id,change_set_id) REFERENCES fm_v2_change_sets(owner_id,change_set_id),
 CHECK(previous_revision IS NULL OR previous_revision=revision-1)
);
CREATE INDEX s02_assignment_target ON fm_v2_s02_assignment_revisions(owner_id,target_block_id,assignment_id,revision);
CREATE TRIGGER s02_definition_head_guard BEFORE UPDATE OF current_revision ON fm_v2_knowledge_blocks WHEN NEW.current_revision<>OLD.current_revision AND EXISTS (SELECT 1 FROM fm_v2_s02_definition_policy p WHERE p.owner_id=OLD.owner_id AND p.block_id=OLD.block_id) AND fm_s02_policy_authorized() IS NOT 1 BEGIN SELECT RAISE(ABORT,'S02 definition head requires guarded writer'); END;
CREATE TRIGGER s02_generation_insert_guard BEFORE INSERT ON fm_v2_s02_generation WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_generation_update_guard BEFORE UPDATE ON fm_v2_s02_generation WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_generation_delete_guard BEFORE DELETE ON fm_v2_s02_generation WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=OLD.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_definition_policy_insert_guard BEFORE INSERT ON fm_v2_s02_definition_policy WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_definition_policy_update_guard BEFORE UPDATE ON fm_v2_s02_definition_policy WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_definition_policy_delete_guard BEFORE DELETE ON fm_v2_s02_definition_policy WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=OLD.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_edge_heads_insert_guard BEFORE INSERT ON fm_v2_s02_edge_heads WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_edge_heads_update_guard BEFORE UPDATE ON fm_v2_s02_edge_heads WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_edge_heads_delete_guard BEFORE DELETE ON fm_v2_s02_edge_heads WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=OLD.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_edge_revisions_insert_guard BEFORE INSERT ON fm_v2_s02_edge_revisions WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_edge_revisions_update_guard BEFORE UPDATE ON fm_v2_s02_edge_revisions WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_edge_revisions_delete_guard BEFORE DELETE ON fm_v2_s02_edge_revisions WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=OLD.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_assignment_heads_insert_guard BEFORE INSERT ON fm_v2_s02_assignment_heads WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_assignment_heads_update_guard BEFORE UPDATE ON fm_v2_s02_assignment_heads WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_assignment_heads_delete_guard BEFORE DELETE ON fm_v2_s02_assignment_heads WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=OLD.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_assignment_revisions_insert_guard BEFORE INSERT ON fm_v2_s02_assignment_revisions WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_assignment_revisions_update_guard BEFORE UPDATE ON fm_v2_s02_assignment_revisions WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_assignment_revisions_delete_guard BEFORE DELETE ON fm_v2_s02_assignment_revisions WHEN fm_s02_policy_authorized() IS NOT 1 AND NOT EXISTS (SELECT 1 FROM fm_v2_privacy_erasure_context c WHERE c.owner_id=OLD.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires guarded writer'); END;
CREATE TRIGGER s02_definition_policy_no_replace BEFORE INSERT ON fm_v2_s02_definition_policy WHEN EXISTS (SELECT 1 FROM fm_v2_s02_definition_policy r WHERE r.owner_id=NEW.owner_id AND r.block_id=NEW.block_id AND r.revision=NEW.revision) BEGIN SELECT RAISE(ABORT,'S02 revisions are append-only'); END;
CREATE TRIGGER s02_edge_revisions_no_replace BEFORE INSERT ON fm_v2_s02_edge_revisions WHEN EXISTS (SELECT 1 FROM fm_v2_s02_edge_revisions r WHERE r.owner_id=NEW.owner_id AND r.edge_id=NEW.edge_id AND r.revision=NEW.revision) BEGIN SELECT RAISE(ABORT,'S02 revisions are append-only'); END;
CREATE TRIGGER s02_assignment_revisions_no_replace BEFORE INSERT ON fm_v2_s02_assignment_revisions WHEN EXISTS (SELECT 1 FROM fm_v2_s02_assignment_revisions r WHERE r.owner_id=NEW.owner_id AND r.assignment_id=NEW.assignment_id AND r.revision=NEW.revision) BEGIN SELECT RAISE(ABORT,'S02 revisions are append-only'); END;
CREATE TRIGGER s02_definition_policy_authority BEFORE INSERT ON fm_v2_s02_definition_policy WHEN NOT EXISTS (SELECT 1 FROM fm_v2_knowledge_revisions r JOIN fm_v2_change_sets c ON c.owner_id=r.owner_id AND c.change_set_id=r.change_set_id WHERE r.owner_id=NEW.owner_id AND r.block_id=NEW.block_id AND r.revision=NEW.revision AND r.change_set_id=NEW.change_set_id AND c.actor_type='user' AND c.actor_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 requires owner revision authority'); END;
CREATE TRIGGER s02_definition_policy_immutable BEFORE UPDATE ON fm_v2_s02_definition_policy BEGIN SELECT RAISE(ABORT,'S02 revisions are append-only'); END;
CREATE TRIGGER s02_edge_revisions_immutable BEFORE UPDATE ON fm_v2_s02_edge_revisions BEGIN SELECT RAISE(ABORT,'S02 revisions are append-only'); END;
CREATE TRIGGER s02_assignment_revisions_immutable BEFORE UPDATE ON fm_v2_s02_assignment_revisions BEGIN SELECT RAISE(ABORT,'S02 revisions are append-only'); END;
CREATE TRIGGER s02_generation_no_replace BEFORE INSERT ON fm_v2_s02_generation WHEN EXISTS (SELECT 1 FROM fm_v2_s02_generation r WHERE r.owner_id=NEW.owner_id) BEGIN SELECT RAISE(ABORT,'S02 identity already exists'); END;
CREATE TRIGGER s02_edge_heads_no_replace BEFORE INSERT ON fm_v2_s02_edge_heads WHEN EXISTS (SELECT 1 FROM fm_v2_s02_edge_heads r WHERE r.owner_id=NEW.owner_id AND r.edge_id=NEW.edge_id) BEGIN SELECT RAISE(ABORT,'S02 identity already exists'); END;
CREATE TRIGGER s02_assignment_heads_no_replace BEFORE INSERT ON fm_v2_s02_assignment_heads WHEN EXISTS (SELECT 1 FROM fm_v2_s02_assignment_heads r WHERE r.owner_id=NEW.owner_id AND r.assignment_id=NEW.assignment_id) BEGIN SELECT RAISE(ABORT,'S02 identity already exists'); END;
CREATE TRIGGER s02_scope_guard BEFORE UPDATE OF workspace_key,project_key ON fm_v2_knowledge_blocks WHEN (NEW.workspace_key<>OLD.workspace_key OR NEW.project_key<>OLD.project_key) AND (EXISTS (SELECT 1 FROM fm_v2_s02_definition_policy p WHERE p.owner_id=OLD.owner_id AND p.block_id=OLD.block_id) OR EXISTS (SELECT 1 FROM fm_v2_s02_assignment_heads h JOIN fm_v2_s02_assignment_revisions r ON r.owner_id=h.owner_id AND r.assignment_id=h.assignment_id AND r.revision=h.current_revision WHERE h.owner_id=OLD.owner_id AND r.target_block_id=OLD.block_id AND r.active=1)) AND fm_s02_policy_authorized() IS NOT 1 BEGIN SELECT RAISE(ABORT,'S02 protected scope requires guarded writer'); END;

CREATE TRIGGER s02_definition_mirror_guard BEFORE INSERT ON fm_v2_outbox WHEN NEW.kind IN ('candidate_review','mirror_update') AND json_valid(NEW.payload_json) AND EXISTS (SELECT 1 FROM fm_v2_s02_definition_policy p WHERE p.owner_id=NEW.owner_id AND p.block_id=json_extract(NEW.payload_json,'$.block_id')) BEGIN SELECT RAISE(ABORT,'S02 definition cannot acquire mirror association'); END;
