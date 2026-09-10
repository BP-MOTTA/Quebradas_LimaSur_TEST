"""Discovery connector for INDECI emergency reports."""

from __future__ import annotations

from collections.abc import Callable

from quebradas_limaeste.inventory.archive_connector import (
    ArchiveDiscoveryConnector,
    ArchiveDiscoverySettings,
)
from quebradas_limaeste.inventory.indeci_portal import (
    INDECI_EMERGENCY_PATH,
    INDECI_PORTAL_HOST,
)
from quebradas_limaeste.inventory.live_smoke import HttpTransport


class ArchiveEmergenciasConnector(ArchiveDiscoveryConnector):
    """Search the official archive of emergency reports."""

    def __init__(
        self,
        settings: ArchiveDiscoverySettings,
        *,
        transport: HttpTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        kwargs = {} if sleep is None else {"sleep": sleep}
        super().__init__(
            name="archive_emergencias",
            portal_url=f"https://{INDECI_PORTAL_HOST}{INDECI_EMERGENCY_PATH}",
            settings=settings,
            transport=transport,
            **kwargs,
        )
