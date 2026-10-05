import json
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    DomainError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .rules import RuleEngine

OFFLINE_KINDS = ("inspection", "alarm")
# Inbox states that already carry an answer: retransmission replays the first
# result instead of merging the record a second time.  "conflict" stays open
# for an admin decision but is still a finished answer for the device.
TERMINAL_INBOX = ("merged", "conflict", "resolved")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "sync_conflict" and action == "resolve":
            return self._resolve_conflict(actor, entity, dict(data or {}))
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    # ------------------------------------------------------------------
    # Offline merge
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_dump(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    def _same_payload(self, left, right):
        return self._canonical_dump(left or {}) == self._canonical_dump(right or {})

    def _parse_record(self, raw):
        if not isinstance(raw, dict):
            raise ValidationError("each offline record must be an object")
        record_id = str(raw.get("record_id", "")).strip()
        if not record_id:
            raise ValidationError("record_id is required")
        kind = self.rules.normalize_kind(str(raw.get("kind", "")).strip())
        if kind not in OFFLINE_KINDS:
            raise ValidationError("offline merge only supports inspection or alarm records")
        field_status = raw.get("status")
        if not field_status:
            raise ValidationError("status is required")
        field_data = raw.get("data")
        if not isinstance(field_data, dict) or not field_data:
            raise ValidationError("data must be a non-empty object")
        base = raw.get("base")
        if base is not None and not isinstance(base, dict):
            raise ValidationError("base must be an object")
        return {
            "record_id": record_id,
            "kind": kind,
            "entity_id": str(raw.get("entity_id", "")).strip() or record_id,
            "status": field_status,
            "data": dict(field_data),
            "base": base,
            "source_id": str(raw.get("source_id", "")).strip(),
        }

    def _action_for(self, kind, from_status, to_status):
        for action, (allowed, target) in self.rules.TRANSITIONS.get(kind, {}).items():
            if to_status == target and from_status in allowed:
                return action
        return None

    def _field_action_data(self, record):
        data = dict(record["data"])
        for field in ("code", "equipment_id", "occurred_at", "scheduled_at", "cycle_days"):
            data.pop(field, None)
        return data

    def _new_entity(self, conn, actor, record):
        """Create an offline-only inspection/alarm, advancing straight to its status."""
        initial = self.rules.INITIAL_STATUS[record["kind"]]
        create_data = dict(record["data"])
        self.rules.validate_create(actor, record["kind"], create_data, self._lookup)
        entity = self.repository.insert_entity(
            conn, record["entity_id"], record["kind"], initial, create_data, actor.user_id
        )
        if record["status"] != initial:
            action = self._action_for(record["kind"], initial, record["status"])
            if not action:
                raise InvalidTransition(
                    "offline record status %s is not reachable from %s"
                    % (record["status"], initial)
                )
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, self._field_action_data(record), self._lookup
            )
            merged = dict(entity["data"])
            merged.update(patch)
            entity = self.repository.apply_entity(conn, entity["id"], next_status, merged)
            self.repository.append_audit_conn(
                conn, entity["id"], actor.user_id, actor.role,
                "offline_" + action, initial, next_status,
                {"record_id": record["record_id"], "source": "offline_new"},
            )
        else:
            self.repository.append_audit_conn(
                conn, entity["id"], actor.user_id, actor.role,
                "offline_create", None, initial,
                {"record_id": record["record_id"], "source": "offline_new"},
            )
        return entity

    def _open_conflict(self, conn, actor, entity, record, base):
        conflict_id = "conflict-" + uuid4().hex[:16]
        payload = {
            "entity_id": entity["id"],
            "kind": entity["kind"],
            "record_id": record["record_id"],
            "server_version": entity["version"],
            "server_status": entity["status"],
            "server_data": entity["data"],
            "field_status": record["status"],
            "field_data": record["data"],
            "base_version": base.get("version"),
            "base_status": base.get("status"),
            "base_data": base.get("data", {}),
            "source_id": record.get("source_id", ""),
        }
        conflict = self.repository.insert_entity(
            conn, conflict_id, "sync_conflict", "open", payload, "system"
        )
        self.repository.append_audit_conn(
            conn, conflict_id, actor.user_id, actor.role,
            "conflict_detected", None, "open",
            {"entity_id": entity["id"], "record_id": record["record_id"]},
        )
        return conflict

    def _merge_into(self, conn, actor, entity, record):
        """Fast-forward an existing entity to the field version (only field changed)."""
        from_status = entity["status"]
        merged_data = dict(entity["data"])
        merged_data.update(record["data"])
        if entity["status"] != record["status"]:
            action = self._action_for(entity["kind"], entity["status"], record["status"])
            if not action:
                raise InvalidTransition(
                    "offline record status %s is not reachable from %s"
                    % (record["status"], entity["status"])
                )
            next_status, patch = self.rules.validate_transition(
                actor, entity, action, self._field_action_data(record), self._lookup
            )
            merged_data = dict(entity["data"])
            merged_data.update(record["data"])
            merged_data.update(patch)
        else:
            action = "offline_update"
            next_status = entity["status"]
        updated = self.repository.apply_entity(conn, entity["id"], next_status, merged_data)
        self.repository.append_audit_conn(
            conn, entity["id"], actor.user_id, actor.role,
            action if action.startswith("offline_") else "offline_" + action,
            from_status, next_status,
            {"record_id": record["record_id"], "source": "offline_fast_forward"},
        )
        return updated

    def _attempt_record(self, conn, actor, record):
        """Try one record inside an open transaction.

        Returns (outcome, entity_or_none, conflict_or_none).
        Outcome is one of: created, updated, noop, conflict.
        Raises DomainError when the record cannot be merged yet (retryable).
        """
        entity = self.repository._row_to_entity(conn, record["entity_id"])
        base = record["base"]
        if entity is None:
            if base and base.get("version"):
                # Field record was based on an entity the server no longer has.
                raise ConflictError("base entity missing: " + record["entity_id"])
            created = self._new_entity(conn, actor, record)
            return "created", created, None

        if base is None:
            # No sync anchor: treat current server state as the common base.
            base = {"version": entity["version"], "status": entity["status"], "data": entity["data"]}

        field_changed = (
            str(base.get("status", "")) != str(record["status"])
            or not self._same_payload(base.get("data", {}), record["data"])
        )
        server_changed = (
            int(entity["version"]) != int(base.get("version", entity["version"]))
            or str(base.get("status", entity["status"])) != str(entity["status"])
            or not self._same_payload(base.get("data", entity["data"]), entity["data"])
        )

        if not field_changed:
            return "noop", entity, None
        if server_changed:
            # Both sides edited the same record: keep both copies, do not touch
            # the entity (and therefore the equipment state) until an admin decides.
            conflict = self._open_conflict(conn, actor, entity, record, base)
            return "conflict", entity, conflict

        updated = self._merge_into(conn, actor, entity, record)
        return "updated", updated, None

    def _run_attempt(self, actor, record):
        """Open a transaction, attempt the record, persist the inbox outcome."""
        with self.repository.transaction() as conn:
            outcome, entity, conflict = self._attempt_record(conn, actor, record)
            inbox_status = "conflict" if outcome == "conflict" else "merged"
            result = {
                "record_id": record["record_id"],
                "outcome": outcome,
                "entity_id": entity["id"],
                "entity_status": entity["status"],
            }
            if conflict:
                result["conflict_id"] = conflict["id"]
            self.repository.update_inbox(
                conn, record["record_id"], inbox_status, entity_id=entity["id"], result=result
            )
        return result

    def _ingest_record(self, actor, raw):
        """Idempotent ingest of one record inside a single serialized write tx.

        BEGIN IMMEDIATE makes concurrent uploads of the same record queue behind
        each other; losers replay the winner's first result instead of creating
        a second copy.  A failed attempt rolls entity writes back to a savepoint
        while the inbox row stays behind for the next reconnect/retry.
        """
        record = self._parse_record(raw)
        with self.repository.transaction() as conn:
            self.repository.insert_inbox(
                conn, record["record_id"], record["kind"], None,
                "pending", raw, actor.user_id,
            )
            inbox = self.repository.get_inbox_conn(conn, record["record_id"])
            if inbox["status"] in TERMINAL_INBOX and inbox.get("result"):
                return inbox["result"]

            conn.execute("SAVEPOINT record_attempt")
            try:
                outcome, entity, conflict = self._attempt_record(conn, actor, record)
            except DomainError as exc:
                conn.execute("ROLLBACK TO SAVEPOINT record_attempt")
                self.repository.update_inbox(
                    conn, record["record_id"], "failed",
                    entity_id=record["entity_id"], error=str(exc),
                )
                return {
                    "record_id": record["record_id"],
                    "outcome": "failed",
                    "entity_id": record["entity_id"],
                    "error": str(exc),
                }
            result = {
                "record_id": record["record_id"],
                "outcome": outcome,
                "entity_id": entity["id"],
                "entity_status": entity["status"],
            }
            if conflict:
                result["conflict_id"] = conflict["id"]
            self.repository.update_inbox(
                conn,
                record["record_id"],
                "conflict" if conflict else "merged",
                entity_id=entity["id"],
                result=result,
            )
            return result

    def merge_offline(self, actor, records):
        """Merge inspection/alarm records captured while offline.

        Each record number is ingested at most once.  Replays of a finished
        record return the first merge result.  Records that fail to merge stay
        in the inbox and can be retried on reconnect.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        return [self._ingest_record(actor, raw) for raw in records]

    def retry_offline(self, actor, record_ids=None):
        """Retry records that never finished merging (failed/pending)."""
        pending = self.repository.list_inbox()
        selected = {str(value) for value in (record_ids or [])}
        results = []
        for inbox in pending:
            if inbox["status"] in TERMINAL_INBOX:
                if inbox.get("result") and (not selected or inbox["record_id"] in selected):
                    results.append(inbox["result"])
                continue
            if selected and inbox["record_id"] not in selected:
                continue
            record = self._parse_record(inbox["envelope"])
            try:
                results.append(self._run_attempt(actor, record))
            except DomainError as exc:
                with self.repository.transaction() as conn:
                    self.repository.update_inbox(
                        conn, record["record_id"], "failed",
                        entity_id=record["entity_id"], error=str(exc)
                    )
                results.append({
                    "record_id": record["record_id"],
                    "outcome": "failed",
                    "entity_id": record["entity_id"],
                    "error": str(exc),
                })
        return results

    def offline_inbox(self, status=None):
        return self.repository.list_inbox(status=status)

    def _resolve_conflict(self, actor, conflict, data):
        """Admin picks the field or server copy of a diverged record."""
        next_status, patch = self.rules.validate_transition(
            actor, conflict, "resolve", data, self._lookup
        )
        entity_id = conflict["data"]["entity_id"]
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("conflicted entity missing: " + entity_id)
        choice = data["choice"]
        with self.repository.transaction() as conn:
            if choice == "field":
                target_status = conflict["data"]["field_status"]
                target_data = dict(conflict["data"]["field_data"])
                updated = self.repository.apply_entity(
                    conn, entity_id, target_status, target_data
                )
                self.repository.append_audit_conn(
                    conn, entity_id, actor.user_id, actor.role,
                    "resolve_conflict_field", entity["status"], target_status,
                    {"conflict_id": conflict["id"]},
                )
            else:
                updated = entity
                self.repository.append_audit_conn(
                    conn, entity_id, actor.user_id, actor.role,
                    "resolve_conflict_server", entity["status"], entity["status"],
                    {"conflict_id": conflict["id"]},
                )
            merged_data = dict(conflict["data"])
            merged_data.update(patch)
            resolved_conflict = self.repository.apply_entity(
                conn, conflict["id"], next_status, merged_data
            )
            self.repository.append_audit_conn(
                conn, conflict["id"], actor.user_id, actor.role,
                "resolve", "open", next_status,
                {"entity_id": entity_id, "choice": choice},
            )
            self.repository.update_inbox(
                conn, conflict["data"]["record_id"], "resolved",
                entity_id=entity_id,
                result={
                    "record_id": conflict["data"]["record_id"],
                    "outcome": "resolved",
                    "entity_id": entity_id,
                    "entity_status": updated["status"],
                    "conflict_id": conflict["id"],
                    "resolution": choice,
                },
            )
        return resolved_conflict

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
