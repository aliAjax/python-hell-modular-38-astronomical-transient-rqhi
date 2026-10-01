from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


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


def _measurement_identity(item):
    """Dedup key for a measurement: a station timestamp never collides with another station."""
    return item.get("station_id"), item.get("observed_at")


def authorize_station(actor, station_id, lookup, require):
    """Authorize an actor acting on behalf of a station.

    Observers (operator role) are bound to one registered station and may only
    submit that station's measurements. Analysts merge without a station.
    require=True limits the action to station staff; require=False additionally
    allows non-staff roles but forbids them from impersonating a station.
    """
    station = _find_one(lookup, "station", "id", station_id) if station_id else None
    if actor.role == "admin":
        if station_id and not station:
            raise ValidationError("station does not exist")
        return station
    if actor.role == "operator":
        if not station_id:
            raise PermissionDenied("observer must present X-Station-Id")
        if not station:
            raise ValidationError("station does not exist")
        if actor.user_id not in (station["data"].get("observer_ids") or []):
            raise PermissionDenied("observer is not assigned to station " + str(station_id))
        if station["status"] != "active":
            raise ConflictError("station is not active")
        return station
    if require:
        raise PermissionDenied("role %s is not allowed for a station action" % actor.role)
    if station_id:
        raise PermissionDenied("role %s cannot act on behalf of a station" % actor.role)
    return None


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
    return {
        "event_key": event_key,
        "measurements": [
            {
                "observed_at": data.get("observed_at"),
                "ra": ra,
                "dec": dec,
                "magnitude": float(data["magnitude"]),
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


def _validate_station(actor, data, lookup):
    if len(str(data.get("name", "")).strip()) < 2:
        raise ValidationError("station name is required")
    site = str(data.get("site_name", "")).strip()
    if not site:
        raise ValidationError("station site_name is required")
    observers = data.get("observer_ids", [])
    if observers is None:
        observers = []
    if not isinstance(observers, list) or any(not str(item).strip() for item in observers):
        raise ValidationError("observer_ids must be a list of user ids")
    return {"observer_ids": list(observers)}


def _validate_measurement_batch(actor, data, lookup):
    """Validate a batch of measurements buffered locally while a station was offline."""
    candidate = _find_one(lookup, "candidate", "id", data.get("candidate_id"))
    if not candidate:
        raise ValidationError("candidate does not exist")
    if candidate["status"] not in ("detected", "triaged"):
        raise InvalidTransition("candidate %s does not accept measurements" % candidate["status"])
    authorize_station(actor, data.get("station_id"), lookup, require=True)

    raw_items = data.get("measurements")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValidationError("measurements must be a non-empty list")
    items, identities = [], set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValidationError("each measurement must be an object")
        try:
            ra = float(raw.get("ra"))
            dec = float(raw.get("dec"))
            magnitude = float(raw.get("magnitude"))
        except (TypeError, ValueError):
            raise ValidationError("measurement coordinates and magnitude must be numeric")
        if ra < 0 or ra >= 360 or dec < -90 or dec > 90:
            raise ValidationError("measurement coordinates are outside valid ranges")
        try:
            calculate_priority(magnitude, candidate["data"].get("transient_type"))
        except ValidationError as exc:
            raise ValidationError("measurement magnitude is invalid: " + str(exc))
        observed_at = str(raw.get("observed_at", "")).strip()
        if not observed_at:
            raise ValidationError("measurement observed_at is required")
        item = {
            "observed_at": observed_at,
            "ra": ra,
            "dec": dec,
            "magnitude": magnitude,
            "station_id": data.get("station_id"),
        }
        identity = _measurement_identity(item)
        if identity in identities:
            raise ValidationError("duplicate measurement timestamp inside batch: " + observed_at)
        existing = candidate["data"].get("measurements") or []
        if any(_measurement_identity(old) == identity for old in existing):
            raise ValidationError("measurement timestamp already merged: " + observed_at)
        identities.add(identity)
        items.append(item)
    items.sort(key=lambda item: (item["observed_at"], str(item.get("station_id"))))
    pending = [dict(item, status="pending") for item in items]
    return {
        "candidate_id": candidate["id"],
        "station_id": data.get("station_id"),
        "measurements": pending,
        "submitted_at": None,
    }


def _validate_assign_observers(actor, entity, data, lookup):
    observers = data.get("observer_ids")
    if not isinstance(observers, list) or any(not str(item).strip() for item in observers):
        raise ValidationError("observer_ids must be a list of user ids")
    return {"observer_ids": list(observers)}


def _validate_merge_measurement(actor, entity, data, lookup):
    measurement = data.get("measurement")
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
    station = authorize_station(actor, data.get("station_id"), lookup, require=False)
    station_id = data.get("station_id") if station is not None else None
    incoming = {"observed_at": observed_at, "ra": ra, "dec": dec, "magnitude": magnitude}
    if station_id:
        incoming["station_id"] = station_id
    existing = list(entity["data"].get("measurements") or [])
    identity = _measurement_identity(incoming)
    if any(_measurement_identity(item) == identity for item in existing):
        raise ConflictError("measurement timestamp already merged")
    merged = existing + [incoming]
    latest = sorted(
        merged,
        key=lambda item: (str(item.get("observed_at")), str(item.get("station_id"))),
    )[-1]
    previous_magnitude = entity["data"].get("latest_magnitude")
    priority_score = calculate_priority(latest["magnitude"], entity["data"].get("transient_type"))
    # Keep raw request keys out of the candidate record; station_id lives on the
    # measurement itself, not on the candidate.
    data.pop("measurement", None)
    data.pop("station_id", None)
    return {
        "measurements": merged,
        "merged_measurement_count": len(merged),
        "latest_magnitude": latest["magnitude"],
        "priority_score": priority_score,
        "priority_recalculated": previous_magnitude != latest["magnitude"],
    }


def _validate_reclassify(actor, entity, data, lookup):
    if data.get("new_type") not in {"grb", "supernova", "tde", "variable", "unknown"}:
        raise ValidationError("unsupported transient type")
    return {"transient_type": data["new_type"], "previous_type": entity["data"].get("transient_type")}


def _validate_correct(actor, entity, data, lookup):
    if not str(data.get("reason", "")).strip():
        raise ValidationError("correction reason is required")
    return {"corrected_by": actor.user_id}


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
        "stations": "station",
        "measurement_batches": "measurement_batch",
    }
    INITIAL_STATUS = {
        "source": "registered",
        "candidate": "detected",
        "telescope": "available",
        "observation": "requested",
        "station": "registered",
        "measurement_batch": "queued",
    }
    TRANSITIONS = {
        "source": {
            "activate": (("registered",), "active"),
            "retire": (("active",), "retired"),
        },
        "candidate": {
            "merge_measurement": (("detected", "triaged"), "triaged"),
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
        "station": {
            "activate": (("registered",), "active"),
            "deactivate": (("active",), "inactive"),
            "reactivate": (("inactive",), "active"),
            "retire": (("registered", "active", "inactive"), "retired"),
            "assign_observers": (("registered", "active", "inactive"), "active"),
        },
        "measurement_batch": {
            "submit": (("queued", "partial"), "partial"),
            "retry": (("partial",), "partial"),
        },
    }
    CREATE_REQUIRED = {
        "source": ("name", "survey_name"),
        "candidate": ("source_id", "event_id", "ra", "dec", "magnitude", "transient_type", "observed_at"),
        "telescope": ("name", "aperture_m", "site_name"),
        "observation": ("candidate_id", "telescope_id", "team_id", "start_at", "end_at", "mode"),
        "station": ("name", "site_name"),
        "measurement_batch": ("candidate_id", "station_id", "measurements"),
    }
    ACTION_REQUIRED = {
        ("source", "retire"): ("reason",),
        ("candidate", "merge_measurement"): ("measurement",),
        ("candidate", "triage"): ("reason",),
        ("candidate", "reclassify"): ("new_type", "reason"),
        ("candidate", "correct"): ("reason",),
        ("candidate", "withdraw"): ("reason",),
        ("candidate", "classify"): ("classification",),
        ("telescope", "restrict"): ("reason",),
        ("observation", "schedule"): ("operator_id",),
        ("observation", "withdraw"): ("reason",),
        ("observation", "correct"): ("reason",),
        ("station", "deactivate"): ("reason",),
        ("station", "retire"): ("reason",),
        ("station", "assign_observers"): ("observer_ids",),
    }
    CREATE_ROLES = {
        "source": ("analyst", "admin"),
        "candidate": ("analyst", "operator", "admin"),
        "telescope": ("coordinator", "admin"),
        "observation": ("analyst", "coordinator", "admin"),
        "station": ("coordinator", "admin"),
        "measurement_batch": ("operator", "admin"),
    }
    ROLE_ACTIONS = {
        "activate": ("coordinator", "admin"),
        "retire": ("coordinator", "admin"),
        "merge_measurement": ("analyst", "operator", "admin"),
        "triage": ("analyst", "operator", "admin"),
        "reclassify": ("analyst", "supervisor", "admin"),
        "correct": ("analyst", "supervisor", "admin"),
        "withdraw": ("analyst", "supervisor", "admin"),
        "classify": ("analyst", "supervisor", "admin"),
        "restrict": ("coordinator", "admin"),
        "restore": ("coordinator", "admin"),
        "schedule": ("coordinator", "admin"),
        "complete": ("operator", "coordinator", "admin"),
        "deactivate": ("coordinator", "admin"),
        "reactivate": ("coordinator", "admin"),
        ("station", "assign_observers"): ("coordinator", "admin"),
        ("measurement_batch", "submit"): ("operator", "admin"),
        ("measurement_batch", "retry"): ("operator", "admin"),
    }
    CUSTOM_CREATE = {
        "source": _validate_source,
        "candidate": _validate_candidate,
        "telescope": _validate_telescope,
        "observation": _validate_observation,
        "station": _validate_station,
        "measurement_batch": _validate_measurement_batch,
    }
    CUSTOM_TRANSITIONS = {
        ("candidate", "merge_measurement"): _validate_merge_measurement,
        ("candidate", "reclassify"): _validate_reclassify,
        ("candidate", "correct"): _validate_correct,
        ("observation", "schedule"): _validate_schedule,
        ("observation", "correct"): _validate_correct,
        ("station", "assign_observers"): _validate_assign_observers,
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
