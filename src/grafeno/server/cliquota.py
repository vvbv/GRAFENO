"""Available CLI usage (quota) operations shared by the REST and WS APIs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import quota
from . import branding

if TYPE_CHECKING:
    from .service import ServerService


def monitor(service: "ServerService") -> quota.QuotaMonitor:
    """The App's monitor when there is one; otherwise one kept on the service (tests)."""
    app = getattr(service, "app", None)
    found = getattr(app, "quota_monitor", None) if app is not None else None
    if isinstance(found, quota.QuotaMonitor):
        return found
    own = getattr(service, "_quota_monitor", None)
    if own is None:
        own = quota.QuotaMonitor()
        service._quota_monitor = own
    return own


async def quota_report(service: "ServerService", cached: bool = False) -> dict:
    """Live (throttled) or cached quota of every CLI used by the config and profiles."""
    payload = await monitor(service).get(live=not cached)
    payload["instance_name"] = branding.instance_name()
    return payload
