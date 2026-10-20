"""Current host logging schema declarations and read-only startup admission."""
from sqlalchemy import Column, String, Integer, JSON, LargeBinary, DateTime, UniqueConstraint
from datetime import datetime, timezone
from sqlalchemy import inspect
from sqlalchemy.orm import declarative_base

LoggingBase = declarative_base()


def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LoggingAttemptDetail(LoggingBase):
    __tablename__ = "logging_attempt_details"
    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    instance_id = Column(String, nullable=False)
    operation_id = Column(String, nullable=True, index=True)
    attempt_id = Column(String, nullable=False, index=True)
    root_operation_id = Column(String, nullable=False)
    policy_revision = Column(Integer, nullable=False)
    attribution = Column(JSON, nullable=False)
    outcome = Column(String, nullable=True)
    capture_state = Column(String, nullable=False, default="pending")
    timing = Column(JSON, nullable=False, default=dict)
    loss_reasons = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, nullable=False, default=utcnow_naive)
    updated_at = Column(DateTime, nullable=False, default=utcnow_naive)
    __table_args__ = (UniqueConstraint("owner", "instance_id", "operation_id", "attempt_id", name="uq_logging_attempt"),)


class LoggingCaptureBody(LoggingBase):
    __tablename__ = "logging_capture_bodies"
    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    attempt_detail_id = Column(String, nullable=False, index=True)
    category = Column(String, nullable=False)
    compressed_body = Column(LargeBinary, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    truncated = Column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint("owner", "attempt_detail_id", "category", name="uq_logging_capture_body"),)


class LoggingCaptureEvent(LoggingBase):
    __tablename__ = "logging_capture_events"
    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    attempt_detail_id = Column(String, nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    payload = Column(JSON, nullable=False)
    size_bytes = Column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint("owner", "attempt_detail_id", "sequence", name="uq_logging_capture_event"),)


def validate_logging_schema(bind):
    """Admit complete current logging schema without creating or upgrading it."""
    inspector = inspect(bind)
    existing = set(inspector.get_table_names())
    missing = []
    for table in LoggingBase.metadata.sorted_tables:
        if table.name not in existing:
            missing.append(table.name)
            continue
        columns = {column["name"]: column for column in inspector.get_columns(table.name)}
        missing.extend(f"{table.name}.{column.name}" for column in table.columns if column.name not in columns)
        for column in table.columns:
            if column.name in columns and bool(columns[column.name]["nullable"]) != bool(column.nullable):
                missing.append(f"{table.name}.{column.name} nullability")
        expected_pk = tuple(column.name for column in table.primary_key.columns)
        if tuple(inspector.get_pk_constraint(table.name).get("constrained_columns") or []) != expected_pk:
            missing.append(f"{table.name} primary key")
        indexes = inspector.get_indexes(table.name)
        for index in table.indexes:
            if not any(item["name"] == index.name and tuple(item["column_names"]) == tuple(column.name for column in index.columns) for item in indexes):
                missing.append(f"{table.name}.{index.name}")
        unique_columns = {tuple(item["column_names"]) for item in inspector.get_unique_constraints(table.name)}
        unique_columns.update(tuple(item["column_names"]) for item in indexes if item.get("unique"))
        for constraint in table.constraints:
            if isinstance(constraint, UniqueConstraint) and tuple(column.name for column in constraint.columns) not in unique_columns:
                missing.append(f"{table.name}.{constraint.name}")
    if missing:
        raise RuntimeError("Logging store does not match this release. Stop writers and keep a complete backup; restore the matching release or prepare an offline conversion. No runtime conversion was performed. Missing schema: " + ", ".join(missing))


def recover_pending_captures(bind):
    """Canonical host-startup capture repair, separate from provider outcome."""
    from sqlalchemy import select, bindparam
    repaired = 0
    while True:
        with bind.begin() as connection:
            rows = connection.execute(select(LoggingAttemptDetail.id, LoggingAttemptDetail.loss_reasons).where(
                LoggingAttemptDetail.capture_state == "pending").limit(1000)).all()
            if not rows:
                return repaired
            statement = LoggingAttemptDetail.__table__.update().where(
                LoggingAttemptDetail.id == bindparam("_capture_id")).values(
                    capture_state="dropped", loss_reasons=bindparam("_capture_losses"), updated_at=utcnow_naive())
            connection.execute(statement, [{"_capture_id": row.id,
                "_capture_losses": list(dict.fromkeys((row.loss_reasons or [])+["interrupted"]))} for row in rows])
            repaired += len(rows)
