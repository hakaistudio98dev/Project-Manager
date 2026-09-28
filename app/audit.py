import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from .models import AuditLog, User


def _default(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, Enum):
        return o.value
    return str(o)


def snapshot(obj) -> dict:
    """Ambil kolom-kolom sebuah ORM object sebagai dict (untuk old/new value)."""
    return {c.key: getattr(obj, c.key) for c in inspect(obj).mapper.column_attrs}


def log(db: Session, actor: User | None, action: str, entity: str, entity_id: int,
        old: dict | None = None, new: dict | None = None, reason: str | None = None,
        org_id: int | None = None):
    db.add(AuditLog(
        org_id=org_id or actor.org_id, actor_id=actor.id if actor else None,
        action=action, entity=entity, entity_id=entity_id,
        old_value=json.dumps(old, default=_default) if old is not None else None,
        new_value=json.dumps(new, default=_default) if new is not None else None,
        reason=reason))
