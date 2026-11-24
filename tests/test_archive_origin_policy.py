from dataclasses import replace
import hashlib
import json
import sqlite3

import pytest

from src.openclank.conversation_archive import (
    ArchiveUnavailableError, ConversationArchive, SourcePart,
    TrustedOriginAdmission, TrustedOriginCompletion, TrustedOriginPart, convert_origin_schema_offline,
)


def test_disposable_origin_policy_exact_completion_is_atomic(tmp_path):
    plain = ConversationArchive(str(tmp_path / 'plain.db'))
    admission = TrustedOriginAdmission('admission', 'alice', 'chat', 'main', 'root', None, None, None,
                                       False, False, False, True, 'trusted_revision', 'policy-v1')
    with pytest.raises(ArchiveUnavailableError):
        plain.record_trusted_origin_admission(admission)
    archive = ConversationArchive(str(tmp_path / 'origin.db'), enable_origin_policy_for_disposable_test=True)
    archive.append_parts([SourcePart(owner='alice', chat_id='chat', actor_id='main', message_id='m', part_id='p', content='private body', event_workspace='workspace', event_project='project', time_created=123, time_updated=124)])
    assert archive.record_trusted_origin_admission(admission)['accepted']
    assert archive.record_trusted_origin_admission(admission)['replayed']
    with pytest.raises(ValueError):
        archive.record_trusted_origin_admission(replace(admission, no_memory=True))
    with pytest.raises(ValueError):
        archive.record_trusted_origin_admission(replace(admission, chat_id='different'))
    with pytest.raises(TypeError):
        archive.record_trusted_origin_admission(admission.__dict__)
    digest = hashlib.sha256(b'private body').hexdigest()
    part = TrustedOriginPart('m', 'p', 1, digest)
    wrong = TrustedOriginCompletion('admission', 'alice', 'chat', 'main', 'completed', (replace(part, content_hash='0'*64),))
    with pytest.raises(ValueError):
        archive.record_trusted_origin_completion(wrong)
    with sqlite3.connect(archive.db_path) as db:
        assert db.execute('SELECT count(*) FROM origin_completion').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM conversation_archive_outbox WHERE event_kind='origin_ready'").fetchone()[0] == 0
    completion = replace(wrong, parts=(part,))
    assert archive.record_trusted_origin_completion(completion)['ready']
    assert archive.record_trusted_origin_completion(completion)['replayed']
    assert archive.record_trusted_origin_completion(completion)['ready']
    with pytest.raises(ValueError):
        archive.record_trusted_origin_completion(replace(completion, outcome='cancelled'))
    with sqlite3.connect(archive.db_path) as db:
        payload = db.execute("SELECT payload_json FROM conversation_archive_outbox WHERE event_kind='origin_ready'").fetchone()[0]
        assert 'private body' not in payload
        event = json.loads(payload)
        assert (event['event_workspace'], event['event_project'], event['time_created'], event['time_updated']) == ('workspace', 'project', 123, 124)
        assert db.execute('SELECT count(*) FROM origin_part_link').fetchone()[0] == 1
    unknown = replace(admission, admission_id='unknown', policy_source=None, policy_value=None)
    archive.record_trusted_origin_admission(unknown)
    archive.append_parts([SourcePart(owner='alice', chat_id='chat', actor_id='main', message_id='m2', part_id='p', content='other')])
    ref = TrustedOriginPart('m2', 'p', 1, hashlib.sha256(b'other').hexdigest())
    result = archive.record_trusted_origin_completion(TrustedOriginCompletion('unknown', 'alice', 'chat', 'main', 'completed', (ref,)))
    assert result['ready'] is False
    with sqlite3.connect(archive.db_path) as db:
        assert db.execute("SELECT count(*) FROM conversation_archive_outbox WHERE event_kind='origin_ready'").fetchone()[0] == 1
    with pytest.raises(ArchiveUnavailableError):
        ConversationArchive(str(tmp_path / 'plain.db'), enable_origin_policy_for_disposable_test=True)

    with pytest.raises(ValueError):
        archive.hydrate_origin_part(owner='alice', chat_id='chat', actor_id='main', part=part, authorize=lambda *_: True)
    assert archive.hydrate_origin_part(owner='alice', chat_id='chat', actor_id='main', part=part, authorize=lambda *_: True, verify_host_source=lambda *_: True).content == 'private body'
    with pytest.raises(ValueError):
        archive.hydrate_origin_part(owner='alice', chat_id='chat', actor_id='main', part=part, authorize=lambda *_: False, verify_host_source=lambda *_: True)
    convert_origin_schema_offline(plain.db_path)
    converted = ConversationArchive(plain.db_path, enable_origin_policy=True)
    assert converted.record_trusted_origin_admission(admission)['accepted']
    assert converted.chat_inventory(owner='alice', chat_id='chat')['tables']['origin_admission'] == 1
    assert converted.erase_chat(owner='alice', chat_id='chat')['residual'] == 0
    with pytest.raises(ArchiveUnavailableError):
        convert_origin_schema_offline(plain.db_path)
