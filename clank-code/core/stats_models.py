"""Canonical, content-free Stats observations for the S01 foundation."""

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint

from core.database import Base, utcnow_naive


class StatsEvent(Base):
    """An immutable numeric observation emitted at a canonical host seam."""

    __tablename__ = "stats_events"

    id = Column(String, primary_key=True)
    replay_key = Column(String, nullable=False, unique=True, index=True)
    owner = Column(String, nullable=False, index=True)
    session_id = Column(String, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=True, index=True)
    message_id = Column(String, ForeignKey("chat_messages.id", ondelete="CASCADE"), nullable=True, index=True)
    root_operation_id = Column(String, nullable=True, index=True)
    operation_id = Column(String, nullable=True, index=True)
    attempt_id = Column(String, nullable=True, index=True)
    parent_id = Column(String, nullable=True, index=True)
    event_kind = Column(String, nullable=False, index=True)
    # Explicitly distinguishes a provider-operation aggregate from a
    # conversation/root aggregate when both share one root operation ID.
    observation_scope = Column(String, nullable=False, default="message", index=True)
    event_time = Column(DateTime, nullable=False, index=True)
    actor_kind = Column(String, nullable=False, default="foreground")
    workspace_id = Column(String, nullable=True, index=True)
    provider_id = Column(String, nullable=True)
    account_id = Column(String, nullable=True)
    route_id = Column(String, nullable=True)
    requested_model = Column(String, nullable=True)
    actual_model = Column(String, nullable=True)
    model_fingerprint = Column(String, nullable=True)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    cache_read_tokens = Column(Integer, nullable=True)
    cache_write_tokens = Column(Integer, nullable=True)
    reasoning_tokens = Column(Integer, nullable=True)
    input_tokens_state = Column(String, nullable=False, default="unavailable")
    output_tokens_state = Column(String, nullable=False, default="unavailable")
    cache_read_tokens_state = Column(String, nullable=False, default="unavailable")
    cache_write_tokens_state = Column(String, nullable=False, default="unavailable")
    reasoning_tokens_state = Column(String, nullable=False, default="unavailable")
    token_state = Column(String, nullable=False, default="unavailable")
    observation_kind = Column(String, nullable=False, default="final_snapshot")
    sequence = Column(Integer, nullable=True)
    supersedes_replay_key = Column(String, nullable=True)
    status = Column(String, nullable=False, default="complete")
    terminal = Column(Boolean, nullable=False, default=True)
    billable = Column(Boolean, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    source = Column(String, nullable=False)
    producer_revision = Column(String, nullable=False)
    event_metadata = Column("metadata", JSON, nullable=False, default=dict)
    incognito = Column(Boolean, nullable=False, default=False)
    # Explicit aggregate coverage is typed so projections never infer it from
    # a root operation alone.  Missing coverage remains unavailable.
    attempt_coverage = Column(String, nullable=False, default="unavailable")
    ingested_at = Column(DateTime, nullable=False, default=utcnow_naive)

    __table_args__ = (
        Index("ix_stats_owner_event_time", "owner", "event_time"),
        Index("ix_stats_owner_kind", "owner", "event_kind"),
        Index("ix_stats_attempt_sequence", "attempt_id", "sequence"),
    )


class StatsPriceSchedule(Base):
    """Immutable, explicitly admitted price authority; no fuzzy matching."""

    __tablename__ = "stats_price_schedules"

    id = Column(String, primary_key=True)
    provider_id = Column(String, nullable=False, index=True)
    billing_lane = Column(String, nullable=False, index=True)
    model_identity = Column(String, nullable=True, index=True)
    route_id = Column(String, nullable=True, index=True)
    token_category = Column(String, nullable=False, index=True)
    currency = Column(String, nullable=False)
    rate_numerator = Column(Integer, nullable=False)
    rate_denominator = Column(Integer, nullable=False)
    rate_unit = Column(String, nullable=False, default="currency_major_per_token")
    effective_start = Column(DateTime, nullable=False, index=True)
    effective_end = Column(DateTime, nullable=True, index=True)
    source_url = Column(String, nullable=False)
    source_hash = Column(String, nullable=False)
    retrieved_at = Column(DateTime, nullable=False, default=utcnow_naive)
    admission_revision = Column(String, nullable=False, index=True)
    active = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("provider_id", "billing_lane", "model_identity", "route_id", "token_category", "currency", "effective_start", "admission_revision", name="uq_stats_price_schedule_revision"),
        Index("ix_stats_price_lookup", "provider_id", "billing_lane", "model_identity", "route_id", "token_category", "effective_start"),
    )


class StatsQuotaObservation(Base):
    __tablename__ = "stats_quota_observations"
    id = Column(String, primary_key=True)
    replay_key = Column(String, nullable=False, unique=True, index=True)
    owner = Column(String, nullable=False, index=True)
    account_id = Column(String, nullable=False, index=True)
    provider_id = Column(String, nullable=False, index=True)
    operation_id = Column(String, nullable=True, index=True)
    window_id = Column(String, nullable=False, index=True)
    window_kind = Column(String, nullable=False)
    label = Column(String, nullable=True)
    utilization_numerator = Column(Integer, nullable=True)
    utilization_denominator = Column(Integer, nullable=True)
    limit_value = Column(Integer, nullable=True)
    remaining_value = Column(Integer, nullable=True)
    reset_at = Column(DateTime, nullable=True)
    observed_at = Column(DateTime, nullable=False)
    received_at = Column(DateTime, nullable=False, default=utcnow_naive)
    source_kind = Column(String, nullable=False)
    adapter_revision = Column(String, nullable=False)
    state = Column(String, nullable=False)
    error_class = Column(String, nullable=True)


class StatsQuotaCycle(Base):
    __tablename__ = "stats_quota_cycles"
    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    account_id = Column(String, nullable=False, index=True)
    provider_id = Column(String, nullable=False)
    window_id = Column(String, nullable=False)
    cycle_key = Column(String, nullable=False, unique=True)
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)
    source_kind = Column(String, nullable=False)
    adapter_revision = Column(String, nullable=False)
