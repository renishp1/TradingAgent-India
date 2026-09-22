"""Public-doc candidate providers. CANDIDATE only. No live API. No ranking."""

from __future__ import annotations

from dataclasses import dataclass

from grow.history.models import CANDIDATE


@dataclass(frozen=True)
class ProviderCandidate:
    name: str
    status: str
    documented_capability: str
    still_to_prove: str
    references: tuple[str, ...]
    live: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "status": self.status,
            "documented_capability": self.documented_capability,
            "still_to_prove": self.still_to_prove,
            "references": list(self.references),
            "live": self.live,
            "ranking": None,
            "approved_for_2e": False,
        }


PUBLIC_CANDIDATES: tuple[ProviderCandidate, ...] = (
    ProviderCandidate(
        name="NSE Data & Analytics",
        status=CANDIDATE,
        documented_capability="Paid EOD F&O, historical trade data, 1/5-minute snapshots, current contract specs.",
        still_to_prove="PIT option-chain reconstruction, lot-size history, licensing for research replay.",
        references=(
            "https://www.nseindia.com/static/market-data/eod-historical-data-subscription",
            "https://www.nseindia.com/static/products-services/equity-derivatives-contract-specifications",
        ),
    ),
    ProviderCandidate(
        name="Global Datafeeds",
        status=CANDIDATE,
        documented_capability="Historical tick/intraday/EOD, option-chain APIs, Greeks, NFO symbol formats.",
        still_to_prove="Long-duration chain snapshots, bid/ask/OI completeness, listing-window semantics.",
        references=(
            "https://globaldatafeeds.in/apis/",
            "https://globaldatafeeds.in/global-datafeeds-apis/global-datafeeds-apis/optionchain-api/subscribeoptionchain/",
        ),
    ),
    ProviderCandidate(
        name="TrueData",
        status=CANDIDATE,
        documented_capability="Live option-chain LTP/volume/OI/bid-ask/Greeks; historical REST availability.",
        still_to_prove="Historical chain depth, lot-size history, PIT snapshot semantics, research license.",
        references=(
            "https://www.truedata.in/products/marketdataapi",
            "https://feedback.truedata.in/knowledge-base/article/historical-data-availability-through-rest-api",
        ),
    ),
)
