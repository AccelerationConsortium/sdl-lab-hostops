"""Best-effort audit posting to the dashboard's event-ingest path.

Mirrors the xArm / OT-2 events-exporter pattern: disabled unless
``HOSTOPS_INGEST_URL`` is set (e.g. ``http://sdl2-server:8001``), never blocks
or fails the underlying operation, and stamps ``extra.source`` so rows are
attributable. Only mutating operations are audited — reads would be noise.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("lab_hostops.audit")


async def post_audit(equipment_id: str, *, event: str, message: str, extra: dict[str, Any]) -> bool:
    base = os.environ.get("HOSTOPS_INGEST_URL")
    if not base:
        return False
    import httpx

    payload = {
        "device_id": equipment_id,
        "records": [
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": event,
                "message": message,
                "extra": {**extra, "source": "lab-hostops"},
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{base.rstrip('/')}/api/ingest/events", json=payload)
        ok = resp.status_code in (200, 204)
        if not ok:
            logger.warning("audit post rejected: HTTP %s %s", resp.status_code, resp.text[:200])
        return ok
    except Exception as exc:
        logger.warning("audit post failed: %s", exc)
        return False
