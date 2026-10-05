import hashlib
import json
from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .rules import RuleEngine


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

    OFFLINE_KINDS = ("inspection", "alarm")
    CONFLICT_STATUS = "conflict"

    def merge_offline(self, actor, records):
        """Merge field-collected inspections and alarms back into the service.

        Each field record is stored once under a stable (source_id, record_id)
        identity. Re-uploads of an already processed record answer with the
        first result. Records modified on both sides are kept as two copies
        and marked as a conflict; the equipment status is left untouched until
        an admin picks a version. Records that fail to merge stay pending and
        are retried on reconnect.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        results = []
        for raw in records:
            results.append(self._merge_one(actor, raw))
        return results

    def list_offline_records(self, status=None):
        return self.repository.list_offline_records(status=status)

    def resolve_conflict(self, actor, entity_id, choice):
        """Admin picks the server or field version of a conflicted record."""
        if actor.role != "admin":
            raise PermissionDenied("only admin can resolve offline conflicts")
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        data = entity.get("data") or {}
        if entity["status"] != self.CONFLICT_STATUS and not data.get("_conflict"):
            raise ConflictError("entity is not marked as a conflict")
        if choice == "server":
            snapshot = data.get("_server_snapshot")
            if not snapshot:
                raise ValidationError("server snapshot is missing")
            target_status = snapshot.get("status")
            target_data = dict(snapshot.get("data") or {})
        elif choice == "field":
            field_copy = data.get("_field_copy")
            if not field_copy:
                raise ValidationError("field copy is missing")
            target_status = field_copy.get("status") or self.rules.initial_status(entity["kind"])
            target_data = dict(field_copy.get("data") or {})
        else:
            raise ValidationError("choice must be 'server' or 'field'")
        target_data.pop("_conflict", None)
        target_data.pop("_field_copy", None)
        target_data.pop("_server_snapshot", None)
        updated = self.repository.update_entity(entity_id, entity["version"], target_status, target_data)
        row = self.repository.get_offline_record_by_entity(entity_id)
        if row:
            row["status"] = "merged"
            row["result"] = self._result(row["source_id"], row["record_id"], row["kind"], "merged", updated)
            self.repository.save_offline_record(row)
        self.audit.record(entity_id, actor, "resolve_offline_conflict", entity["status"], updated["status"], {"choice": choice})
        return updated

    def _merge_one(self, actor, raw):
        if not isinstance(raw, dict):
            return self._merge_failed("", "", "", None, raw, "each offline record must be an object")
        source_id = str(raw.get("source_id", "")).strip()
        record_id = str(raw.get("record_id", "")).strip()
        if not source_id or not record_id:
            return self._merge_failed(source_id, record_id, "", None, raw, "source_id and record_id are required")
        kind = self.rules.normalize_kind(str(raw.get("kind", "")).strip())
        entity_id = "offline-" + hashlib.sha256((source_id + "\0" + record_id).encode("utf-8")).hexdigest()[:32]
        if kind not in self.OFFLINE_KINDS:
            return self._merge_failed(source_id, record_id, kind, entity_id, raw, "offline records must be inspection or alarm")
        data = raw.get("data")
        if not isinstance(data, dict):
            return self._merge_failed(source_id, record_id, kind, entity_id, raw, "data must be an object")
        content_hash = self._content_hash(raw)
        row = self.repository.get_offline_record(source_id, record_id)
        if row and row["status"] in ("merged", "conflict") and row.get("content_hash") == content_hash:
            # Duplicate re-upload of an already processed record: answer with the first result.
            return row["result"]
        try:
            return self._apply_merge(actor, source_id, record_id, kind, data, raw, entity_id, row, content_hash)
        except Exception as exc:
            attempts = (row["attempts"] if row else 0) + 1
            return self._merge_failed(source_id, record_id, kind, entity_id, raw, str(exc),
                                      content_hash=content_hash, base_version=raw.get("base_version"), attempts=attempts)

    def _apply_merge(self, actor, source_id, record_id, kind, data, raw, entity_id, row, content_hash):
        entity = self.repository.get_entity(entity_id)
        base_version = raw.get("base_version")
        base_data = raw.get("base_data")
        base_status = raw.get("base_status")
        field_status = raw.get("status")
        attempts = (row["attempts"] if row else 0) + 1

        if entity is None:
            payload = dict(data)
            self.rules.validate_create(actor, kind, payload, self._lookup)
            status = self.rules.initial_status(kind, payload)
            entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
            entity = self._apply_field_state(actor, entity, field_status, data)
            result = self._result(source_id, record_id, kind, "merged", entity)
            self._persist(source_id, record_id, kind, entity_id, raw, content_hash, "merged", result,
                          base_version=base_version, attempts=attempts)
            self.audit.record(entity_id, actor, "merge_offline", None, entity["status"],
                              {"source_id": source_id, "record_id": record_id, "kind": kind})
            return result

        if entity["kind"] != kind:
            raise ConflictError("offline id collision: %s is a %s, not %s" % (entity_id, entity["kind"], kind))

        if entity["status"] == self.CONFLICT_STATUS or (entity.get("data") or {}).get("_conflict"):
            # Already in conflict: keep the latest field copy and stay conflicted until admin picks.
            updated = self._refresh_conflict(entity, data, field_status)
            result = self._result(source_id, record_id, kind, "conflict", updated)
            self._persist(source_id, record_id, kind, entity_id, raw, content_hash, "conflict", result,
                          base_version=base_version, attempts=attempts)
            return result

        server_changed = base_version is not None and int(base_version) != int(entity["version"])
        field_changed = True
        if base_data is not None:
            field_changed = (data != base_data) or (field_status is not None and field_status != base_status)
        if server_changed and field_changed:
            field_copy = {"data": data, "status": field_status, "recorded_at": raw.get("recorded_at")}
            snapshot = {"data": entity["data"], "status": entity["status"], "version": entity["version"]}
            updated_data = dict(entity["data"])
            updated_data["_conflict"] = True
            updated_data["_field_copy"] = field_copy
            updated_data["_server_snapshot"] = snapshot
            updated = self.repository.update_entity(entity_id, entity["version"], self.CONFLICT_STATUS, updated_data)
            result = self._result(source_id, record_id, kind, "conflict", updated)
            self._persist(source_id, record_id, kind, entity_id, raw, content_hash, "conflict", result,
                          base_version=base_version, attempts=attempts)
            self.audit.record(entity_id, actor, "merge_offline", entity["status"], self.CONFLICT_STATUS,
                              {"source_id": source_id, "record_id": record_id, "kind": kind, "conflict": True})
            return result

        before_status = entity["status"]
        if field_changed:
            merged_data = dict(entity["data"])
            merged_data.update(data)
            merged_data.pop("_conflict", None)
            merged_data.pop("_field_copy", None)
            merged_data.pop("_server_snapshot", None)
            entity = self.repository.update_entity(entity_id, entity["version"], entity["status"], merged_data)
            entity = self._apply_field_state(actor, entity, field_status, data)
        result = self._result(source_id, record_id, kind, "merged", entity)
        self._persist(source_id, record_id, kind, entity_id, raw, content_hash, "merged", result,
                       base_version=base_version, attempts=attempts)
        self.audit.record(entity_id, actor, "merge_offline", before_status, entity["status"],
                          {"source_id": source_id, "record_id": record_id, "kind": kind})
        return result

    def _refresh_conflict(self, entity, data, field_status):
        updated_data = dict(entity["data"])
        updated_data["_field_copy"] = {"data": data, "status": field_status, "recorded_at": None}
        return self.repository.update_entity(entity["id"], entity["version"], self.CONFLICT_STATUS, updated_data)

    def _apply_field_state(self, actor, entity, field_status, data):
        if not field_status or field_status == entity["status"]:
            return entity
        kind = self.rules.normalize_kind(entity["kind"])
        action = self._action_to_reach(kind, entity["status"], field_status)
        if not action:
            return entity
        try:
            next_status, patch = self.rules.validate_transition(actor, entity, action, dict(data or {}), self._lookup)
        except Exception:
            return entity
        merged = dict(entity["data"])
        merged.update(patch)
        return self.repository.update_entity(entity["id"], entity["version"], next_status, merged)

    def _action_to_reach(self, kind, from_status, to_status):
        for action, (allowed, target) in self.rules.TRANSITIONS.get(kind, {}).items():
            if target == to_status and from_status in allowed:
                return action
        return None

    def _persist(self, source_id, record_id, kind, entity_id, raw, content_hash, status, result,
                 base_version=None, attempts=1):
        self.repository.save_offline_record({
            "source_id": source_id,
            "record_id": record_id,
            "kind": kind,
            "entity_id": entity_id,
            "status": status,
            "payload": raw,
            "result": result,
            "error": None,
            "content_hash": content_hash,
            "base_version": base_version,
            "attempts": attempts,
        })

    def _merge_failed(self, source_id, record_id, kind, entity_id, raw, error,
                      content_hash=None, base_version=None, attempts=1):
        if content_hash is None:
            content_hash = self._content_hash(raw)
        result = self._result(source_id, record_id, kind, "failed", None, error=error)
        self.repository.save_offline_record({
            "source_id": source_id,
            "record_id": record_id,
            "kind": kind,
            "entity_id": entity_id,
            "status": "failed",
            "payload": raw,
            "result": result,
            "error": error,
            "content_hash": content_hash,
            "base_version": base_version,
            "attempts": attempts,
        })
        return result

    @staticmethod
    def _result(source_id, record_id, kind, status, entity, error=None):
        return {
            "source_id": source_id,
            "record_id": record_id,
            "kind": kind,
            "status": status,
            "entity": entity,
            "error": error,
            "duplicate": False,
        }

    @staticmethod
    def _content_hash(raw):
        return hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

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
