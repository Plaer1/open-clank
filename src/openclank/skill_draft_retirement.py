"""Private held-source boundary for a future qualified skill-draft retirement."""
import asyncio


async def retire_then_tombstone(*, capability, archive, owner, chat_id, actor_id,
                                message_id, part_id, revision, reason, current):
    """Retire exact derived originals, then tombstone the unchanged archive part.

    The caller holds the existing chat admission guard until this coroutine returns.
    Unknown Native outcomes fail closed; this helper never admits a session or
    enables the retirement capability itself.
    """
    from src.openclank.chat_source_bridge import (
        _HostChatFenceCapability, HostSourceFenceUnavailable, retire_skill_draft_manifests,
    )
    from src.openclank.memory_job_admission import _completed_turn_thread
    if (type(capability) is not _HostChatFenceCapability or not callable(current)
            or any(type(value) is not str or not value for value in (owner, chat_id, actor_id, message_id, part_id))
            or (revision is not None and (type(revision) is not int or revision < 1))):
        raise HostSourceFenceUnavailable('skill draft retirement unavailable')
    def check():
        if current() is not True or archive.resolve_owner(owner) != capability.account:
            raise HostSourceFenceUnavailable('managed history binding changed')
        capability.validate(provider=capability.provider, owner=owner, chat_id=chat_id, whole_chat=False)
        if not capability.whole_chat and message_id not in capability.fenced_ids:
            raise HostSourceFenceUnavailable('managed history source is not fenced')
    check()
    identity = await retire_skill_draft_manifests(capability=capability, archive=archive,
        part={'actor_id': actor_id, 'message_id': message_id, 'part_id': part_id, 'revision': revision})
    if (type(identity) is not dict or identity.get('actor_id') != actor_id
            or identity.get('message_id') != message_id or identity.get('part_id') != part_id
            or type(identity.get('revision')) is not int or type(identity.get('content_hash')) is not str):
        raise HostSourceFenceUnavailable('skill draft part preimage unavailable')
    check()
    def commit():
        check()
        return archive.tombstone_part(owner=owner, chat_id=chat_id, actor_id=actor_id,
            message_id=message_id, part_id=part_id, revision=identity['revision'], reason=reason,
            _expected_content_hash=identity['content_hash'], _expected_latest=revision is None,
            _expected_account=capability.account)
    result, cancelled = await _completed_turn_thread(commit)
    if cancelled:
        raise asyncio.CancelledError
    check()
    return result
