"""Discovery connector for INDECI preliminary/complementary reports."""

from __future__ import annotations

from collections.abc import Callable

from quebradas_limaeste.inventory.archive_connector import (
    ArchiveDiscoveryConnector,
    ArchiveDiscoverySettings,
)
from quebradas_limaeste.inventory.indeci_portal import (
    INDECI_PORTAL_HOST,
    INDECI_RESULTS_PATH,
)
from quebradas_limaeste.inventory.live_smoke import HttpTransport


class ArchiveInformesConnector(ArchiveDiscoveryConnector):
    """Search the official archive of preliminary/complementary reports."""

    def __init__(
        self,
        settings: ArchiveDiscoverySettings,
        *,
        transport: HttpTransport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        kwargs = {} if sleep is None else {"sleep": sleep}
        super().__init__(
            name="archive_informes",
            portal_url=f"https://{INDECI_PORTAL_HOST}{INDECI_RESULTS_PATH}",
            settings=settings,
            transport=transport,
            **kwargs,
        )
