"""Managed-only capture admission, bounded persistence and owned detail reads."""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import secrets
import threading
import time
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import urlsplit
from datetime import datetime, timedelta

from sqlalchemy import func, cast, String
from core.database import SessionLocal, utcnow_naive
from services.stats.privacy import identity_handle, owner_scope
from src.openclank.logging_models import LoggingAttemptDetail, LoggingCaptureBody, LoggingCaptureEvent
from src.openclank.logging_policy import _STORE as POLICY

MAX_BODY_BYTES = 256 * 1024
MAX_EVENTS = 256
MAX_BATCH_BYTES = 1024 * 1024
MAX_ADMISSIONS = 10000
_PROBE_WRITES = ThreadPoolExecutor(max_workers=2, thread_name_prefix="logging-probe")
_PROBE_CAPACITY = threading.BoundedSemaphore(32)
PROFILE_BY_WIRE = {"chat-completions.v1": "openai-wire-inclusive-v1", "responses.v1": "openai-wire-inclusive-v1",
                   "anthropic-messages.v1": "anthropic-wire-separated-v1"}


class CaptureError(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def sanitize(value, depth=0, *, binary=False):
    if depth > 20:
        return "[depth limit]"
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            name = str(key).lower().replace("_", "").replace("-", "")
            if name in {"authorization", "proxyauthorization", "cookie", "setcookie", "apikey", "credential", "credentials",
                        "accesstoken", "refreshtoken", "password", "admissionid", "leaseid", "xapikey", "xgoogapikey", "secret", "token"}:
                continue
            if not binary and isinstance(child, str) and (name in {"b64json", "base64", "imagedata", "audiodata"} or name == "data" and (value.get("type") == "base64" or value.get("format") in {"wav", "mp3", "pcm16"})):
                result[str(key)] = {"omitted": "embedded_media", "encoded_bytes": len(child.encode())}
            else:
                result[str(key)] = sanitize(child, depth+1, binary=binary)
        return result
    if isinstance(value, list):
        return [sanitize(child, depth+1, binary=binary) for child in value[:MAX_EVENTS]]
    if isinstance(value, str):
        if value.startswith("data:") and not binary:
            return {"media_type": value.split(";", 1)[0][5:80], "encoded_bytes": len(value.encode()), "omitted": "embedded_media"}
        return value[:MAX_BODY_BYTES]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return "[unsupported]"


def sanitize_headers(value):
    """Dedicated header redaction; never depend on body field filtering."""
    result, total = [], 0
    if isinstance(value, dict):
        value = [{"name": name, "values": values if isinstance(values, list) else [values]} for name, values in value.items()]
    if not isinstance(value, list):
        return []
    for item in value[:64]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip().lower()[:128]
        if not re.fullmatch(r"[a-z0-9!#$%&'*+.^_`|~-]+", name):
            continue
        compact = name.replace("-", "").replace("_", "")
        secret = (any(part in compact for part in ("authorization", "cookie", "credential", "secret"))
                  or compact.endswith("apikey") or compact.endswith("token")
                  or compact in {"token", "xamzsecuritytoken", "xsessiontoken", "xcsrftoken", "xauth"})
        if secret or item.get("state") == "redacted":
            result.append({"name": name, "values": [], "state": "redacted"})
            continue
        if item.get("state") == "omitted":
            result.append({"name": name, "values": [], "state": "omitted"})
            continue
        values = item.get("values") or []
        if not isinstance(values, list):
            values = [values]
        safe, truncated = [], len(values) > 8 or item.get("state") == "truncated"
        for raw in values[:8]:
            text = str(raw).replace("\r", " ").replace("\n", " ")
            if re.search(r"(?i)\b(?:bearer|basic)\s+[a-z0-9/+_.=-]+|\bsk-[a-z0-9_-]{8,}", text):
                safe = []
                secret = True
                break
            encoded = text.encode()
            remaining = max(0, min(2048, 32768-total))
            bounded = encoded[:remaining].decode(errors="ignore")
            truncated = truncated or len(encoded) > remaining
            if remaining:
                safe.append(bounded)
                total += len(bounded.encode())
        result.append({"name": name, "values": [] if secret else safe,
                       "state": "redacted" if secret else "truncated" if truncated else "reported"})
    if len(value) > 64:
        result.append({"name": "capture-limit", "values": [], "state": "truncated"})
    return result


def sanitize_http(value):
    if not isinstance(value, dict):
        return {"coverage": {"state": "unsupported"}}
    result = {}
    if isinstance(value.get("method"), str):
        result["method"] = value["method"][:16].upper()
    endpoint = value.get("endpoint")
    if isinstance(endpoint, dict):
        parsed = urlsplit(str(endpoint.get("origin") or ""))
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            hostname = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            origin = f"{parsed.scheme}://{hostname}"
            try:
                if parsed.port:
                    origin += f":{parsed.port}"
            except ValueError:
                pass
            path = urlsplit(str(endpoint.get("path") or "/")).path[:1024]
            path = re.sub(r"(?i)(?:sk-[a-z0-9_-]+|(?<=/)[a-z0-9_-]{64,})", "[redacted]", path)
            segments = path.split("/")
            for index in range(1, len(segments)):
                if segments[index-1].lower() in {"key", "token", "secret", "credential", "auth", "api-key", "api_key"}:
                    segments[index] = "[redacted]"
            result["endpoint"] = {"origin": origin[:512], "path": "/".join(segments)}
    status = value.get("status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        result["status"] = status
    for key in ("requestHeaders", "responseHeaders"):
        if key in value:
            result[key] = sanitize_headers(value[key])
    if isinstance(value.get("coverage"), dict):
        result["coverage"] = {key: str(child)[:128] for key, child in value["coverage"].items()
                              if key in {"state", "request", "response", "reason"}}
    return result


class LoggingCaptureStore:
    def __init__(self, session_factory=SessionLocal, *, policy_store=POLICY):
        self.session_factory = session_factory
        self.policy = policy_store
        self.lock = threading.RLock()
        self.admissions = {}
        self.health = {}
        self.previews = {}
        self.finished = {}

    def admit(self, *, owner, holder_id, context, binding):
        """One validated bound scope; no database work precedes dispatch."""
        if not context or context.get("owner") != owner or context.get("holder_id") != holder_id or context.get("root_operation_id") != binding.root_operation_id:
            raise CaptureError("inactive or foreign logging operation")
        policy = self.policy.pin_policy(owner, binding.root_operation_id, holder_id=holder_id)
        private = bool(context.get("incognito") or context.get("temporary") or
                       self.policy.private_operation(owner, binding.root_operation_id))
        persistence = {"numeric": not private, "content": not private,
                       "reason": "incognito" if context.get("incognito") else "temporary" if private else "normal"}
        attribution = {key: context.get(key) for key in (
            "root_operation_id", "operation_id", "session_id", "workspace_id", "operation_type", "actor_id", "actor_kind", "route_id")}
        attribution.update(instance_id=holder_id, provider_id=binding.provider_id,
                           connection_id=binding.connection_id, account_id=binding.selected_account_id,
                           billing_lane=binding.billing_lane, requested_model=binding.model_id, actual_model=None,
                           identity_coverage="complete" if context.get("operation_id") else "partial")
        expired = []
        with self.lock:
            now = time.monotonic()
            for pool in (self.admissions, self.finished):
                for token, record in list(pool.items()):
                    if record["expires"] <= now:
                        expired.append(record)
                        pool.pop(token, None)
            if len(self.admissions) + len(self.finished) >= MAX_ADMISSIONS:
                raise CaptureError("capture admission capacity")
            key = (owner, holder_id, binding.id, binding.revision, attribution.get("operation_id"))
            existing = next((rec for rec in self.admissions.values() if rec["key"] == key), None)
            if existing:
                return copy.deepcopy(existing["result"])
            token = secrets.token_urlsafe(32)
            result = {"admissionID": token, "policy": {key: policy[key] for key in (
                "revision", "advanced_enabled", "request_body_enabled", "response_body_enabled", "binary_body_enabled")},
                "context": {"rootOperationID": binding.root_operation_id, "operationID": attribution.get("operation_id"),
                    "instanceID": holder_id, "providerID": binding.provider_id, "bindingID": binding.id,
                    "bindingRevision": binding.revision, "persistence": persistence,
                    "identityCoverage": attribution["identity_coverage"],
                    "transportMode": "advanced_proxy" if policy["advanced_enabled"] and persistence["content"] else "direct"}}
            self.admissions[token] = {"key": key, "context": attribution, "policy": policy,
                "persistence": persistence, "expires": now + 24*3600, "dispatches": {}, "result": result}
        for record in expired:
            self._close_gap(record, "delivery_timeout")
        return copy.deepcopy(result)

    def observe_probe(self, *, owner, provider_id, endpoint_id, model_id, url, headers, body, send):
        """Authenticated app-owned probe transport; no external interception.

        The caller owns resolved HTTP credentials and the exact send operation.
        Persist on a bounded worker after the original request settles.
        """
        if not owner:
            raise CaptureError("trusted probe owner required")
        owner = str(owner).strip().lower()
        root, dispatch = "probe_"+uuid.uuid4().hex, uuid.uuid4().hex
        holder = "host-model-probe"
        parsed = urlsplit(url)
        connection = str(endpoint_id or "probe_endpoint_"+_hash(parsed.hostname or "")[:16])
        context = {"owner": owner, "holder_id": holder, "root_operation_id": root, "operation_id": None,
            "operation_type": "model.probe", "actor_kind": "internal", "identity_coverage": "partial"}
        admission, journal, journal_store = None, None, None
        started = time.monotonic()
        try:
            from src.openclank.operation_journal import OperationJournalStore
            journal_store = OperationJournalStore(self.session_factory)
            journal, _ = journal_store.begin(owner=owner, root_operation_id=root, operation="chat.complete",
                idempotency_key=root, request={"kind": "model.probe", "model": model_id, "endpoint": connection},
                connection_id=connection, billing_lane="local" if provider_id == "ollama" else "api",
                model_route_id=connection)
            context["operation_id"] = journal.id
            context["identity_coverage"] = "complete"
            binding = SimpleNamespace(id="probe_binding_"+dispatch, revision=1, root_operation_id=root,
                provider_id=provider_id or "compatible", connection_id=connection, selected_account_id=None,
                billing_lane="local" if provider_id == "ollama" else "api", model_id=model_id)
            admission = self.admit(owner=owner, holder_id=holder, context=context, binding=binding)
        except Exception:
            self._loss(owner, "admission_unavailable")
        response, failure = None, False
        try:
            response = send()
            return response
        except BaseException:
            failure = True
            raise
        finally:
            try:
                elapsed = (time.monotonic()-started)*1000
                # Headers/content never enter the ordinary numeric ledger.
                metrics, coverage, losses, response_body = {}, {}, [], None
                try:
                    if response is not None and len(response.content) <= MAX_BODY_BYTES:
                        response_body = response.json()
                        usage = response_body.get("usage") or {} if isinstance(response_body, dict) else {}
                        for key, candidates in {
                            "input_tokens": ("input_tokens", "prompt_tokens"), "output_tokens": ("output_tokens", "completion_tokens"),
                            "cache_read_tokens": ("cache_read_input_tokens",), "cache_write_tokens": ("cache_creation_input_tokens",),
                        }.items():
                            raw = next((usage[name] for name in candidates if name in usage), None)
                            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                                metrics[key] = raw
                        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
                        outputs = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
                        for key, raw in (("cache_read_tokens", details.get("cached_tokens")), ("reasoning_tokens", outputs.get("reasoning_tokens"))):
                            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                                metrics[key] = raw
                        if provider_id == "ollama":
                            for key, field in (("input_tokens", "prompt_eval_count"), ("output_tokens", "eval_count")):
                                raw = response_body.get(field)
                                if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                                    metrics[key] = raw
                    elif response is not None:
                        losses.append("truncated")
                except Exception:
                    losses.append("parse_degraded")
                for key in metrics:
                    coverage[key] = {"state": "reported", "coverage": "complete", "source": "wire"}
                profile = "anthropic-wire-separated-v1" if provider_id == "anthropic" else None if provider_id == "ollama" else "openai-wire-inclusive-v1"
                outcome = "upstream_error" if failure or response is not None and response.status_code >= 400 else "completed"
                payload = {"dispatchID": dispatch, "source": "wire", "sequence": 0, "terminal": True,
                    "outcome": outcome, "metrics": metrics, "metricCoverage": coverage,
                    "normalizationProfile": profile, "identityCoverage": context["identity_coverage"],
                    "lossReasons": losses + ([] if admission else ["admission_unavailable"]),
                    "dispatchIndex": 0, "timing": {"clock_domain": holder, "terminal_ms": elapsed}, "billable": None}
                if admission:
                    payload["admissionID"] = admission["admissionID"]
                    if admission["context"]["transportMode"] == "advanced_proxy":
                        payload["http"] = sanitize_http({"method": "POST", "endpoint": {"origin": parsed.scheme+"://"+parsed.netloc, "path": parsed.path},
                            "requestHeaders": headers, **({"status": response.status_code, "responseHeaders": [{"name": key, "values": [value]} for key, value in response.headers.multi_items()]} if response is not None else {}),
                            "coverage": {"state": "reported" if response is not None else "partial"}})
                        if admission["policy"]["request_body_enabled"]:
                            payload["requestBody"] = sanitize(body, binary=admission["policy"]["binary_body_enabled"])
                        if response_body is not None and admission["policy"]["response_body_enabled"]:
                            payload["responseBody"] = sanitize(response_body, binary=admission["policy"]["binary_body_enabled"])
                def settle_journal():
                    if journal:
                        try:
                            journal_store.cas(owner=owner, operation_id=journal.id, expected_revision=journal.revision,
                                state="failed" if outcome == "upstream_error" else "complete",
                                commit_reason="probe_dispatch_settled")
                        except Exception:
                            self._loss(owner, "journal_settlement_unavailable")
                def persist():
                    try:
                        if admission:
                            self.events(owner=owner, holder_id=holder, payload=payload)
                        else:
                            from services.stats.ledger import capture_attempt_event
                            db = self.session_factory()
                            try:
                                capture_attempt_event(db, owner=owner, context={**context, "instance_id": holder,
                                    "provider_id": provider_id or "compatible", "connection_id": connection,
                                    "requested_model": model_id}, attempt_id=dispatch,
                                    payload={"source": "wire", "sequence": 0, "terminal": True, "outcome": outcome,
                                        "metrics": metrics, "metric_coverage": coverage, "normalization_profile": profile,
                                        "identity_coverage": context["identity_coverage"], "loss_reasons": payload["lossReasons"],
                                        "timing": payload["timing"]})
                                db.commit()
                            finally:
                                db.close()
                    except Exception:
                        self._loss(owner, "write_failed")
                    finally:
                        settle_journal()
                        if admission:
                            self.finish_operation(owner, holder, root)
                if _PROBE_CAPACITY.acquire(blocking=False):
                    try:
                        future = _PROBE_WRITES.submit(persist)
                        future.add_done_callback(lambda _: _PROBE_CAPACITY.release())
                    except Exception:
                        _PROBE_CAPACITY.release()
                        self._loss(owner, "write_failed")
                        settle_journal()
                        self.finish_operation(owner, holder, root)
                else:
                    self._loss(owner, "queue_full")
                    settle_journal()
                    self.finish_operation(owner, holder, root)
            except Exception:
                self._loss(owner, "write_failed")
                self.finish_operation(owner, holder, root)

    def _loss(self, owner, kind):
        with self.lock:
            health = self.health.setdefault(owner, {"last_successful_write": None, "losses": {}})
            health["losses"][kind] = health["losses"].get(kind, 0)+1

    def events(self, *, owner, holder_id, payload):
        """Persist ordinary facts and optional detail under a scope capability.

        Only map bookkeeping uses the global lock. Each dispatch owns its own
        write lock; database, compression and retention never block admission.
        """
        token, dispatch_id = payload.get("admissionID"), payload.get("dispatchID")
        if not isinstance(dispatch_id, str) or not 1 <= len(dispatch_id) <= 128:
            raise CaptureError("invalid dispatch identity")
        sequence, source = payload.get("sequence"), payload.get("source")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0 or source not in {"wire", "sdk", "acp", "executor"}:
            raise CaptureError("invalid observation identity")
        if len(json.dumps(payload, ensure_ascii=False).encode()) > MAX_BATCH_BYTES:
            raise CaptureError("capture batch limit")
        with self.lock:
            scope = self.admissions.get(token) or self.finished.get(token)
            if not scope or scope["key"][:2] != (owner, holder_id) or scope["expires"] < time.monotonic():
                raise CaptureError("invalid capture admission")
            if not scope["persistence"]["numeric"] or self.policy.private_operation(owner, scope["context"]["root_operation_id"]):
                return {"accepted": True, "replayed": False, "captureState": "private"}
            record = scope["dispatches"].get(dispatch_id)
            if record is None:
                if len(scope["dispatches"]) >= 1000:
                    self._loss(owner, "queue_full")
                    return {"accepted": False, "replayed": False, "captureState": "queue_full"}
                identity = f"{owner}:{holder_id}:{scope['context'].get('operation_id')}:{dispatch_id}"
                record = {"context": scope["context"], "policy": scope["policy"], "attempt_id": dispatch_id,
                    "detail_id": _hash(identity), "terminal": False, "sequences": {}, "event_count": 0,
                    "event_bytes": 0, "lock": threading.RLock(), "gap_sequence": 0}
                scope["dispatches"][dispatch_id] = record
        with record["lock"]:
            if scope.get("erased"):
                return {"accepted": False, "replayed": False, "captureState": "erased"}
            if sequence <= record["sequences"].get(source, -1):
                return {"accepted": True, "replayed": True}
            if payload.get("terminal") and payload.get("outcome") not in {"completed", "upstream_error", "cancelled", "disconnected", "interrupted", "unknown"}:
                raise CaptureError("invalid terminal outcome")
            normalized = {"sequence": sequence, "source": source, "terminal": bool(payload.get("terminal")),
                "outcome": payload.get("outcome"), "metrics": payload.get("metrics") or {},
                "metric_coverage": payload.get("metricCoverage") or {},
                "normalization_profile": payload.get("normalizationProfile"), "actual_model": payload.get("actualModel"),
                "timing": payload.get("timing") or {}, "billable": payload.get("billable"),
                "dispatch_index": payload.get("dispatchIndex"), "retry_of_dispatch_id": payload.get("retryOfDispatchID"),
                "covered_dispatch_ids": payload.get("coveredDispatchIDs"), "identity_coverage": record["context"]["identity_coverage"],
                "loss_reasons": payload.get("lossReasons") or [],
                "observation_kind": payload.get("observationKind", "final_snapshot" if payload.get("terminal") else "cumulative_snapshot")}
            saved_counts = record["event_count"], record["event_bytes"]
            db = self.session_factory()
            detail_failed = False
            try:
                from services.stats.ledger import capture_attempt_event
                capture_attempt_event(db, owner=owner, context=record["context"], attempt_id=dispatch_id, payload=normalized)
                if normalized["terminal"] and isinstance(payload.get("quota"), dict):
                    try:
                        with db.begin_nested():
                            from services.stats.quota_adapters import native_capacity_envelope, admit_terminal_quota_payload
                            from datetime import timezone
                            context = record["context"]
                            quota = native_capacity_envelope(context["provider_id"], payload["quota"], observed_at=datetime.now(timezone.utc))
                            if quota:
                                admit_terminal_quota_payload(db, owner=owner, operation_id=context.get("operation_id"), root_operation_id=context["root_operation_id"], connection_id=context.get("connection_id"), provider_id=context["provider_id"], billing_lane=context.get("billing_lane"), account_id=context.get("account_id"), payload=quota)
                    except Exception:
                        self._loss(owner, "quota_observation_unavailable")
                if record["policy"]["advanced_enabled"] and scope["persistence"]["content"]:
                    try:
                        with db.begin_nested():
                            self._persist_detail(db, owner, record, payload)
                    except Exception:
                        detail_failed = True
                        record["event_count"], record["event_bytes"] = saved_counts
                        self._loss(owner, "write_failed")
                        # Persist the optional-detail gap in the same numeric authority.
                        record["gap_sequence"] += 1
                        gap = {**normalized, "source": "host", "sequence": record["gap_sequence"],
                               "metrics": {}, "loss_reasons": ["write_failed"]}
                        capture_attempt_event(db, owner=owner, context=record["context"], attempt_id=dispatch_id, payload=gap)
                db.commit()
                record["sequences"][source] = sequence
                record["terminal"] = record["terminal"] or normalized["terminal"]
                with self.lock:
                    self.health.setdefault(owner, {"losses": {}})["last_successful_write"] = utcnow_naive().isoformat()
            except Exception:
                db.rollback()
                record["event_count"], record["event_bytes"] = saved_counts
                self._loss(owner, "write_failed")
                return {"accepted": False, "replayed": False, "captureState": "write_failed"}
            finally:
                db.close()
            if detail_failed:
                self._finalize_detail_gap(owner, record, "write_failed")
            if normalized["terminal"]:
                try:
                    self.maintain_retention(owner, self.policy.get_policy(owner))
                except Exception:
                    self._loss(owner, "retention_unavailable")
            return {"accepted": not detail_failed, "replayed": False, "captureState": "write_failed" if detail_failed else "ready"}

    def _persist_detail(self, db, owner, record, payload):
        row = db.query(LoggingAttemptDetail).filter_by(id=record["detail_id"], owner=owner).first()
        if row is None:
            context = record["context"]
            row = LoggingAttemptDetail(id=record["detail_id"], owner=owner, instance_id=context["instance_id"],
                operation_id=context.get("operation_id"), attempt_id=record["attempt_id"], root_operation_id=context["root_operation_id"],
                policy_revision=record["policy"]["revision"], attribution={**context, "content_preferences": {key: record["policy"][key] for key in ("request_body_enabled", "response_body_enabled", "binary_body_enabled")}}, timing={}, loss_reasons=[], capture_state="pending")
            db.add(row)
        row.updated_at = utcnow_naive()
        attribution = dict(row.attribution)
        if isinstance(payload.get("actualModel"), str) and payload["actualModel"]:
            attribution.update(actual_model=payload["actualModel"][:256], model_identity_source="provider_response")
        if payload.get("http") is not None:
            previous = attribution.get("http") or {}
            attribution["http"] = {**previous, **sanitize_http(payload["http"])}
        attribution["dispatch_index"] = payload.get("dispatchIndex", attribution.get("dispatch_index"))
        attribution["retry_of_dispatch_id"] = payload.get("retryOfDispatchID", attribution.get("retry_of_dispatch_id"))
        row.attribution = attribution
        row.timing = {**(row.timing or {}), **sanitize(payload.get("timing") or {})}
        losses = list(dict.fromkeys((row.loss_reasons or []) + list(payload.get("lossReasons") or [])))[:16]
        for category, switch in (("request", "request_body_enabled"), ("response", "response_body_enabled")):
            field = category+"Body"
            if field not in payload or not record["policy"][switch]:
                continue
            raw = json.dumps(sanitize(payload[field], binary=record["policy"]["binary_body_enabled"]), ensure_ascii=False).encode()
            truncated = len(raw) > MAX_BODY_BYTES
            bounded = raw[:MAX_BODY_BYTES].decode("utf-8", errors="ignore").encode()
            if truncated:
                losses.append("truncated")
            body = db.query(LoggingCaptureBody).filter_by(owner=owner, attempt_detail_id=row.id, category=category).first()
            if body is None:
                body = LoggingCaptureBody(id=_hash(row.id+category), owner=owner, attempt_detail_id=row.id, category=category)
                db.add(body)
            body.compressed_body, body.size_bytes, body.truncated = gzip.compress(bounded), len(bounded), int(truncated)
        # Wire event bodies only; SDK numeric snapshots never duplicate them.
        events = (payload.get("events") or []) if record["policy"]["response_body_enabled"] and payload["source"] == "wire" else []
        remaining = max(0, MAX_EVENTS - record["event_count"])
        count, event_bytes = record["event_count"], record["event_bytes"]
        if len(events) > remaining:
            losses.append("truncated")
        for event in events[:remaining]:
            safe_event = sanitize(event, binary=record["policy"]["binary_body_enabled"])
            size = len(json.dumps(safe_event).encode())
            if event_bytes + size > MAX_BODY_BYTES:
                losses.append("truncated")
                break
            db.add(LoggingCaptureEvent(id=_hash(row.id+str(count)), owner=owner, attempt_detail_id=row.id,
                sequence=count, payload=safe_event, size_bytes=size))
            count += 1
            event_bytes += size
        db.flush()
        record["event_count"], record["event_bytes"] = count, event_bytes
        if payload.get("terminal"):
            row.outcome = payload["outcome"]
            row.capture_state = next((kind for kind in ("write_failed", "delivery_timeout", "queue_full", "dropped", "truncated", "parse_degraded") if kind in losses), "complete")
        row.loss_reasons = list(dict.fromkeys(losses))
        for kind in set(payload.get("lossReasons") or []):
            self._loss(owner, kind)

    def _finalize_detail_gap(self, owner, record, reason):
        db = self.session_factory()
        try:
            row = db.query(LoggingAttemptDetail).filter_by(owner=owner, id=record["detail_id"]).first()
            if row:
                row.capture_state = "write_failed" if reason == "write_failed" else "dropped"
                row.loss_reasons = list(dict.fromkeys((row.loss_reasons or [])+[reason]))
                row.updated_at = utcnow_naive()
                db.commit()
        except Exception:
            db.rollback()
            self._loss(owner, "write_failed")
        finally:
            db.close()

    def _close_gap(self, scope, reason="delivery_timeout"):
        owner = scope["key"][0]
        if not scope["persistence"]["numeric"] or self.policy.private_operation(owner, scope["context"]["root_operation_id"]):
            return
        for record in list(scope["dispatches"].values()):
            with record["lock"]:
                if record["terminal"]:
                    continue
                self._loss(owner, reason)
                db = self.session_factory()
                try:
                    from services.stats.ledger import capture_attempt_event
                    record["gap_sequence"] += 1
                    capture_attempt_event(db, owner=owner, context=record["context"], attempt_id=record["attempt_id"],
                        payload={"source": "host", "sequence": record["gap_sequence"], "terminal": False,
                            "metrics": {}, "metric_coverage": {}, "loss_reasons": [reason], "identity_coverage": record["context"]["identity_coverage"]})
                    db.commit()
                except Exception:
                    db.rollback()
                    self._loss(owner, "write_failed")
                finally:
                    db.close()
                self._finalize_detail_gap(owner, record, reason)

    def finish_operation(self, owner, holder_id, root_operation_id):
        with self.lock:
            records = [(token, rec) for token, rec in self.admissions.items()
                       if rec["key"][:2] == (owner, holder_id) and rec["context"]["root_operation_id"] == root_operation_id]
            for token, record in records:
                record["expires"] = time.monotonic()+300
                self.finished[token] = record
                self.admissions.pop(token, None)
        for _, record in records:
            self._close_gap(record)
        self.policy.release_pin(owner, root_operation_id, holder_id=holder_id)

    def recover_generation(self, owner, holder_id):
        """One-owner startup repair; old capture gaps are not provider outcomes."""
        db = self.session_factory()
        try:
            rows = db.query(LoggingAttemptDetail).filter(LoggingAttemptDetail.owner == owner,
                    LoggingAttemptDetail.instance_id != holder_id, LoggingAttemptDetail.capture_state == "pending").limit(1000).all()
            for row in rows:
                row.capture_state = "dropped"
                row.loss_reasons = list(dict.fromkeys((row.loss_reasons or [])+["interrupted"]))
                row.updated_at = utcnow_naive()
            db.commit()
            if rows:
                self._loss(owner, "interrupted")
        except Exception:
            db.rollback()
            self._loss(owner, "write_failed")
        finally:
            db.close()

    def interrupt_holder(self, owner, holder_id):
        with self.lock:
            records = [rec for pool in (self.admissions, self.finished) for rec in pool.values() if rec["key"][:2] == (owner, holder_id)]
            self.admissions = {token: rec for token, rec in self.admissions.items() if rec["key"][:2] != (owner, holder_id)}
            self.finished = {token: rec for token, rec in self.finished.items() if rec["key"][:2] != (owner, holder_id)}
        for record in records:
            self._close_gap(record, "interrupted")
        self.policy.release_holder(owner, holder_id)

    def status(self, owner):
        db = self.session_factory()
        try:
            byte_total = db.query(func.coalesce(func.sum(LoggingCaptureBody.size_bytes), 0)).filter_by(owner=owner).scalar()
            pending = db.query(LoggingAttemptDetail).filter_by(owner=owner, capture_state="pending").count()
            return {**copy.deepcopy(self.health.get(owner, {"last_successful_write": None, "losses": {}})),
                    "capture_counts": {state: count for state, count in db.query(LoggingAttemptDetail.capture_state, func.count(LoggingAttemptDetail.id)).filter_by(owner=owner).group_by(LoggingAttemptDetail.capture_state)},
                    "storage_state": "ready", "body_bytes": int(byte_total)+int(db.query(func.coalesce(func.sum(LoggingCaptureEvent.size_bytes), 0)).filter_by(owner=owner).scalar()), "pending_captures": pending,
                    "limits": {"body_bytes": MAX_BODY_BYTES, "events_per_attempt": MAX_EVENTS, "event_bytes_per_attempt": MAX_BODY_BYTES, "batch_bytes": MAX_BATCH_BYTES},
                    "formats": {wire: {"state": "supported", "normalization_profile": profile} for wire, profile in PROFILE_BY_WIRE.items()}}
        finally:
            db.close()

    def attempt(self, owner, handle, *, include_bodies=False):
        db = self.session_factory()
        try:
            row = db.query(LoggingAttemptDetail).filter_by(owner=owner, id=handle).first()
            if row is None:
                raise CaptureError("attempt not found")
            context = row.attribution
            public = {"handle": handle, "policy_revision": row.policy_revision, "outcome": row.outcome,
                      "capture_state": row.capture_state, "loss_reasons": row.loss_reasons, "timing": row.timing,
                      "session_handle": identity_handle(owner, "session_id", context["session_id"]) if context.get("session_id") else None,
                      "operation_type": context.get("operation_type"), "actor_id": context.get("actor_id"),
                      "requested_model": context.get("requested_model"), "actual_model": context.get("actual_model"),
                      "http": context.get("http", {"coverage": {"state": "missing"}}),
                      "dispatch_id": row.attempt_id, "dispatch_index": context.get("dispatch_index"),
                      "retry_of_dispatch_id": context.get("retry_of_dispatch_id"),
                      "identity_coverage": context.get("identity_coverage", "partial")}
            try:
                from services.stats.ledger import read_attempt_measurements
                public["measurement"] = read_attempt_measurements(db, owner=owner, instance_id=row.instance_id,
                    root_operation_id=row.root_operation_id, operation_id=row.operation_id, attempt_id=row.attempt_id)
            except Exception:
                public["measurement"] = {"metrics": {}, "metric_coverage": {}, "coverage": "unavailable",
                    "loss_reasons": ["measurement_read_unavailable"], "provenance": []}
            retained = {category for (category,) in db.query(LoggingCaptureBody.category).filter_by(owner=owner, attempt_detail_id=row.id)}
            events_retained = db.query(LoggingCaptureEvent.id).filter_by(owner=owner, attempt_detail_id=row.id).first() is not None
            preferences = context.get("content_preferences") or {}
            pruned = set(context.get("content_pruned_categories") or [])
            coverage = {}
            for category, switch in (("request", "request_body_enabled"), ("response", "response_body_enabled"), ("events", "response_body_enabled")):
                present = events_retained if category == "events" else category in retained
                state = "retained" if present else "pruned" if category in pruned else "disabled" if preferences.get(switch) is False else "missing" if preferences.get(switch) is True else "unknown"
                coverage[category] = {"state": state, "preference": preferences.get(switch), "policy_revision": row.policy_revision}
            coverage["embedded_media"] = {"state": "disabled" if preferences.get("binary_body_enabled") is False else "enabled" if preferences.get("binary_body_enabled") is True else "unknown", "preference": preferences.get("binary_body_enabled")}
            public["content_coverage"] = coverage
            if include_bodies:
                public["bodies"] = [{"category": b.category, "text": gzip.decompress(b.compressed_body).decode(), "truncated": bool(b.truncated)}
                                    for b in db.query(LoggingCaptureBody).filter_by(owner=owner, attempt_detail_id=row.id).all()]
                public["events"] = [event.payload for event in db.query(LoggingCaptureEvent).filter_by(owner=owner, attempt_detail_id=row.id)
                                    .order_by(LoggingCaptureEvent.sequence).limit(MAX_EVENTS).all()]
            return {"schema": "open-clank.logging.v1", "owner_scope": owner_scope(owner), "resource": public}
        finally:
            db.close()

    @staticmethod
    def _session_detail_query(db, owner, session_id):
        if not isinstance(session_id, str) or not session_id or not owner:
            raise CaptureError("trusted owner and exact session identity required")
        return db.query(LoggingAttemptDetail).filter(
            LoggingAttemptDetail.owner == owner,
            LoggingAttemptDetail.attribution["session_id"].as_string() == session_id)

    def session_inventory(self, db, *, owner, session_id):
        """Content-free review counts; caller owns the database transaction."""
        selected = self._session_detail_query(db, owner, session_id)
        detail_ids = selected.with_entities(LoggingAttemptDetail.id)
        with self.lock:
            counts = [sum(rec["key"][0] == owner and rec["context"].get("session_id") == session_id
                          for rec in pool.values()) for pool in (self.admissions, self.finished)]
        return {"scope": "owner_session", "attempt_details": selected.count(),
            "capture_bodies": db.query(LoggingCaptureBody).filter(LoggingCaptureBody.owner == owner,
                LoggingCaptureBody.attempt_detail_id.in_(detail_ids)).count(),
            "capture_events": db.query(LoggingCaptureEvent).filter(LoggingCaptureEvent.owner == owner,
                LoggingCaptureEvent.attempt_detail_id.in_(detail_ids)).count(),
            "active_scopes": counts[0], "finished_scopes": counts[1]}

    def erase_session(self, db, *, owner, session_id):
        """Idempotent exact-session content erasure; Stats/journal stay authoritative.

        The conversation coordinator settles provider work and commits its
        deletion transaction. No provider dispatch or retry belongs here.
        """
        selected = self._session_detail_query(db, owner, session_id)
        with self.lock:
            if any(rec["key"][0] == owner and rec["context"].get("session_id") == session_id
                   for rec in self.admissions.values()):
                raise CaptureError("active capture root requires conversation settlement")
            scopes, counts = [], []
            for pool in (self.admissions, self.finished):
                matching = [(token, rec) for token, rec in pool.items()
                    if rec["key"][0] == owner and rec["context"].get("session_id") == session_id]
                counts.append(len(matching))
                for token, rec in matching:
                    rec["erased"] = True
                    scopes.append(rec)
                    pool.pop(token, None)
            # Paging snapshots carry content handles. Invalidate this owner's
            # cursors/previews rather than return erased handles from memory.
            self.previews = {key: rec for key, rec in self.previews.items() if rec.get("owner") != owner}
        locks = [record["lock"] for scope in scopes for record in scope["dispatches"].values()]
        for lock in locks:
            lock.acquire()
        try:
            detail_ids = selected.with_entities(LoggingAttemptDetail.id)
            events = db.query(LoggingCaptureEvent).filter(LoggingCaptureEvent.owner == owner,
                LoggingCaptureEvent.attempt_detail_id.in_(detail_ids)).delete(synchronize_session=False)
            bodies = db.query(LoggingCaptureBody).filter(LoggingCaptureBody.owner == owner,
                LoggingCaptureBody.attempt_detail_id.in_(detail_ids)).delete(synchronize_session=False)
            details = selected.delete(synchronize_session=False)
            return {"scope": "owner_session", "action": "erased", "attempt_details": details,
                "capture_bodies": bodies, "capture_events": events,
                "active_scopes": counts[0], "finished_scopes": counts[1]}
        finally:
            for lock in reversed(locks):
                lock.release()
            for scope in scopes:
                self.policy.release_pin(owner, scope["context"]["root_operation_id"], holder_id=scope["key"][1])

    def erase_owner(self, db, owner):
        for model in (LoggingCaptureEvent, LoggingCaptureBody, LoggingAttemptDetail):
            db.query(model).filter_by(owner=owner).delete(synchronize_session=False)
        with self.lock:
            self.admissions = {token: rec for token, rec in self.admissions.items() if rec["key"][0] != owner}
            self.health.pop(owner, None)
            self.finished = {token: rec for token, rec in self.finished.items() if rec["key"][0] != owner}

    def maintain_retention(self, owner, policy):
        """Bounded maintenance after capture, only for explicitly chosen rules."""
        if all(policy[key]["mode"] == "keep_until_deleted" for key in ("body_retention", "metadata_retention")):
            return
        db = self.session_factory()
        try:
            result = self._prune_selected(db, owner, policy)
            db.commit()
            self.health.setdefault(owner, {"losses": {}})["retention_maintenance"] = result
        except Exception:
            db.rollback()
            self._loss(owner, "write_failed")
        finally:
            db.close()

    def _revision(self, db, owner):
        values = [db.query(func.count(model.id), func.max(model.id)).filter_by(owner=owner).first()
                  for model in (LoggingAttemptDetail, LoggingCaptureBody, LoggingCaptureEvent)]
        values.append(db.query(func.max(LoggingAttemptDetail.updated_at)).filter_by(owner=owner).scalar())
        return _hash(str(values))

    def attempts(self, owner, *, limit=50, cursor=None, session=None, operation=None, since=None, until=None, provider_id=None, account_id=None, actual_model=None, workspace_id=None, requested_model=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CaptureError("invalid attempt limit")
        filters = {"session": session, "operation": operation, "since": since, "until": until, "provider_id": provider_id, "account_id": account_id, "actual_model": actual_model, "workspace_id": workspace_id, "requested_model": requested_model}
        db = self.session_factory()
        try:
            revision = self._revision(db, owner)
            offset = 0
            if cursor:
                with self.lock:
                    page = self.previews.get(cursor)
                if not page or page["owner"] != owner or page["kind"] != "attempts" or page["expires"] < time.monotonic() or page["filters"] != filters or page["revision"] != revision:
                    raise CaptureError("stale or foreign capture cursor")
                offset = page["offset"]
            query = db.query(LoggingAttemptDetail).filter_by(owner=owner)
            from services.stats.privacy import IdentityCatalog
            for field, handle in (("provider_id", provider_id), ("account_id", account_id), ("actual_model", actual_model), ("workspace_id", workspace_id), ("requested_model", requested_model)):
                if handle:
                    values = [item[0] for item in db.query(LoggingAttemptDetail.attribution[field].as_string()).filter(LoggingAttemptDetail.owner == owner).distinct().limit(10001)]
                    try:
                        value = IdentityCatalog(owner, field, [item for item in values if item is not None]).resolve(handle)
                    except Exception:
                        raise CaptureError("filter identity not found") from None
                    query = query.filter(LoggingAttemptDetail.attribution[field].as_string() == str(value))
            if operation:
                values = [item[0] for item in db.query(LoggingAttemptDetail.operation_id).filter_by(owner=owner).distinct().limit(10001)]
                try:
                    raw_operation = IdentityCatalog(owner, "operation_id", values).resolve(operation)
                except Exception:
                    raise CaptureError("operation identity not found") from None
                query = query.filter(LoggingAttemptDetail.operation_id == raw_operation)
            for bound, field in ((since, "since"), (until, "until")):
                if bound:
                    try:
                        moment = datetime.fromisoformat(bound.replace("Z", "+00:00"))
                        if moment.tzinfo:
                            from datetime import timezone
                            moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
                    except (ValueError, AttributeError):
                        raise CaptureError("invalid date bound") from None
                    query = query.filter(LoggingAttemptDetail.created_at >= moment if field == "since" else LoggingAttemptDetail.created_at < moment)
            if session:
                # Match the public owner-bound handle without exposing raw IDs;
                # the SQL bound is on an indexed session key carried in attribution.
                from core.database import Session as ChatSession
                sessions = db.query(ChatSession.id).filter_by(owner=owner).limit(10001).all()
                raw = next((value for (value,) in sessions if identity_handle(owner, "session_id", value) == session), None)
                if raw is None:
                    raise CaptureError("session not found")
                query = query.filter(LoggingAttemptDetail.attribution["session_id"].as_string() == str(raw))
            rows = query.order_by(LoggingAttemptDetail.created_at.desc(), LoggingAttemptDetail.id.desc()).offset(offset).limit(limit+1).all()
            items = [{"handle": row.id, "outcome": row.outcome, "capture_state": row.capture_state,
                      "created_at": row.created_at.isoformat()+"Z", "operation_type": row.attribution.get("operation_type"),
                      "operation_handle": identity_handle(owner, "operation_id", row.operation_id),
                      "session_handle": identity_handle(owner, "session_id", row.attribution["session_id"]) if row.attribution.get("session_id") else None,
                      "requested_model": row.attribution.get("requested_model"), "actual_model": row.attribution.get("actual_model"),
                      "loss_reasons": row.loss_reasons} for row in rows[:limit]]
            next_cursor = None
            if len(rows) > limit:
                next_cursor = self._remember({"kind": "attempts", "owner": owner, "filters": filters, "revision": revision, "offset": offset+limit})
            return {"schema": "open-clank.logging.v1", "owner_scope": owner_scope(owner), "items": items,
                    "coverage": {"state": "reported", "source_revision": revision}, "next_cursor": next_cursor}
        finally:
            db.close()

    def _remember(self, value):
        with self.lock:
            self.previews = {key: item for key, item in self.previews.items() if item["expires"] > time.monotonic()}
            if len(self.previews) >= 10000:
                self.previews.pop(next(iter(self.previews)))
            token = secrets.token_urlsafe(32)
            self.previews[token] = {**value, "expires": time.monotonic()+600}
            return token

    def prune(self, *, owner, action, payload, **_bounds):
        target = payload.get("target")
        field = {"advanced_bodies": "body_retention", "advanced_metadata": "metadata_retention"}.get(target)
        if not field:
            raise CaptureError("unknown pruning target")
        db = self.session_factory()
        try:
            revision = self._revision(db, owner)
            if action == "preview":
                from src.openclank.logging_policy import normalize
                policy = normalize({field: payload.get("retention", {"mode": "age", "days": 1})})
                preview = self._prune_selected(db, owner, policy, dry_run=True)
                token = self._remember({"kind": "prune", "owner": owner, "target": target, "field": field,
                                        "rule": policy[field], "revision": revision})
                return {"schema": "open-clank.logging.v1", "owner_scope": owner_scope(owner),
                        "preview": {**preview, "target": target, "retention": policy[field], "source_revision": revision}, "preview_id": token}
            entry = self.validate_preview(owner, field, None, payload.get("preview_id"), db=db)
            from src.openclank.logging_policy import defaults
            policy = defaults()
            policy[field] = entry["rule"]
            result = self._prune_selected(db, owner, policy)
            db.commit()
            with self.lock:
                self.previews.pop(payload["preview_id"], None)
            return {"schema": "open-clank.logging.v1", "applied": result}
        finally:
            db.close()

    def validate_preview(self, owner, field, rule, token, *, db=None):
        with self.lock:
            entry = self.previews.get(token)
        if not entry or entry["kind"] != "prune" or entry["owner"] != owner or entry["field"] != field or entry["expires"] < time.monotonic() or (rule is not None and entry["rule"] != rule):
            raise CaptureError("retention preview required or expired")
        own_db = db is None
        db = db or self.session_factory()
        try:
            if entry["revision"] != self._revision(db, owner):
                raise CaptureError("retention preview stale; preview again")
            return entry
        finally:
            if own_db:
                db.close()

    def _prune_selected(self, db, owner, policy, *, dry_run=False):
        eligible = db.query(LoggingAttemptDetail).filter_by(owner=owner).filter(LoggingAttemptDetail.capture_state != "pending")
        candidates = {}
        for field, label in (("body_retention", "bodies"), ("metadata_retention", "metadata")):
            rule = policy[field]
            query = eligible
            if label == "bodies":
                body_ids = db.query(LoggingCaptureBody.attempt_detail_id).filter_by(owner=owner)
                event_ids = db.query(LoggingCaptureEvent.attempt_detail_id).filter_by(owner=owner)
                query = query.filter(LoggingAttemptDetail.id.in_(body_ids.union(event_ids)))
            if rule["mode"] == "age":
                query = query.filter(LoggingAttemptDetail.created_at < utcnow_naive()-timedelta(days=rule["days"]))
            candidates[label] = query.order_by(LoggingAttemptDetail.created_at, LoggingAttemptDetail.id).limit(1001).all() if rule["mode"] != "keep_until_deleted" else []
        rows = list({row.id: row for values in candidates.values() for row in values[:1000]}.values())
        ids = [r.id for r in rows]
        content = {}
        for model in (LoggingCaptureBody, LoggingCaptureEvent):
            for key, total in db.query(model.attempt_detail_id, func.sum(model.size_bytes)).filter(model.owner == owner, model.attempt_detail_id.in_(ids)).group_by(model.attempt_detail_id):
                content[key] = content.get(key, 0)+int(total or 0)
        # Global owner totals include every retained row, even beyond the bounded
        # deletion page. Inflight evidence remains protected and is reported.
        global_content = sum(int(db.query(func.coalesce(func.sum(model.size_bytes), 0)).filter_by(owner=owner).scalar()) for model in (LoggingCaptureBody, LoggingCaptureEvent))
        estimate = func.length(cast(LoggingAttemptDetail.attribution, String))+func.length(cast(LoggingAttemptDetail.timing, String))+512
        global_metadata = int(db.query(func.coalesce(func.sum(estimate), 0)).filter(LoggingAttemptDetail.owner == owner).scalar())
        selected = {"bodies": set(), "metadata": set()}
        remaining = {"bodies": global_content, "metadata": global_metadata}
        for field, label in (("body_retention", "bodies"), ("metadata_retention", "metadata")):
            rule = policy[field]
            for row in candidates[label][:1000]:
                amount = content.get(row.id, 0) if label == "bodies" else len(json.dumps(row.attribution))+len(json.dumps(row.timing))+512
                expired = rule["mode"] == "age" and row.created_at < utcnow_naive()-timedelta(days=rule["days"])
                oversize = rule["mode"] == "size" and remaining[label] > rule["max_bytes"]
                if expired or oversize:
                    selected[label].add(row.id)
                    remaining[label] -= amount
        selected_ids = selected["bodies"] | selected["metadata"]
        byte_count = sum(content.get(key, 0) for key in selected_ids)
        body_records = db.query(LoggingCaptureBody).filter(LoggingCaptureBody.owner == owner, LoggingCaptureBody.attempt_detail_id.in_(selected_ids)).count() if selected_ids else 0
        event_records = db.query(LoggingCaptureEvent).filter(LoggingCaptureEvent.owner == owner, LoggingCaptureEvent.attempt_detail_id.in_(selected_ids)).count() if selected_ids else 0
        if not dry_run and selected_ids:
            # Record only categories actually erased, never infer old policy.
            for row in candidates["bodies"]:
                if row.id not in selected["bodies"] or row.id in selected["metadata"]:
                    continue
                categories = {category for (category,) in db.query(LoggingCaptureBody.category).filter_by(owner=owner, attempt_detail_id=row.id)}
                if db.query(LoggingCaptureEvent.id).filter_by(owner=owner, attempt_detail_id=row.id).first() is not None:
                    categories.add("events")
                row.attribution = {**row.attribution, "content_pruned_categories": sorted(set(row.attribution.get("content_pruned_categories") or []) | categories)}
            for model in (LoggingCaptureBody, LoggingCaptureEvent):
                db.query(model).filter(model.owner == owner, model.attempt_detail_id.in_(selected_ids)).delete(synchronize_session=False)
            if selected["metadata"]:
                db.query(LoggingAttemptDetail).filter(LoggingAttemptDetail.owner == owner, LoggingAttemptDetail.id.in_(selected["metadata"])).delete(synchronize_session=False)
        return {"body_records": body_records, "event_records": event_records, "body_attempts": len(selected_ids), "body_bytes": byte_count, "metadata_records": len(selected["metadata"]),
                "owner_body_bytes": global_content, "owner_metadata_bytes_estimated": global_metadata,
                "bounded_batch_limit": 1000, "more_maintenance_required": any((policy[field]["mode"] == "age" and len(candidates[label]) > 1000) or (policy[field]["mode"] == "size" and remaining[label] > policy[field]["max_bytes"]) for field, label in (("body_retention", "bodies"), ("metadata_retention", "metadata"))),
                "protected_authorities": ["stats_events", "conversation_archive", "saved_chat", "lore", "semantic_memory", "inflight_captures"],
                "metadata_size_method": "estimated_serialized_metadata", "body_size_method": "uncompressed_bodies_and_serialized_events"}


CAPTURE = LoggingCaptureStore()
