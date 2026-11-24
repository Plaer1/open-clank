"""Slide text profile over the existing shared operation authority."""
from src.openclank.collaboration_operations import public_slides_selector_digest
from src.openclank.collaboration_slides import validate_slides
from src.openclank.shared_text_service import SharedTextService


class SharedSlidesService(SharedTextService):
    def _validate_profile(self, text):
        validate_slides(text)

    def _selector(self, publication_id, operation_id, base_sha256, objects):
        return public_slides_selector_digest(publication_id=publication_id, operation_id=operation_id,
                                              base_sha256=base_sha256, objects=objects)

    def _intent_edit(self, objects):
        return {'kind':'collaboration.slides.v1', 'objects':objects}

    async def _commit_edit(self, target, reservation, base, base_sha256, objects, **kwargs):
        return await target.provider.collaboration_slides_commit(target, reservation, base=base,
                                                                  base_sha256=base_sha256, objects=objects, **kwargs)

    def _status_options(self):
        return {'kind':'collaboration.slides.v1'}

    async def commit(self, *, recipient_account, publication_id, operation_id, base_text, base_sha256,
                     objects, client_digest=None, initiator=None):
        return await super().commit(recipient_account=recipient_account, publication_id=publication_id,
            operation_id=operation_id, base_text=base_text, base_sha256=base_sha256, edits=objects,
            client_digest=client_digest, initiator=initiator)
