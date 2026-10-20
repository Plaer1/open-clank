from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from services.stats.ledger import capture_operation_result
from core.stats_models import StatsEvent
from src.openclank.operation_router import ManagedOperationRequest, ManagedOperationResult


def test_managed_terminal_producer_preserves_usage_categories_and_profile(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'producer.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    request = ManagedOperationRequest(
        owner="alice", operation="chat.complete", input={}, purpose="chat",
    )
    result = ManagedOperationResult(
        operation_id="operation-1", root_operation_id="root-1",
        operation="chat.complete", state="complete", committed=True,
        replayed=False, model_route_id="route-1", connection_id="connection-1",
        billing_lane="mutable-result-lane", output={}, artifacts=(),
        usage={"inputTokens": 100, "outputTokens": 30,
               "cacheReadTokens": 40, "cacheWriteTokens": 10,
               "reasoningTokens": 5},
        normalization_profile="acp-separated-v1",
    )
    db = factory()
    try:
        frozen_route = SimpleNamespace(
            billing_lane="frozen-api-lane",
            provider_id="frozen-provider",
            model_id="frozen-model",
        )
        capture_operation_result(db, request, result, selected_route=frozen_route)
        db.commit()
        row = db.query(StatsEvent).one()
        assert row.input_tokens == 100
        assert row.output_tokens == 30
        assert row.cache_read_tokens == 40
        assert row.cache_write_tokens == 10
        assert row.reasoning_tokens == 5
        assert row.event_metadata["normalization_profile"] == "acp-separated-v1"
        assert row.event_metadata["billing_lane"] == "frozen-api-lane"
        assert row.provider_id == "frozen-provider"
        assert row.actual_model == "frozen-model"
    finally:
        db.close()
        engine.dispose()
