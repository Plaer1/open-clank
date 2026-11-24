"""One producer-to-host discovery contract regression."""

from __future__ import annotations

import json
import os
from datetime import datetime
import subprocess
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.provider_models import ProviderBase
from routes.provider_v1_routes import _engine_model_routes
from src.openclank.managed_protocol import validate_engine_method_result
from src.openclank.provider_store import ProviderStore


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "packages/mimo-code/packages/cli/test/acp/discovery-bridge.fixture.ts"


def test_real_engine_discovery_crosses_python_validation_and_host_persistence():
    completed = subprocess.run(
        ["bun", "run", str(FIXTURE)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    engine_result = json.loads(completed.stdout)
    validate_engine_method_result(
        "_openclank/provider-control/v1/account/validate", engine_result
    )

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    ProviderBase.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    store = ProviderStore(factory, clock=lambda: datetime(2026, 10, 27, 1, 2, 3))
    connection = store.create_connection(
        owner="bridge-owner",
        connection_id="pcn_bridge",
        family_id="anthropic",
        adapter_id="anthropic-messages",
        kind="subscription",
        billing_lane="subscription",
        label="Bridge",
    )
    engine_routes = _engine_model_routes(
        store=store,
        owner="bridge-owner",
        operation="oauth.complete:add:pcn_bridge",
        idempotency_key="bridge-flow",
        raw_routes=engine_result["modelRoutes"],
    )

    account = store.persist_account_discovery(
        owner="bridge-owner",
        account_id="host-account-bridge",
        connection_id=connection.id,
        label="Bridge account",
        auth_method=engine_result["authMethod"],
        auth_class=engine_result["authClass"],
        credentials=engine_result["credential"],
        safe_identity=engine_result["safeIdentity"],
        model_routes=engine_routes,
        provenance={"source": "claude-account-models", "observedAt": 1_700_000_000_000},
    )

    routes = store.list_model_routes(owner="bridge-owner", connection_id=connection.id)
    assert account.id == "host-account-bridge"
    assert [route.id for route in routes] == [engine_routes[0]["id"]]
    assert routes[0].provider_model_id == "claude-sonnet-4-6"
    assert routes[0].provenance == {
        "authority": "managed-engine",
        "origin": "account-discovery",
        "catalog": "claude-account-models",
        "familyID": "anthropic",
        "source": "claude-account-models",
        "observedAt": 1_700_000_000_000,
    }


def test_real_engine_local_routes_cross_the_shared_validator():
    environment = {**os.environ, "BRIDGE_CASE": "local"}
    completed = subprocess.run(
        ["bun", "run", str(FIXTURE)],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    engine_result = json.loads(completed.stdout)
    validate_engine_method_result(
        "_openclank/provider-control/v1/connection/validate", engine_result
    )
    assert [route["modelID"] for route in engine_result["modelRoutes"]] == [
        "fastembed/default",
        "kokoro/Kokoro-82M",
        "faster-whisper/default",
        "diffusion/default",
        "realesrgan/x4plus",
        "briaai/RMBG-1.4",
        "gfpgan/clean-v1",
    ]
    kokoro = engine_result["modelRoutes"][1]
    assert kokoro["capabilities"] == {
        "localExecutorID": "openclank.kokoro.v1",
        "runtimeStatus": "unknown",
        "artifacts": True,
        "format": "wav",
        "voice": "af_heart",
        "speed": 1,
    }
