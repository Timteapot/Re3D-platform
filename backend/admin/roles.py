from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from backend.db.models import AdminRoleChangeEvent, User


@dataclass(frozen=True)
class RoleChangeResult:
    user_id: uuid.UUID
    previous_role: str
    new_role: str
    changed: bool
    event_id: uuid.UUID | None


class AdminRoleService:
    """Apply explicit operator role changes while preserving an audit trail."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def set_role(
        self,
        *,
        user_id: uuid.UUID,
        role: str,
        confirmed: bool,
    ) -> RoleChangeResult:
        if not confirmed:
            raise ValueError("administrator role changes require explicit confirmation")
        if role not in {"user", "admin"}:
            raise ValueError("role must be user or admin")

        with self.sessions.begin() as session:
            active_admin_ids: list[uuid.UUID] | None = None
            if role == "user":
                active_admin_ids = list(
                    session.execute(
                        select(User.id)
                        .where(
                            User.role == "admin",
                            User.is_active.is_(True),
                        )
                        .order_by(User.id)
                        .with_for_update()
                    ).scalars()
                )
            user = session.execute(
                select(User).where(User.id == user_id).with_for_update()
            ).scalar_one_or_none()
            if user is None:
                raise ValueError("role-change target user does not exist")
            previous_role = user.role
            if previous_role == role:
                return RoleChangeResult(
                    user_id=user.id,
                    previous_role=previous_role,
                    new_role=role,
                    changed=False,
                    event_id=None,
                )
            if role == "admin" and not user.is_active:
                raise ValueError("an inactive user cannot be promoted to administrator")
            if previous_role == "admin" and role == "user":
                assert active_admin_ids is not None
                if user.is_active and len(active_admin_ids) <= 1:
                    raise ValueError("the last active administrator cannot be demoted")
                if not user.is_active and not active_admin_ids:
                    raise ValueError(
                        "an inactive administrator cannot be demoted until another "
                        "active administrator exists"
                    )

            event_id = uuid.uuid4()
            user.role = role
            user.updated_at = _database_now(session)
            session.add(
                AdminRoleChangeEvent(
                    id=event_id,
                    actor_kind="maintenance_cli",
                    actor_user_id=None,
                    target_user_id=user.id,
                    previous_role=previous_role,
                    new_role=role,
                    reason_code="OPERATOR_ROLE_CHANGE",
                )
            )
            return RoleChangeResult(
                user_id=user.id,
                previous_role=previous_role,
                new_role=role,
                changed=True,
                event_id=event_id,
            )


def _database_now(session: Session) -> datetime:
    value = session.execute(select(func.current_timestamp())).scalar_one()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
