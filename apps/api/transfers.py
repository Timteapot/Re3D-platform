from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException

from backend.transfers import TransferLimitExceededError, TransferLimitService


LOGGER = logging.getLogger(__name__)


def transfer_limit_http_exception(
    exc: TransferLimitExceededError,
) -> HTTPException:
    headers = {
        "X-Re3D-Error-Code": exc.reason_code,
        "Cache-Control": "no-store",
    }
    if exc.retry_after_seconds is not None:
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return HTTPException(status_code=429, detail=str(exc), headers=headers)


def release_transfer_lease(
    service: TransferLimitService,
    lease_id: uuid.UUID,
) -> None:
    try:
        service.release(lease_id)
    except Exception:
        # The lease expiry remains a bounded recovery path if PostgreSQL is
        # temporarily unavailable while a response is closing.
        LOGGER.exception("Could not release transfer lease %s", lease_id)
