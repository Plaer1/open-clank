from src.general_hex_composition import compose_general_hexes, defaults_instruction
from src.openclank.planning_defaults import PLANNING_DEFAULT_ID, PLANNING_DEFAULT_BODY


def test_planning_default_is_versioned_budgeted_and_subordinate():
    saved = {'owner_id':'alice', 'hex_id':'saved', 'revision':1, 'hash':'h', 'title':'Local preference', 'tags':[], 'provenance':{}, 'body':'Use my layout.', 'enabled':True, 'accepted':True}
    inputs = {'entries':[saved], 'workspace_tags':None, 'task_tags':None}
    result = compose_general_hexes(inputs, owner_id='alice')
    assert result['product_defaults'][0]['id'] == PLANNING_DEFAULT_ID
    assert result['product_defaults'][0]['body'] == PLANNING_DEFAULT_BODY
    assert result['defaults'].index('Use my layout.') < result['defaults'].index('Product default: planning')
    assert 'no-plan' in defaults_instruction(result) and 'Small tasks' in result['defaults']
    assert '.clanker/futures/' in result['defaults'] and '.clanker/robonotes/' in result['defaults']
    short = compose_general_hexes(inputs, owner_id='alice', budget_chars=100)
    assert short['product_defaults'][0]['state'] == 'omitted-for-budget'
    assert short['budget']['omitted_count'] == 2
    guidance = result['product_defaults'][1]
    assert guidance['state'] == 'included' and guidance['id'] == 'openclank.product.guidance-review'
    assert guidance['body'] in defaults_instruction(result)
    assert 'owner approves' in guidance['body']
    assert short['budget']['used_chars'] <= 100 and short['snapshot_hash'] != result['snapshot_hash']
    duplicate = compose_general_hexes({'entries':[dict(saved, hex_id=PLANNING_DEFAULT_ID)], 'workspace_tags':None, 'task_tags':None}, owner_id='alice')
    assert duplicate['product_defaults'][0]['state'] == 'deduplicated'
    assert 'Product default: planning' not in duplicate['defaults']
