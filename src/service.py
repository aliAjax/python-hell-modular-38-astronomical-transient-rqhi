from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .repository import utcnow
from .rules import RuleEngine, authorize_station


BATCH_ACTIONS = ("submit", "retry")
TERMINAL_ITEM_STATUSES = ("merged", "rejected")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def _bind_batch_station(self, actor, payload):
        declared = payload.get("station_id")
        if actor.station_id and declared and actor.station_id != declared:
            raise ValidationError("X-Station-Id does not match batch station_id")
        if actor.role == "operator":
            if not actor.station_id:
                raise ValidationError("observer must present X-Station-Id")
            payload["station_id"] = actor.station_id
        elif not payload.get("station_id"):
            raise ValidationError("station_id is required")

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        if kind == "measurement_batch":
            self._bind_batch_station(actor, payload)
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        payload = dict(data or {})
        if kind == "candidate" and action == "merge_measurement":
            payload["station_id"] = actor.station_id or payload.get("station_id")
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, payload, self._lookup
        )
        if kind == "measurement_batch" and action in BATCH_ACTIONS:
            return self._process_batch(actor, entity, action)
        merged = dict(entity["data"])
        merged.update(patch)
        if action == "merge_measurement" and merged.get("priority_recalculated"):
            merged["priority_recalculated_at"] = utcnow()
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

    def _process_batch(self, actor, batch, action):
        """Merge buffered measurements one by one.

        Every item keeps an outcome: merged measurements stay merged, invalid
        ones are rejected permanently, and items that lost the optimistic-lock
        race remain blocked so the batch can be retried without replaying work.
        """
        station_id = batch["data"].get("station_id")
        authorize_station(actor, station_id, self._lookup, require=True)
        candidate_id = batch["data"].get("candidate_id")
        candidate = self.repository.get_entity(candidate_id)
        items = [dict(item) for item in batch["data"].get("measurements", [])]
        counts = {"merged": 0, "blocked": 0, "rejected": 0, "pending": 0}

        for item in items:
            if item.get("status") in TERMINAL_ITEM_STATUSES:
                counts[item["status"]] += 1
                continue
            item.pop("error", None)
            if not candidate:
                item.update(status="rejected", error="candidate does not exist")
                counts["rejected"] += 1
                continue
            measurement = {key: item[key] for key in ("observed_at", "ra", "dec", "magnitude")}
            request = {"measurement": measurement, "station_id": item.get("station_id")}
            try:
                _, patch = self.rules.validate_transition(
                    actor, candidate, "merge_measurement", request, self._lookup
                )
            except ConflictError:
                # Another station/batch already merged this station timestamp:
                # the first write wins and replay becomes a no-op.
                item.update(status="merged", merged_at=utcnow(), deduped=True)
                counts["merged"] += 1
                continue
            except (ValidationError, InvalidTransition, PermissionDenied) as exc:
                item.update(status="rejected", error=str(exc))
                counts["rejected"] += 1
                continue
            candidate_data = dict(candidate["data"])
            candidate_data.update(patch)
            if patch.get("priority_recalculated"):
                candidate_data["priority_recalculated_at"] = utcnow()
            try:
                candidate = self.repository.update_entity(
                    candidate_id, candidate["version"], "triaged", candidate_data
                )
            except ConflictError as exc:
                item.update(status="blocked", error=str(exc))
                counts["blocked"] += 1
                # Reload so later items merge against the winning state instead
                # of cascading into spurious version conflicts.
                candidate = self.repository.get_entity(candidate_id)
                continue
            item.update(status="merged", merged_at=utcnow())
            counts["merged"] += 1
            self.audit.record(
                candidate_id,
                actor,
                "merge_measurement",
                "triaged",
                "triaged",
                {
                    "from_batch": batch["id"],
                    "observed_at": item["observed_at"],
                    "station_id": item.get("station_id"),
                    "priority_recalculated": patch.get("priority_recalculated"),
                },
            )

        all_terminal = all(item.get("status") in TERMINAL_ITEM_STATUSES for item in items)
        final_status = "completed" if all_terminal else "partial"
        data = dict(batch["data"])
        data.update(
            {
                "measurements": items,
                "merged_count": counts["merged"],
                "blocked_count": counts["blocked"],
                "rejected_count": counts["rejected"],
                "submitted_at": batch["data"].get("submitted_at") or utcnow(),
            }
        )
        updated = self.repository.update_entity(batch["id"], batch["version"], final_status, data)
        self.audit.record(
            batch["id"],
            actor,
            action,
            batch["status"],
            final_status,
            {"counts": counts},
        )
        return updated

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
