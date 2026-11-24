"""The native-memory-ops capability list: Rust constant == Python constant (order included), and the two live
operations still negotiate over the real transport on a fresh guarded-v18 fixture (first checked-in guarded-18 test)."""
import re

from tests import _native_ops_fixture as F

MAIN = F.CRATES / "memery-worker/src/main.rs"


def rust_constant():
    m = re.search(r"^const NATIVE_MEMORY_OPS:\[&str;(\d+)\]=\[([^\]]*)\];", MAIN.read_text(), re.M)
    assert m, "NATIVE_MEMORY_OPS not found in memery-worker main.rs"
    ops = re.findall(r'"([a-z0-9_]+)"', m.group(2))
    assert int(m.group(1)) == len(ops)
    return ops


def test_rust_and_python_capability_lists_are_identical_in_order():
    from src.memery_provider import NATIVE_MEMORY_OPS
    assert list(NATIVE_MEMORY_OPS) == rust_constant() == ["explicit_add_review_batch_v1", "curated_knowledge_snapshot_review_v1"]


def test_existing_ops_negotiate_on_fresh_guarded_18_store():
    fx = F.build_fixture()
    preparation, provider, coordinator = fx
    assert preparation.schema_version == 18
    async def go():
        try:
            await provider.initialize()
            assert await provider._host_negotiate_native_memory_ops(owner="alice", workspace_id="ws-a") is True
            assert await provider._host_negotiate_explicit_add(owner="alice", workspace_id="ws-a") is True
            # the capability reply is exactly the advertised constant (the negotiation compares it for equality)
            result = await provider._s07_owner_rpc("alice", "ws-a", {"phase": "native_memory_ops_capability"},
                                                   __import__("asyncio").get_running_loop().time() + 2.0, read_ticket={})
            assert result["operations"] == rust_constant() and result["feature"] == "native_memory_ops_v1"
        finally:
            await F.shutdown(fx)
    F.run(go())
    # the real batch path works end to end (preview + apply) and left genuine rows
    async def seed():
        try:
            return await F.seed_records(fx, "alice", ["parity one", "parity two"])
        finally:
            await F.shutdown(fx)
    seeded = F.run(seed())
    assert len(seeded) == 2
    assert F.rows(fx.db, "SELECT COUNT(*) FROM fm_v2_knowledge_blocks WHERE owner_id='alice'") == [(2,)]
