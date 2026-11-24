"""Process one signed tool event through the host's disposable history archive."""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


def main():
    request = json.load(sys.stdin)
    with tempfile.TemporaryDirectory(prefix="queued-history-") as directory:
        os.environ.update(OPEN_CLANK_DATA_DIR=directory, DATABASE_URL="sqlite:///:memory:")
        from src.openclank import conversation_archive
        from src.openclank.acp_bridge import ACPBridge
        from src.openclank.managed_history import MUTATE, ManagedHistoryCallbacks

        archive = conversation_archive.ConversationArchive(
            str(Path(directory) / "archive.sqlite"),
            owner_resolver=lambda owner: "account" if owner == "alice" else owner,
        )
        conversation_archive.get_conversation_archive = lambda: archive
        verifier = {key: request[key] for key in ("publicKey", "clockID")}
        client = SimpleNamespace(tool_timing_verifier=lambda: {
            **verifier, "version": 1, "algorithm": "ed25519",
        })

        class Worker:
            def is_alive(self):
                return True

            def read(self):
                raise AssertionError("the history callback should not request actor state")

        worker = Worker()
        bridge = ACPBridge.__new__(ACPBridge)
        worker._bridge = bridge
        worker._proc = object()
        worker._client = bridge._client = client
        worker._managed_callbacks = SimpleNamespace(holder_id="holder")
        bridge._managed_actor_execution_reader = worker.read
        bridge._managed_provider_generation = "holder"
        bridge._managed_history_binding = lambda session: ("alice", "chat-t")
        callbacks = ManagedHistoryCallbacks(
            archive, bridge._managed_history_binding, bridge._managed_history_tool_timing,
        )
        print(json.dumps(asyncio.run(callbacks.dispatch(MUTATE, request["params"]))))


if __name__ == "__main__":
    main()
