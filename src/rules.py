from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError, utcnow


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def calculate_priority(magnitude, transient_type):
    """Return a deterministic observing priority from 0 to 100."""
    try:
        mag = float(magnitude)
    except (TypeError, ValueError):
        raise ValidationError("magnitude must be numeric")
    if mag < -30 or mag > 40:
        raise ValidationError("magnitude is outside the supported range")
    type_weights = {
        "grb": 30,
        "supernova": 22,
        "tde": 20,
        "variable": 8,
        "unknown": 12,
    }
    score = max(0.0, 100.0 - mag * 4.0) + type_weights.get(transient_type, 5)
    return round(min(100.0, score), 2)


def measurements_overlap(first_start, first_end, second_start, second_end):
    return str(first_start) < str(second_end) and str(second_start) < str(first_end)


def _ensure_station(actor, measurement_station_id):
    """Reject a measurement that claims a station the actor does not belong to.

    Admins may submit on behalf of any station. Actors without a station
    affiliation (e.g. legacy callers) are not blocked, but once both sides
    declare a station they must agree.
    """
    if getattr(actor, "role", None) == "admin":
        return
    actor_station = getattr(actor, "station_id", None)
    if actor_station is None:
        return
    if measurement_station_id is not None and measurement_station_id != actor_station:
        raise PermissionDenied(
            "cannot submit measurements for station %s; actor belongs to %s"
            % (measurement_station_id, actor_station)
        )


def _normalize_measurement(measurement, actor):
    """Validate one measurement dict and return a cleaned copy, or raise."""
    if not isinstance(measurement, dict):
        raise ValidationError("measurement must be an object")
    try:
        ra = float(measurement.get("ra"))
        dec = float(measurement.get("dec"))
        magnitude = float(measurement.get("magnitude"))
    except (TypeError, ValueError):
        raise ValidationError("measurement coordinates and magnitude must be numeric")
    if ra < 0 or ra >= 360 or dec < -90 or dec > 90:
        raise ValidationError("measurement coordinates are outside valid ranges")
    observed_at = str(measurement.get("observed_at", "")).strip()
    if not observed_at:
        raise ValidationError("measurement observed_at is required")
    station_id = measurement.get("station_id") or getattr(actor, "station_id", None)
    _ensure_station(actor, station_id)
    return {
        "observed_at": observed_at,
        "ra": ra,
        "dec": dec,
        "magnitude": magnitude,
        "station_id": station_id,
    }


def _validate_source(actor, data, lookup):
    if len(str(data.get("name", "")).strip()) < 2:
        raise ValidationError("source name is required")
    return {}


def _validate_candidate(actor, data, lookup):
    source_id = data.get("source_id")
    if not _find_one(lookup, "source", "id", source_id):
        raise ValidationError("source does not exist")
    try:
        ra = float(data.get("ra"))
        dec = float(data.get("dec"))
    except (TypeError, ValueError):
        raise ValidationError("ra and dec must be numeric")
    if ra < 0 or ra >= 360 or dec < -90 or dec > 90:
        raise ValidationError("coordinates are outside valid ranges")
    event_key = "%s:%s" % (source_id, data.get("event_id"))
    duplicate = _find_one(lookup, "candidate", "event_key", event_key)
    if duplicate:
        raise ConflictError("candidate already exists for event key " + event_key)
    observed_at = str(data.get("observed_at", "")).strip()
    station_id = data.get("station_id") or getattr(actor, "station_id", None)
    return {
        "event_key": event_key,
        "measurements": [
            {
                "observed_at": observed_at,
                "ra": ra,
                "dec": dec,
                "magnitude": float(data["magnitude"]),
                "station_id": station_id,
            }
        ],
        "priority_score": calculate_priority(data.get("magnitude"), data.get("transient_type")),
    }


def _validate_telescope(actor, data, lookup):
    if not (0 < float(data.get("aperture_m", 0))):
        raise ValidationError("aperture_m must be positive")
    return {}


def _validate_observation(actor, data, lookup):
    if not _find_one(lookup, "candidate", "id", data.get("candidate_id")):
        raise ValidationError("candidate does not exist")
    if not _find_one(lookup, "telescope", "id", data.get("telescope_id")):
        raise ValidationError("telescope does not exist")
    if str(data.get("start_at")) >= str(data.get("end_at")):
        raise ValidationError("observation end must be after start")
    return {"scheduled_team": data.get("team_id")}


def _validate_merge_measurement(actor, entity, data, lookup):
    cleaned = _normalize_measurement(data.get("measurement"), actor)
    existing = list(entity["data"].get("measurements") or [])
    if any(item.get("observed_at") == cleaned["observed_at"] for item in existing):
        raise ConflictError("measurement timestamp already merged")
    merged = existing + [cleaned]
    latest = sorted(merged, key=lambda item: item["observed_at"])[-1]
    return {
        "measurements": merged,
        "merged_measurement_count": len(merged),
        "latest_magnitude": latest["magnitude"],
        "priority_score": calculate_priority(latest["magnitude"], entity["data"].get("transient_type")),
    }


def _validate_merge_batch(actor, entity, data, lookup):
    """Merge a batch of offline-accumulated measurements.

    Valid measurements are appended; invalid ones are retained in
    ``pending_measurements`` with a reason so they can be retried later.
    """
    raw = data.get("measurements")
    if not isinstance(raw, list) or not raw:
        raise ValidationError("measurements must be a non-empty list")
    batch_id = str(data.get("batch_id", "")).strip() or None
    existing = list(entity["data"].get("measurements") or [])
    pending = list(entity["data"].get("pending_measurements") or [])
    processed = dict(entity["data"].get("processed_batches") or {})

    if batch_id and batch_id in processed:
        stored = processed[batch_id]
        return {
            "batch_result": stored,
            "processed_batches": processed,
            "pending_measurements": pending,
        }

    merged = list(existing)
    rejected = []
    seen = {item.get("observed_at") for item in existing}
    for index, measurement in enumerate(raw):
        try:
            cleaned = _normalize_measurement(measurement, actor)
        except ValidationError as exc:
            rejected.append({"index": index, "measurement": measurement, "reason": str(exc), "code": "invalid"})
            continue
        except PermissionDenied as exc:
            rejected.append({"index": index, "measurement": measurement, "reason": str(exc), "code": "permission_denied"})
            continue
        if cleaned["observed_at"] in seen:
            rejected.append({"index": index, "measurement": measurement, "reason": "measurement timestamp already merged", "code": "duplicate"})
            continue
        merged.append(cleaned)
        seen.add(cleaned["observed_at"])

    for item in rejected:
        pending.append(
            {
                "measurement": item["measurement"],
                "reason": item["reason"],
                "code": item["code"],
                "attempts": 1,
                "station_id": (item["measurement"] if isinstance(item["measurement"], dict) else {}).get("station_id") or getattr(actor, "station_id", None),
            }
        )

    result = {
        "merged_count": len(merged) - len(existing),
        "rejected_count": len(rejected),
        "rejected": rejected,
        "pending_count": len(pending),
    }
    if batch_id:
        processed[batch_id] = result
        # keep the registry bounded
        if len(processed) > 100:
            oldest = sorted(processed.items(), key=lambda kv: kv[1].get("processed_at", ""))[:-100]
            for key, _ in oldest:
                processed.pop(key, None)
        result["processed_at"] = utcnow()

    patch = {
        "measurements": merged,
        "merged_measurement_count": len(merged),
        "pending_measurements": pending,
        "pending_count": len(pending),
        "batch_result": result,
    }
    if batch_id:
        patch["processed_batches"] = processed
    if merged:
        latest = sorted(merged, key=lambda item: item["observed_at"])[-1]
        patch["latest_magnitude"] = latest["magnitude"]
        patch["priority_score"] = calculate_priority(latest["magnitude"], entity["data"].get("transient_type"))
    return patch


def _validate_retry_pending(actor, entity, data, lookup):
    """Re-attempt pending measurements; keep the ones that still fail.

    Pending items whose timestamp is already merged (e.g. merged by a
    concurrent batch) are discarded automatically.
    """
    pending = list(entity["data"].get("pending_measurements") or [])
    if not pending:
        raise ValidationError("no pending measurements to retry")
    existing = list(entity["data"].get("measurements") or [])
    merged = list(existing)
    remaining = []
    newly_merged = 0
    discarded = 0
    seen = {item.get("observed_at") for item in merged}
    for item in pending:
        measurement = item.get("measurement")
        attempts = int(item.get("attempts", 0)) + 1
        # If the timestamp was already merged (e.g. corrected by a later batch),
        # discard the stale pending copy regardless of its validity.
        observed_at = str(measurement.get("observed_at", "")).strip() if isinstance(measurement, dict) else ""
        if observed_at and observed_at in seen:
            discarded += 1
            continue
        try:
            cleaned = _normalize_measurement(measurement, actor)
        except (ValidationError, PermissionDenied) as exc:
            remaining.append({**item, "reason": str(exc), "attempts": attempts})
            continue
        merged.append(cleaned)
        seen.add(cleaned["observed_at"])
        newly_merged += 1
    patch = {
        "measurements": merged,
        "merged_measurement_count": len(merged),
        "pending_measurements": remaining,
        "pending_count": len(remaining),
        "retry_result": {"newly_merged": newly_merged, "discarded_duplicates": discarded, "remaining": len(remaining)},
    }
    if merged:
        latest = sorted(merged, key=lambda item: item["observed_at"])[-1]
        patch["latest_magnitude"] = latest["magnitude"]
        patch["priority_score"] = calculate_priority(latest["magnitude"], entity["data"].get("transient_type"))
    return patch


def _validate_discard_pending(actor, entity, data, lookup):
    """Explicitly clear pending measurements that will not be retried."""
    pending = list(entity["data"].get("pending_measurements") or [])
    if not pending:
        raise ValidationError("no pending measurements to discard")
    return {
        "pending_measurements": [],
        "pending_count": 0,
        "discarded_pending_count": len(pending),
        "discard_result": {"discarded": len(pending)},
    }


def _validate_recalculate_priority(actor, entity, data, lookup):
    measurements = list(entity["data"].get("measurements") or [])
    if not measurements:
        raise ValidationError("cannot recalculate priority without measurements")
    latest = sorted(measurements, key=lambda item: item["observed_at"])[-1]
    return {
        "latest_magnitude": latest["magnitude"],
        "priority_score": calculate_priority(latest["magnitude"], entity["data"].get("transient_type")),
        "priority_recalculated_at": utcnow(),
    }


def _validate_reclassify(actor, entity, data, lookup):
    if data.get("new_type") not in {"grb", "supernova", "tde", "variable", "unknown"}:
        raise ValidationError("unsupported transient type")
    patch = {"transient_type": data["new_type"], "previous_type": entity["data"].get("transient_type")}
    measurements = list(entity["data"].get("measurements") or [])
    if measurements:
        latest = sorted(measurements, key=lambda item: item["observed_at"])[-1]
        patch["priority_score"] = calculate_priority(latest["magnitude"], data["new_type"])
    return patch


def _validate_correct(actor, entity, data, lookup):
    if not str(data.get("reason", "")).strip():
        raise ValidationError("correction reason is required")
    return {"corrected_by": actor.user_id}


def _validate_withdraw_candidate(actor, entity, data, lookup):
    """Do not silently orphan scheduled observations when a candidate is withdrawn."""
    observations = lookup("observation", "candidate_id", entity["id"]) if lookup else []
    active = [item for item in observations if item["status"] in ("requested", "scheduled")]
    if active:
        raise ConflictError(
            "cannot withdraw candidate %s: %d active observation(s) must be handled first"
            % (entity["id"], len(active))
        )
    return {}


def _validate_schedule(actor, entity, data, lookup):
    observations = lookup("observation", "telescope_id", entity["data"].get("telescope_id")) if lookup else []
    for other in observations:
        if other["id"] == entity["id"] or other["status"] != "scheduled":
            continue
        if measurements_overlap(
            entity["data"].get("start_at"),
            entity["data"].get("end_at"),
            other["data"].get("start_at"),
            other["data"].get("end_at"),
        ):
            raise ConflictError("telescope is already scheduled in this window")
    team_observations = lookup("observation", "team_id", entity["data"].get("team_id")) if lookup else []
    for other in team_observations:
        if other["id"] == entity["id"] or other["status"] != "scheduled":
            continue
        if measurements_overlap(
            entity["data"].get("start_at"),
            entity["data"].get("end_at"),
            other["data"].get("start_at"),
            other["data"].get("end_at"),
        ):
            raise ConflictError("observation team is already committed in this window")
    return {"scheduled_by": actor.user_id}


class RuleEngine:
    ALIASES = {
        "sources": "source",
        "candidates": "candidate",
        "telescopes": "telescope",
        "observations": "observation",
    }
    INITIAL_STATUS = {
        "source": "registered",
        "candidate": "detected",
        "telescope": "available",
        "observation": "requested",
    }
    TRANSITIONS = {
        "source": {
            "activate": (("registered",), "active"),
            "retire": (("active",), "retired"),
        },
        "candidate": {
            "merge_measurement": (("detected", "triaged"), "triaged"),
            "merge_batch": (("detected", "triaged"), "triaged"),
            "retry_pending": (("detected", "triaged"), "triaged"),
            "discard_pending": (("detected", "triaged"), "triaged"),
            "recalculate_priority": (("detected", "triaged", "classified"), "triaged"),
            "triage": (("detected",), "triaged"),
            "reclassify": (("triaged",), "triaged"),
            "correct": (("detected", "triaged", "classified"), "triaged"),
            "withdraw": (("detected", "triaged"), "withdrawn"),
            "classify": (("triaged",), "classified"),
        },
        "telescope": {
            "restrict": (("available",), "restricted"),
            "restore": (("restricted",), "available"),
        },
        "observation": {
            "schedule": (("requested",), "scheduled"),
            "complete": (("scheduled",), "completed"),
            "withdraw": (("requested", "scheduled"), "withdrawn"),
            "correct": (("requested", "scheduled"), "requested"),
        },
    }
    CREATE_REQUIRED = {
        "source": ("name", "survey_name"),
        "candidate": ("source_id", "event_id", "ra", "dec", "magnitude", "transient_type", "observed_at"),
        "telescope": ("name", "aperture_m", "site_name"),
        "observation": ("candidate_id", "telescope_id", "team_id", "start_at", "end_at", "mode"),
    }
    ACTION_REQUIRED = {
        ("source", "retire"): ("reason",),
        ("candidate", "merge_measurement"): ("measurement",),
        ("candidate", "merge_batch"): ("measurements",),
        ("candidate", "retry_pending"): (),
        ("candidate", "discard_pending"): (),
        ("candidate", "recalculate_priority"): (),
        ("candidate", "triage"): ("reason",),
        ("candidate", "reclassify"): ("new_type", "reason"),
        ("candidate", "correct"): ("reason",),
        ("candidate", "withdraw"): ("reason",),
        ("candidate", "classify"): ("classification",),
        ("telescope", "restrict"): ("reason",),
        ("observation", "schedule"): ("operator_id",),
        ("observation", "withdraw"): ("reason",),
        ("observation", "correct"): ("reason",),
    }
    CREATE_ROLES = {
        "source": ("analyst", "admin"),
        "candidate": ("analyst", "operator", "admin"),
        "telescope": ("coordinator", "admin"),
        "observation": ("analyst", "coordinator", "admin"),
    }
    ROLE_ACTIONS = {
        "activate": ("coordinator", "admin"),
        "retire": ("coordinator", "admin"),
        "merge_measurement": ("analyst", "operator", "admin"),
        "merge_batch": ("analyst", "operator", "admin"),
        "retry_pending": ("analyst", "operator", "admin"),
        "discard_pending": ("analyst", "operator", "admin"),
        "recalculate_priority": ("analyst", "supervisor", "admin"),
        "triage": ("analyst", "operator", "admin"),
        "reclassify": ("analyst", "supervisor", "admin"),
        "correct": ("analyst", "supervisor", "admin"),
        "withdraw": ("analyst", "supervisor", "admin"),
        "classify": ("analyst", "supervisor", "admin"),
        "restrict": ("coordinator", "admin"),
        "restore": ("coordinator", "admin"),
        "schedule": ("coordinator", "admin"),
        "complete": ("operator", "coordinator", "admin"),
    }
    CUSTOM_CREATE = {
        "source": _validate_source,
        "candidate": _validate_candidate,
        "telescope": _validate_telescope,
        "observation": _validate_observation,
    }
    CUSTOM_TRANSITIONS = {
        ("candidate", "merge_measurement"): _validate_merge_measurement,
        ("candidate", "merge_batch"): _validate_merge_batch,
        ("candidate", "retry_pending"): _validate_retry_pending,
        ("candidate", "discard_pending"): _validate_discard_pending,
        ("candidate", "recalculate_priority"): _validate_recalculate_priority,
        ("candidate", "reclassify"): _validate_reclassify,
        ("candidate", "correct"): _validate_correct,
        ("candidate", "withdraw"): _validate_withdraw_candidate,
        ("observation", "schedule"): _validate_schedule,
        ("observation", "correct"): _validate_correct,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        return custom(actor, data, lookup) if custom else {}

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed_roles = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
