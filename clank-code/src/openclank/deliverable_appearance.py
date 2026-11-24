"""Closed request-scoped deliverable appearance data, not a settings store."""
import json
import re

_COLOR = re.compile(r'^(?:#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})|rgba?\([\d.,%\s/+-]+\)|color\(srgb\s+[\d.\s/%+-]+\))$', re.I)
_TEXT = re.compile(r"^[\w .,'-]{1,100}$", re.ASCII)
_BASE = {'bg','fg','panel','border','red'}
_ADV = set('userBubbleBg aiBubbleBg bubbleBorder sidebarBg brandColor brandMixTo hamburgerColor inputBg inputBorder sendBtnBg sendBtnHover codeBg codeFg toggleActive accentPrimary accentHover accentFocus accentDisabled accentError successColor'.split())
_SEM = set('accent accent-primary accent-hover accent-focus accent-selection accent-disabled accent-error color-accent color-error color-error-light color-danger color-success color-warning color-muted color-muted-alt color-agent-active color-link-hover green warn'.split())
_MAT = set('clanker-field clanker-surface clanker-surface-2 clanker-well clanker-ink clanker-soft-ink clanker-rail clanker-control clanker-bevel clanker-outline clanker-raised clanker-cobalt clanker-sky clanker-lime clanker-gold clanker-coral clanker-pink clanker-lilac'.split())
_STANDARD = {'id':'open-clank.deliverable-design','version':'1.0.0','sha256':'21708f3285fd26c50100db6b4467e08b58461ca60ae835ac6f3ea006bdf36675'}

def validate_deliverable_appearance(value):
    if isinstance(value,str):
        if len(value.encode('utf-8')) > 16384: raise ValueError('appearance too large')
        value = json.loads(value)
    if type(value) is not dict or set(value) != {'standard','identity','colors','advanced','semantic','material','typography','layout','motion','provenance'}:
        raise ValueError('invalid appearance envelope')
    if value['standard'] != _STANDARD: raise ValueError('invalid standard')
    identity=value['identity']; typo=value['typography']; layout=value['layout']; motion=value['motion']; provenance=value['provenance']
    if (type(identity) is not dict or set(identity)!={'name','kind','overridePresent'} or not isinstance(identity['name'],str) or not _TEXT.fullmatch(identity['name']) or identity['kind'] not in ('builtin','custom') or type(identity['overridePresent']) is not bool): raise ValueError('invalid identity')
    for group, allowed, required in [('colors',_BASE,True),('advanced',_ADV,False),('semantic',_SEM,False),('material',_MAT,False)]:
        roles=value[group]
        if type(roles) is not dict or (set(roles) != allowed if required else bool(set(roles)-allowed)) or any(not isinstance(v,str) or not _COLOR.fullmatch(v) for v in roles.values()): raise ValueError('invalid color roles')
    if (type(typo) is not dict or set(typo)!={'font','family','density'} or any(not isinstance(typo[k],str) or not _TEXT.fullmatch(typo[k]) for k in typo)): raise ValueError('invalid typography')
    if type(layout) is not dict or set(layout)!={'edgeHardness'} or type(layout['edgeHardness']) not in (int,float) or not 0 <= layout['edgeHardness'] <= 100: raise ValueError('invalid layout')
    if type(motion) is not dict or set(motion)!={'reduced'} or type(motion['reduced']) is not bool: raise ValueError('invalid motion')
    if (type(provenance) is not dict or set(provenance)!={'overrideColors','source'} or provenance['source']!='current-applied-theme' or type(provenance['overrideColors']) is not list or len(provenance['overrideColors'])>5 or len(set(provenance['overrideColors'])) != len(provenance['overrideColors']) or any(k not in _BASE for k in provenance['overrideColors'])): raise ValueError('invalid provenance')
    encoded=json.dumps(value,ensure_ascii=True,separators=(',',':'),allow_nan=False)
    if len(encoded.encode()) > 16384: raise ValueError('appearance too large')
    return json.loads(encoded)
