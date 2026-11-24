"""Workbook profile over the existing shared operation authority."""
from src.openclank.collaboration_operations import public_workbook_selector_digest
from src.openclank.collaboration_workbook import validate_workbook
from src.openclank.shared_text_service import SharedTextService


class SharedWorkbookService(SharedTextService):
    def _validate_profile(self, text):
        validate_workbook(text)

    def _selector(self, publication_id, operation_id, base_sha256, cells):
        return public_workbook_selector_digest(publication_id=publication_id, operation_id=operation_id,
                                                base_sha256=base_sha256, cells=cells)

    def _intent_edit(self, cells):
        return {'kind':'collaboration.workbook.v1', 'cells':cells}

    async def _commit_edit(self, target, reservation, base, base_sha256, cells, **kwargs):
        return await target.provider.collaboration_workbook_commit(target, reservation, base=base,
                                                                    base_sha256=base_sha256, cells=cells, **kwargs)

    def _status_options(self):
        return {'kind':'collaboration.workbook.v1'}

    async def commit(self, *, recipient_account, publication_id, operation_id, base_text, base_sha256,
                     cells, client_digest=None, initiator=None):
        return await super().commit(recipient_account=recipient_account, publication_id=publication_id,
            operation_id=operation_id, base_text=base_text, base_sha256=base_sha256, edits=cells,
            client_digest=client_digest, initiator=initiator)
