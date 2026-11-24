"""Versioned product planning guidance, subordinate to saved and local rules."""
from src.clanker_paths import CLANKER_FUTURES_DIR, CLANKER_ROBONOTES_DIR

PLANNING_DEFAULT_ID = 'openclank.product.indexed-planning'
PLANNING_DEFAULT_VERSION = 1
PLANNING_DEFAULT_BODY = (
    'For substantive multi-step work, use judgment to keep a short linked metaplan and coherent slices. '
    f'Unless the user or workspace specifies another layout, put the metaplan at {CLANKER_FUTURES_DIR}/<topic>.md, '
    f'slices under {CLANKER_FUTURES_DIR}/<topic>/, and focused notes under {CLANKER_ROBONOTES_DIR}/. '
    'Record outcome, scope/files, dependencies, owner/status, meaningful acceptance and recovery; '
    'keep the index current and handoffs linked to evidence. Small tasks can remain lightweight. '
    'Honor explicit no-plan requests and user, workspace and saved General instructions. '
    'A plan does not itself authorize implementation.'
)
