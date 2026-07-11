from __future__ import annotations

from dataclasses import dataclass

from kunjin.adapters.eastmoney import EastmoneyFundClient, EastmoneyMarketClient
from kunjin.storage.repository import Repository


@dataclass(frozen=True)
class ResearchSyncResult:
    entity: str
    observations: int


class ResearchSyncService:
    def __init__(
        self,
        repository: Repository,
        fund_client: EastmoneyFundClient,
        market_client: EastmoneyMarketClient,
    ) -> None:
        self.repository = repository
        self.fund_client = fund_client
        self.market_client = market_client

    def sync_fund(self, fund_code: str) -> ResearchSyncResult:
        _, name, fund_type, observations = self.fund_client.fetch_nav_history(fund_code)
        self.repository.save_fund_history(
            fund_code, name, fund_type, "eastmoney", observations
        )
        return ResearchSyncResult(fund_code, len(observations))

    def sync_market(self) -> ResearchSyncResult:
        observations = []
        for sector_kind in ("industry", "concept"):
            _, items = self.market_client.fetch_sectors(sector_kind)
            observations.extend(items)
        self.repository.save_sector_snapshots(observations)
        return ResearchSyncResult("market_sectors", len(observations))

