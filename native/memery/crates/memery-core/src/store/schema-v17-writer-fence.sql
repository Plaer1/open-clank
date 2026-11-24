-- Inactive compatibility candidate. No user_version change or runtime activation.
-- The v2 function is a compatibility marker, not authentication.
-- Requires S01 and S03 v17 schema additions before installation.
-- Protected tables: curated, raw, facts, graph_nodes, graph_edges, graph_cues, candidates, memory_quarantine, memory_tombstones, memory_retention_policy, memory_retention_operations, fm_v2_predicates, fm_v2_change_sets, fm_v2_entities, fm_v2_entity_revisions, fm_v2_knowledge_blocks, fm_v2_knowledge_revisions, fm_v2_conflicts, fm_v2_sources, fm_v2_evidence, fm_v2_documents, fm_v2_chunks, fm_v2_projects, fm_v2_project_locators, fm_v2_policy_projections, fm_v2_spells, fm_v2_policy_transitions, fm_v2_outbox, fm_v2_migration_runs, fm_v2_migration_plan, fm_v2_migration_events, fm_v2_trust_assignments, fm_v2_entity_aliases, fm_v2_entity_roles, fm_v2_entity_tags, fm_v2_revision_evidence, fm_v2_candidates, fm_v2_candidate_revisions, fm_v2_candidate_evidence, fm_v2_conflict_events, fm_v2_privacy_erasure_context, fm_v2_privacy_erasure_tombstones, fm_v2_legacy_outbox, fm_v2_migration_outbox_events, fm_v2_migration_review, fm_v2_policy_trust, fm_v2_policy_profiles, fm_v2_revision_metadata, fm_v2_revision_field_authority, fm_v2_episodes, fm_v2_derived_generations, fm_v2_index_pointers, fm_v2_index_publications
-- Optional principal binding table is covered by schema-v17-principal-writer-fence.sql;
-- offline cutover must install that companion only when the optional table exists.
-- Optional media tables are covered by schema-v17-media-writer-fence.sql;
-- offline cutover installs that companion only when both optional tables exist.
-- Observational exclusions: embeddings, curated last_accessed_at and metadata uses,
-- graph last_seen and traversal_count. FTS virtual tables cannot carry triggers.

CREATE TRIGGER s03_writer_curated_insert BEFORE INSERT ON curated
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_curated_update BEFORE UPDATE OF id,content,kind,priority,trust_score,confidence_score,importance_score,scene_name,source,source_type,owner,workspace_id,session_key,session_id,tags,source_message_ids,timestamps,created_at,updated_at,archived,exempt_from_decay,exempt_from_dedup,workspace_path ON curated
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_curated_delete BEFORE DELETE ON curated
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_raw_insert BEFORE INSERT ON raw
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_raw_update BEFORE UPDATE OF id,role,content,session_key,session_id,workspace_id,owner,recorded_at,metadata,workspace_path ON raw
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_raw_delete BEFORE DELETE ON raw
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_facts_insert BEFORE INSERT ON facts
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_facts_update BEFORE UPDATE OF id,content,entities,trust_score,created_at,updated_at,owner,workspace_id,candidate_id,status ON facts
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_facts_delete BEFORE DELETE ON facts
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_graph_nodes_insert BEFORE INSERT ON graph_nodes
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_graph_nodes_update BEFORE UPDATE OF id,kind,label,name,norm_name,layer,ref_table,ref_id,trust,created_at,owner,workspace_id,candidate_id,status ON graph_nodes
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_graph_nodes_delete BEFORE DELETE ON graph_nodes
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_graph_edges_insert BEFORE INSERT ON graph_edges
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_graph_edges_update BEFORE UPDATE OF id,src_id,tag,dst_id,fact_id,weight,trust,created_at,owner,workspace_id,candidate_id,status ON graph_edges
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_graph_edges_delete BEFORE DELETE ON graph_edges
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_graph_cues_insert BEFORE INSERT ON graph_cues
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_graph_cues_update BEFORE UPDATE OF cue,node_id,source,created_at,owner,workspace_id,candidate_id,status ON graph_cues
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_graph_cues_delete BEFORE DELETE ON graph_cues
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_candidates_insert BEFORE INSERT ON candidates
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_candidates_update BEFORE UPDATE ON candidates
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_candidates_delete BEFORE DELETE ON candidates
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_quarantine_insert BEFORE INSERT ON memory_quarantine
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_quarantine_update BEFORE UPDATE ON memory_quarantine
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_quarantine_delete BEFORE DELETE ON memory_quarantine
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_tombstones_insert BEFORE INSERT ON memory_tombstones
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_tombstones_update BEFORE UPDATE ON memory_tombstones
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_tombstones_delete BEFORE DELETE ON memory_tombstones
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_retention_policy_insert BEFORE INSERT ON memory_retention_policy
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_retention_policy_update BEFORE UPDATE ON memory_retention_policy
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_retention_policy_delete BEFORE DELETE ON memory_retention_policy
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_retention_operations_insert BEFORE INSERT ON memory_retention_operations
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_retention_operations_update BEFORE UPDATE ON memory_retention_operations
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_memory_retention_operations_delete BEFORE DELETE ON memory_retention_operations
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_predicates_insert BEFORE INSERT ON fm_v2_predicates
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_predicates_update BEFORE UPDATE ON fm_v2_predicates
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_predicates_delete BEFORE DELETE ON fm_v2_predicates
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_change_sets_insert BEFORE INSERT ON fm_v2_change_sets
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_change_sets_update BEFORE UPDATE ON fm_v2_change_sets
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_change_sets_delete BEFORE DELETE ON fm_v2_change_sets
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entities_insert BEFORE INSERT ON fm_v2_entities
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entities_update BEFORE UPDATE ON fm_v2_entities
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entities_delete BEFORE DELETE ON fm_v2_entities
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_revisions_insert BEFORE INSERT ON fm_v2_entity_revisions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_revisions_update BEFORE UPDATE ON fm_v2_entity_revisions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_revisions_delete BEFORE DELETE ON fm_v2_entity_revisions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_knowledge_blocks_insert BEFORE INSERT ON fm_v2_knowledge_blocks
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_knowledge_blocks_update BEFORE UPDATE ON fm_v2_knowledge_blocks
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_knowledge_blocks_delete BEFORE DELETE ON fm_v2_knowledge_blocks
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_knowledge_revisions_insert BEFORE INSERT ON fm_v2_knowledge_revisions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_knowledge_revisions_update BEFORE UPDATE ON fm_v2_knowledge_revisions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_knowledge_revisions_delete BEFORE DELETE ON fm_v2_knowledge_revisions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_conflicts_insert BEFORE INSERT ON fm_v2_conflicts
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_conflicts_update BEFORE UPDATE ON fm_v2_conflicts
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_conflicts_delete BEFORE DELETE ON fm_v2_conflicts
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_sources_insert BEFORE INSERT ON fm_v2_sources
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_sources_update BEFORE UPDATE ON fm_v2_sources
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_sources_delete BEFORE DELETE ON fm_v2_sources
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_evidence_insert BEFORE INSERT ON fm_v2_evidence
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_evidence_update BEFORE UPDATE ON fm_v2_evidence
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_evidence_delete BEFORE DELETE ON fm_v2_evidence
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_documents_insert BEFORE INSERT ON fm_v2_documents
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_documents_update BEFORE UPDATE ON fm_v2_documents
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_documents_delete BEFORE DELETE ON fm_v2_documents
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_chunks_insert BEFORE INSERT ON fm_v2_chunks
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_chunks_update BEFORE UPDATE ON fm_v2_chunks
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_chunks_delete BEFORE DELETE ON fm_v2_chunks
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_projects_insert BEFORE INSERT ON fm_v2_projects
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_projects_update BEFORE UPDATE ON fm_v2_projects
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_projects_delete BEFORE DELETE ON fm_v2_projects
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_project_locators_insert BEFORE INSERT ON fm_v2_project_locators
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_project_locators_update BEFORE UPDATE ON fm_v2_project_locators
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_project_locators_delete BEFORE DELETE ON fm_v2_project_locators
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_projections_insert BEFORE INSERT ON fm_v2_policy_projections
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_projections_update BEFORE UPDATE ON fm_v2_policy_projections
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_projections_delete BEFORE DELETE ON fm_v2_policy_projections
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_spells_insert BEFORE INSERT ON fm_v2_spells
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_spells_update BEFORE UPDATE ON fm_v2_spells
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_spells_delete BEFORE DELETE ON fm_v2_spells
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_transitions_insert BEFORE INSERT ON fm_v2_policy_transitions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_transitions_update BEFORE UPDATE ON fm_v2_policy_transitions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_transitions_delete BEFORE DELETE ON fm_v2_policy_transitions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_outbox_insert BEFORE INSERT ON fm_v2_outbox
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_outbox_update BEFORE UPDATE ON fm_v2_outbox
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_outbox_delete BEFORE DELETE ON fm_v2_outbox
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_runs_insert BEFORE INSERT ON fm_v2_migration_runs
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_runs_update BEFORE UPDATE ON fm_v2_migration_runs
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_runs_delete BEFORE DELETE ON fm_v2_migration_runs
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_plan_insert BEFORE INSERT ON fm_v2_migration_plan
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_plan_update BEFORE UPDATE ON fm_v2_migration_plan
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_plan_delete BEFORE DELETE ON fm_v2_migration_plan
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_events_insert BEFORE INSERT ON fm_v2_migration_events
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_events_update BEFORE UPDATE ON fm_v2_migration_events
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_events_delete BEFORE DELETE ON fm_v2_migration_events
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_trust_assignments_insert BEFORE INSERT ON fm_v2_trust_assignments
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_trust_assignments_update BEFORE UPDATE ON fm_v2_trust_assignments
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_trust_assignments_delete BEFORE DELETE ON fm_v2_trust_assignments
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_aliases_insert BEFORE INSERT ON fm_v2_entity_aliases
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_aliases_update BEFORE UPDATE ON fm_v2_entity_aliases
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_aliases_delete BEFORE DELETE ON fm_v2_entity_aliases
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_roles_insert BEFORE INSERT ON fm_v2_entity_roles
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_roles_update BEFORE UPDATE ON fm_v2_entity_roles
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_roles_delete BEFORE DELETE ON fm_v2_entity_roles
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_tags_insert BEFORE INSERT ON fm_v2_entity_tags
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_tags_update BEFORE UPDATE ON fm_v2_entity_tags
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_entity_tags_delete BEFORE DELETE ON fm_v2_entity_tags
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_revision_evidence_insert BEFORE INSERT ON fm_v2_revision_evidence
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_revision_evidence_update BEFORE UPDATE ON fm_v2_revision_evidence
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_revision_evidence_delete BEFORE DELETE ON fm_v2_revision_evidence
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_candidates_insert BEFORE INSERT ON fm_v2_candidates
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_candidates_update BEFORE UPDATE ON fm_v2_candidates
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_candidates_delete BEFORE DELETE ON fm_v2_candidates
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_candidate_revisions_insert BEFORE INSERT ON fm_v2_candidate_revisions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_candidate_revisions_update BEFORE UPDATE ON fm_v2_candidate_revisions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_candidate_revisions_delete BEFORE DELETE ON fm_v2_candidate_revisions
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_candidate_evidence_insert BEFORE INSERT ON fm_v2_candidate_evidence
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_candidate_evidence_update BEFORE UPDATE ON fm_v2_candidate_evidence
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_candidate_evidence_delete BEFORE DELETE ON fm_v2_candidate_evidence
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_conflict_events_insert BEFORE INSERT ON fm_v2_conflict_events
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_conflict_events_update BEFORE UPDATE ON fm_v2_conflict_events
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_conflict_events_delete BEFORE DELETE ON fm_v2_conflict_events
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_privacy_erasure_context_insert BEFORE INSERT ON fm_v2_privacy_erasure_context
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_privacy_erasure_context_update BEFORE UPDATE ON fm_v2_privacy_erasure_context
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_privacy_erasure_context_delete BEFORE DELETE ON fm_v2_privacy_erasure_context
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_privacy_erasure_tombstones_insert BEFORE INSERT ON fm_v2_privacy_erasure_tombstones
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_privacy_erasure_tombstones_update BEFORE UPDATE ON fm_v2_privacy_erasure_tombstones
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_privacy_erasure_tombstones_delete BEFORE DELETE ON fm_v2_privacy_erasure_tombstones
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_legacy_outbox_insert BEFORE INSERT ON fm_v2_legacy_outbox
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_legacy_outbox_update BEFORE UPDATE ON fm_v2_legacy_outbox
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_legacy_outbox_delete BEFORE DELETE ON fm_v2_legacy_outbox
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_outbox_events_insert BEFORE INSERT ON fm_v2_migration_outbox_events
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_outbox_events_update BEFORE UPDATE ON fm_v2_migration_outbox_events
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_outbox_events_delete BEFORE DELETE ON fm_v2_migration_outbox_events
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_review_insert BEFORE INSERT ON fm_v2_migration_review
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_review_update BEFORE UPDATE ON fm_v2_migration_review
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_migration_review_delete BEFORE DELETE ON fm_v2_migration_review
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_trust_insert BEFORE INSERT ON fm_v2_policy_trust
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_trust_update BEFORE UPDATE ON fm_v2_policy_trust
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_trust_delete BEFORE DELETE ON fm_v2_policy_trust
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_profiles_insert BEFORE INSERT ON fm_v2_policy_profiles
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_profiles_update BEFORE UPDATE ON fm_v2_policy_profiles
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_policy_profiles_delete BEFORE DELETE ON fm_v2_policy_profiles
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_revision_metadata_insert BEFORE INSERT ON fm_v2_revision_metadata
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_revision_metadata_update BEFORE UPDATE ON fm_v2_revision_metadata
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_revision_metadata_delete BEFORE DELETE ON fm_v2_revision_metadata
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_revision_field_authority_insert BEFORE INSERT ON fm_v2_revision_field_authority
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_revision_field_authority_update BEFORE UPDATE ON fm_v2_revision_field_authority
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_revision_field_authority_delete BEFORE DELETE ON fm_v2_revision_field_authority
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

-- A uses-only metadata update remains observational; malformed JSON is semantic.
CREATE TRIGGER s03_writer_curated_metadata BEFORE UPDATE OF metadata ON curated
WHEN (NOT json_valid(OLD.metadata) OR NOT json_valid(NEW.metadata) OR json_remove(CASE WHEN json_valid(OLD.metadata) THEN OLD.metadata ELSE '{}' END,'$.uses') IS NOT json_remove(CASE WHEN json_valid(NEW.metadata) THEN NEW.metadata ELSE '{}' END,'$.uses'))
 AND fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

-- Derived source and publication lineage (embeddings remain a cache).
CREATE TRIGGER s03_writer_fm_v2_episodes_insert BEFORE INSERT ON fm_v2_episodes
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_episodes_update BEFORE UPDATE ON fm_v2_episodes
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_episodes_delete BEFORE DELETE ON fm_v2_episodes
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_derived_generations_insert BEFORE INSERT ON fm_v2_derived_generations
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_derived_generations_update BEFORE UPDATE ON fm_v2_derived_generations
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_derived_generations_delete BEFORE DELETE ON fm_v2_derived_generations
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_index_pointers_insert BEFORE INSERT ON fm_v2_index_pointers
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_index_pointers_update BEFORE UPDATE ON fm_v2_index_pointers
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_index_pointers_delete BEFORE DELETE ON fm_v2_index_pointers
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_index_publications_insert BEFORE INSERT ON fm_v2_index_publications
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_index_publications_update BEFORE UPDATE ON fm_v2_index_publications
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

CREATE TRIGGER s03_writer_fm_v2_index_publications_delete BEFORE DELETE ON fm_v2_index_publications
WHEN fm_writer_capability() IS NOT 'openclank.frankenmemory.writer.v2'
BEGIN SELECT RAISE(ABORT, 'incompatible memory writer'); END;

