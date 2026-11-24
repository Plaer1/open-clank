"""BQ-23: the user-facing undo of a cross-root managed move (facade method behind POST /operations/{id}/undo-move), with the simulated Native
used by the cross-root Host tests extended by `inverse_of` (the real Native undo is proven by the real-service case)."""
import hashlib
import json
from pathlib import Path

from src.openclank.files_facade import FilesFacade, FilesFacadeError
from src.openclank.files_host_provider import _origin
from src.openclank.files_service_client import FilesServiceError
from tests.test_files_host_cross_root_move import Native, World, directory, run, seed_workspace


class UndoNative(Native):
    """Remembers each forward move; an `inverse_of` request moves it back, refusing like the real Native."""
    def __init__(self, client):
        super().__init__(client); self.forward, self.undone, self.undo_calls = {}, {}, 0

    async def request(self, operation, path, payload=None, root_operation_id=None):
        if operation == "managed_file_move" and payload and "inverse_of" in payload:
            self.undo_calls += 1
            opid, target = payload["operation_id"], payload["inverse_of"]
            if opid in self.undone: return {"data": {"operation_id": opid, "status": "committed"}}          # replay of the same undo
            if target in self.undone or target not in self.forward and target in self.undone.values():
                raise FilesServiceError("managed_undo_of_undo_unsupported", code="unsupported")
            if target not in self.forward:
                raise FilesServiceError("managed_undo_journal_missing", code="unsupported")
            parts = self.forward[target]
            for part in parts:
                if part["destination"] is not None and hashlib.sha256(Path(part["destination"]).read_bytes()).hexdigest() != part["after"]:
                    raise FilesServiceError("filesystem operation failed", code="conflict")                  # drift since the move
            for part in parts:
                if part["destination"] is not None:
                    Path(part["source"]).parent.mkdir(parents=True, exist_ok=True)
                    Path(part["source"]).write_bytes(part["before"]); Path(part["destination"]).unlink()
            self.undone[opid] = target
            return {"data": {"operation_id": opid, "status": "committed"}}
        before = {p["source"]: Path(p["source"]).read_bytes() for p in (payload or {}).get("participants", []) if Path(p["source"]).exists()} if operation == "managed_file_move" else {}
        result = await super().request(operation, path, payload, root_operation_id=root_operation_id)
        if operation == "managed_file_move" and payload and payload["operation_id"] not in self.forward:
            self.forward[payload["operation_id"]] = [{"source": p["source"], "destination": p["destination"], "before": before.get(p["source"], b""),
                "after": hashlib.sha256(Path(p["destination"]).read_bytes()).hexdigest() if p["destination"] is not None and Path(p["destination"]).exists() else ""}
                for p in payload["participants"]]
        return result


async def forward_move(w, name="Note"):
    W, S = w.root("W"), w.root("S")
    w.workspace("W"); (Path(S) / "Sel").mkdir()
    w.native = UndoNative(w.client)
    doc = await seed_workspace(w, W, name)
    ctx = w.ctx(); facade = FilesFacade([w.provider], operation_store=w.repo)
    async def ref(path): return facade._resource(ctx, "host", await w.provider.stat(ctx, origin_id=_origin(path)))
    src, folder = await ref(doc), await ref(S + "/Sel")
    receipt = await facade.transfer_resources(ctx, operation_id="op-fwd", kind="move", sources=[{"item_id": "i1", "resource_ref": src["ref"], "expected_revision": src.get("revision")}],
        destination_ref=folder["ref"], destination_root={"resource_ref": folder["ref"], "expected_revision": folder.get("revision")})
    assert receipt["items"][0]["outcome"] == "committed", receipt
    return facade, ctx, W, S, doc


def test_receipt_offers_undo_for_a_completed_cross_root_file_move_and_the_undo_puts_everything_back():
    async def body(w):
        facade, ctx, W, S, doc = await forward_move(w)
        pre_text = "![i](.media/Note/img.png) and [[.media/Note/img.png]]"
        status = await facade.operation_receipt(ctx, operation_id="op-fwd")
        item = status["items"][0]
        assert item["outcome"] == "committed" and item["undo"] == "available"
        assert Path(S + "/Sel/Note.md").exists() and not Path(doc).exists()
        first = await facade.undo_managed_move(ctx, operation_id="op-fwd", item_id="i1")
        assert first == {"operation_id": "op-fwd", "item_id": "i1", "undo": "committed"}
        assert Path(doc).read_text() == pre_text and Path(W + "/.media/Note/img.png").read_bytes() == b"PNGDATA"
        assert not Path(S + "/Sel/Note.md").exists() and not Path(S + "/Sel/.media/Note/img.png").exists()
        again = await facade.undo_managed_move(ctx, operation_id="op-fwd", item_id="i1")          # a retry is the SAME undo (derived id), read back
        assert again["undo"] == "committed" and w.native.undo_calls == 2 and len(w.native.undone) == 1
    run(body)


def test_typed_refusals_arrive_in_plain_words_and_touch_nothing():
    async def body(w):
        facade, ctx, W, S, doc = await forward_move(w)
        moved = Path(S + "/Sel/Note.md"); moved.write_text("edited at the new place")           # drift since the move
        try: await facade.undo_managed_move(ctx, operation_id="op-fwd", item_id="i1")
        except FilesFacadeError as exc:
            assert exc.code == "resource_changed" and "nothing was touched" in str(exc)
        else: raise AssertionError("drift accepted")
        assert moved.read_text() == "edited at the new place" and not Path(doc).exists()
        w.native.forward.clear()                                                                  # the Native journal is gone
        try: await facade.undo_managed_move(ctx, operation_id="op-fwd", item_id="i1")
        except FilesFacadeError as exc:
            assert exc.code == "managed_undo_journal_missing" and "can no longer be undone" in str(exc)
        else: raise AssertionError("missing journal accepted")
        async def undo_of_undo(*a, **k): return {"outcome": "failed", "code": "managed_undo_of_undo_unsupported"}
        w.provider.undo_managed_cross_root_move = undo_of_undo
        try: await facade.undo_managed_move(ctx, operation_id="op-fwd", item_id="i1")
        except FilesFacadeError as exc:
            assert exc.code == "managed_undo_of_undo_unsupported" and "cannot be undone again" in str(exc)
        else: raise AssertionError("undo of undo accepted")
    run(body)


def test_same_root_and_unknown_items_are_not_undoable_and_other_owners_are_refused():
    async def body(w):
        W = w.root("W"); w.workspace("W")
        w.native = UndoNative(w.client)
        doc = await seed_workspace(w, W, "Same"); ctx = w.ctx()
        facade = FilesFacade([w.provider], operation_store=w.repo)
        src = facade._resource(ctx, "host", await w.provider.stat(ctx, origin_id=_origin(doc)))
        (Path(W) / "sub").mkdir()
        folder = facade._resource(ctx, "host", await w.provider.stat(ctx, origin_id=_origin(W + "/sub")))
        receipt = await facade.transfer_resources(ctx, operation_id="op-same", kind="move", sources=[{"item_id": "i1", "resource_ref": src["ref"], "expected_revision": src.get("revision")}], destination_ref=folder["ref"])
        status = await facade.operation_receipt(ctx, operation_id="op-same")
        assert all("undo" not in i for i in status["items"]), "a same-root move is not offered an undo"
        for op, item in (("op-same", "i1"), ("op-none", "i1"), ("op-same", "ghost")):
            try: await facade.undo_managed_move(ctx, operation_id=op, item_id=item)
            except FilesFacadeError as exc: assert exc.code == "resource_unavailable"
            else: raise AssertionError((op, item))
        assert w.native.undo_calls == 0
    run(body)
