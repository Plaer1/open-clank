-- Explicit offline capability. Older connections do not implement this
-- function and therefore cannot publish these managed results.
CREATE TRIGGER policy_projection_s08_commit BEFORE UPDATE OF state ON fm_v2_jobs
WHEN NEW.kind='access_consolidation' AND NEW.state='succeeded'
 AND fm_policy_projection_capability() IS NOT 'openclank.policy_projection.writer.v1'
BEGIN SELECT RAISE(ABORT,'policy projection writer required'); END;

CREATE TRIGGER policy_projection_maintenance_insert BEFORE INSERT ON fm_v2_evidence
WHEN NEW.locator_type='derived_compaction_summary'
 AND fm_policy_projection_capability() IS NOT 'openclank.policy_projection.writer.v1'
BEGIN SELECT RAISE(ABORT,'policy projection writer required'); END;

-- Human review has a distinct authority contract and remains unavailable
-- until its own commit-time policy proof is joined.
CREATE TRIGGER policy_projection_s08_review BEFORE INSERT ON fm_v2_candidate_revisions
WHEN substr(NEW.candidate_id,1,13)='s08candidate_' AND NEW.state='activated'
 AND fm_policy_s08_review_capability() IS NOT 'openclank.policy_projection.review.v1'
BEGIN SELECT RAISE(ABORT,'policy projection review required'); END;
