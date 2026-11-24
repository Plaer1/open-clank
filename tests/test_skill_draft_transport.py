"""Scoped skill-draft transport identity; no Native work is issued here."""
import pytest
from src.memery_provider import _skill_draft_envelope
from src.openclank.fm_owned_stdio import OwnedStdio


def test_skill_draft_envelope_checks_nested_original_and_full_size():
    original={'owner':'owner','workspace_id':'workspace'}
    reserve={'original':original}
    attempt={'reservation_original':reserve}
    for phase,request in (('skill_draft_prepare',original),('skill_draft_reserve',reserve),
                          ('skill_draft_dispatch',attempt),('skill_draft_terminal',{'attempt':attempt}),
                          ('skill_draft_original',attempt)):
        payload={'phase':phase,'request':request}
        envelope=_skill_draft_envelope(payload,'owner','workspace')
        assert OwnedStdio._argument_digest(envelope)
        with pytest.raises(ValueError): _skill_draft_envelope(payload,'different','workspace')
    with pytest.raises(ValueError):
        _skill_draft_envelope({'phase':'skill_draft_prepare','request':{**original,'padding':'x'*65536}},'owner','workspace')
    with pytest.raises(ValueError):
        _skill_draft_envelope({'phase':'skill_draft_unknown','request':original},'owner','workspace')


def test_retirement_identity_reads_only_exact_part_metadata(tmp_path):
    from src.openclank.conversation_archive import ConversationArchive, SourcePart
    archive=ConversationArchive(str(tmp_path/'archive.db'))
    archive.append_parts([SourcePart(owner='account',chat_id='chat',actor_id='main',message_id='message',part_id='part',
        revision=1,role='assistant',part_type='tool',content='private output')])
    identity=archive.private_part_retirement_identity(owner='account',chat_id='chat',actor_id='main',
        message_id='message',part_id='part',revision=1)
    assert identity['revision']==1 and identity['role']=='assistant' and identity['part_type']=='tool'
    assert 'content' not in identity and 'text' not in identity
    assert archive.private_part_retirement_identity(owner='other',chat_id='chat',actor_id='main',
        message_id='message',part_id='part',revision=1) is None
