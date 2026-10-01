from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DomainError(Exception):
    """Base class for domain failures."""


class ValidationError(DomainError):
    """Input does not satisfy a domain rule."""


class PermissionDenied(DomainError):
    """Actor is not allowed to perform the action."""


class NotFoundError(DomainError):
    """Requested record does not exist."""


class ConflictError(DomainError):
    """A uniqueness or version constraint was violated."""


class InvalidTransition(DomainError):
    """The requested state transition is not valid."""


class Role(str, Enum):
    viewer = "viewer"
    reporter = "reporter"
    operator = "operator"
    analyst = "analyst"
    coordinator = "coordinator"
    supervisor = "supervisor"
    auditor = "auditor"
    admin = "admin"


@dataclass
class Actor:
    user_id: str
    role: str
    station_id: str = None

    @classmethod
    def from_headers(cls, headers):
        user_id = headers.get("X-User-Id", "anonymous")
        role = headers.get("X-Role", "viewer")
        if role not in {item.value for item in Role}:
            raise PermissionDenied("unknown role: " + role)
        station_id = headers.get("X-Station-Id") or None
        return cls(user_id=user_id, role=role, station_id=station_id)


@dataclass
class Entity:
    id: str
    kind: str
    status: str
    version: int
    data: Dict[str, Any]
    created_by: str
    created_at: str
    updated_at: str
