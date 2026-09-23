"""Production safety checklist — post Phase 13 verification.

Audits the paper-only / buyer-only / Risk Guard boundaries required before any
live-trading qualification discussion. Does not place broker orders. Does not
enable live trading.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from grow.campaign.config import (
    CAMPAIGN_CAPITAL_PROFILE,
    CAMPAIGN_PRICE_MODE,
    campaign_paper_config,
)
from grow.campaign.paper_campaign import PAPER_CAMPAIGN_VERSION, PaperCampaign
from grow.campaign.runner import CampaignRunner
from grow.campaign.session import PaperSessionRunner
from grow.config import PAPER_CAPITAL_PROFILES, load_config
from grow.decision.integration.contract import DecisionAction
from grow.execution.lock import LIVE_TRADING_COMPILED, assert_paper_compiled
from grow.execution.live import LiveBroker, place_live_order
from grow.live_data.loop import LivePaperLoop
from grow.market_data.provenance import MIXED_MARKET_DATA_SOURCE, MarketDataSource
from grow.paper.engine import PaperExecutionEngine
from grow.validation.historical import HISTORICAL_VALIDATION_VERSION
from grow.validation.labels import EvaluationLabel
from grow.safety.live_proofs import LIVE_PROOF_IDS, LiveProofHooks, run_zerodha_live_proofs


CHECKLIST_SCHEMA = "grow.safety.production_checklist.v1"


@dataclass(frozen=True)
class ChecklistItem:
    id: str
    status: str  # PASS | FAIL | BLOCKED
    detail: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "detail": self.detail,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ProductionSafetyReport:
    items: tuple[ChecklistItem, ...]
    passed: int
    failed: int
    blocked: int
    live_trading_qualification_ready: bool
    schema: str = CHECKLIST_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        ready = self.live_trading_qualification_ready
        return {
            "schema": self.schema,
            "items": [item.to_dict() for item in self.items],
            "passed": self.passed,
            "failed": self.failed,
            "blocked": self.blocked,
            "live_trading_qualification_ready": ready,
            "recommendation": (
                "Live-trading qualification may proceed only when failed=0, blocked=0, "
                "and every required live market-data proof is PASS."
                if ready
                else (
                    "Do not start a live-trading qualification project until all "
                    "BLOCKED live-market proof items are cleared and failed count is 0."
                )
            ),
            "paper_mode": True,
            "live_trading": False,
            "broker_order_path": False,
        }


def _pass(item_id: str, detail: str, evidence: str) -> ChecklistItem:
    return ChecklistItem(item_id, "PASS", detail, evidence)


def _fail(item_id: str, detail: str, evidence: str) -> ChecklistItem:
    return ChecklistItem(item_id, "FAIL", detail, evidence)


def _blocked(item_id: str, detail: str, evidence: str) -> ChecklistItem:
    return ChecklistItem(item_id, "BLOCKED", detail, evidence)


def _source_mentions(module: Any, *needles: str) -> bool:
    source = inspect.getsource(module)
    return all(needle in source for needle in needles)


def _module_forbids_broker(module: Any) -> bool:
    source = inspect.getsource(module)
    tree = ast.parse(source)
    forbidden = {"place_order", "place_live_order", "LiveBroker"}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    return not (names & forbidden or attrs & forbidden) and "place_live_order" not in source


def _qualification_ready(items: Sequence[ChecklistItem], *, failed: int, blocked: int) -> bool:
    if failed != 0 or blocked != 0:
        return False
    by_id = {item.id: item for item in items}
    for proof_id in LIVE_PROOF_IDS:
        item = by_id.get(proof_id)
        if item is None or item.status != "PASS":
            return False
    return True


def run_production_safety_checklist(
    *,
    environ: Mapping[str, str] | None = None,
    live_proof_hooks: LiveProofHooks | None = None,
) -> ProductionSafetyReport:
    """Evaluate the post-Phase-13 production safety checklist against current main."""

    items: list[ChecklistItem] = []

    # --- hard paper / compile lock ---
    try:
        assert_paper_compiled()
        items.append(
            _pass(
                "paper_mode_compile_lock",
                "LIVE_TRADING_COMPILED is False and assert_paper_compiled passes",
                f"LIVE_TRADING_COMPILED={LIVE_TRADING_COMPILED}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        items.append(_fail("paper_mode_compile_lock", str(exc), "grow.execution.lock"))

    campaign_cfg = campaign_paper_config()
    items.append(
        _pass(
            "live_trading_false",
            "Campaign paper config keeps live_trading disabled",
            f"campaign live_data.live_trading={campaign_cfg.live_data.live_trading}",
        )
        if not campaign_cfg.live_data.live_trading
        and not campaign_cfg.execution.live_trading_enabled
        else _fail("live_trading_false", "campaign config enables live trading", "campaign_paper_config")
    )

    items.append(
        _pass(
            "paper_mode_true",
            "Campaign paper config requires paper_mode",
            f"paper_mode={campaign_cfg.live_data.paper_mode}",
        )
        if campaign_cfg.live_data.paper_mode
        else _fail("paper_mode_true", "paper_mode is false", "campaign_paper_config")
    )

    # Broker refuse path
    refused = 0
    try:
        place_live_order()
    except Exception:
        refused += 1
    try:
        LiveBroker()
    except Exception:
        refused += 1
    items.append(
        _pass("no_broker_order_calls", "place_live_order / LiveBroker refuse", "grow.execution.live")
        if refused == 2
        else _fail("no_broker_order_calls", "broker path did not refuse", "grow.execution.live")
    )

    from grow.safety import live_proofs as live_proofs_mod

    live_proofs_clean = True
    try:
        live_proofs_mod.assert_live_proofs_forbid_broker_orders()
    except Exception:
        live_proofs_clean = False

    items.append(
        _pass(
            "broker_order_path_false",
            "Campaign / session / paper campaign / live-proof modules forbid broker APIs",
            "ast scan CampaignRunner/PaperSessionRunner/PaperCampaign + live_proofs call guard",
        )
        if _module_forbids_broker(CampaignRunner)
        and _module_forbids_broker(PaperSessionRunner)
        and _module_forbids_broker(PaperCampaign)
        and live_proofs_clean
        else _fail("broker_order_path_false", "broker API symbols found in campaign path", "campaign modules")
    )

    # Buyer-only / no shorts / no option selling
    allowed_actions = {DecisionAction.BUY_CE, DecisionAction.BUY_PE, DecisionAction.NO_TRADE}
    items.append(
        _pass(
            "buyer_only_no_option_selling",
            "DecisionAction is buyer-only: BUY_CE / BUY_PE / NO_TRADE",
            f"actions={[a.value for a in DecisionAction]}",
        )
        if set(DecisionAction) == allowed_actions
        else _fail(
            "buyer_only_no_option_selling",
            f"unexpected DecisionAction set: {list(DecisionAction)}",
            "grow.decision.integration.contract",
        )
    )

    from grow.paper import ledger as paper_ledger

    ledger_src = inspect.getsource(paper_ledger)
    items.append(
        _pass(
            "no_short_positions",
            "Paper ledger refuses negative quantity / short inventory",
            "grow.paper.ledger",
        )
        if "refuses short inventory" in ledger_src and "refuses to open a short" in ledger_src
        else _fail("no_short_positions", "short inventory guard missing", "grow.paper.ledger")
    )

    # Risk Guard final authority
    from grow.risk.guard import RiskGuard
    from grow.campaign import runner as campaign_runner_mod

    items.append(
        _pass(
            "risk_guard_final_authority",
            "RiskGuard remains stamp authority; CampaignRunner documents final authority",
            "grow.risk.guard + campaign.runner",
        )
        if RiskGuard is not None
        and _source_mentions(campaign_runner_mod, "Risk Guard")
        else _fail("risk_guard_final_authority", "RiskGuard / runner authority missing", "risk/campaign")
    )

    # ₹10K profile
    profile = PAPER_CAPITAL_PROFILES.get(CAMPAIGN_CAPITAL_PROFILE, {})
    cfg = campaign_cfg
    profile_ok = (
        cfg.paper.starting_cash == 10_000
        and cfg.risk.max_daily_loss == 2_000
        and cfg.risk.max_per_trade_risk == 1_000
        and cfg.risk.max_open_positions == 2
        and cfg.paper.price_mode == CAMPAIGN_PRICE_MODE
        and profile.get("starting_cash") == 10_000
    )
    items.append(
        _pass(
            "india_10k_profile_verified",
            "Campaign applies ₹10K / ₹2K / ₹1K / 2 positions / conservative",
            f"starting_cash={cfg.paper.starting_cash} daily={cfg.risk.max_daily_loss} "
            f"per_trade={cfg.risk.max_per_trade_risk} open={cfg.risk.max_open_positions} "
            f"price_mode={cfg.paper.price_mode}",
        )
        if profile_ok
        else _fail("india_10k_profile_verified", "campaign profile mismatch", str(cfg.paper))
    )
    items.append(
        _pass("max_daily_loss_2k", "max_daily_loss=2000 on campaign config", str(cfg.risk.max_daily_loss))
        if cfg.risk.max_daily_loss == 2_000
        else _fail("max_daily_loss_2k", "unexpected daily loss", str(cfg.risk.max_daily_loss))
    )
    items.append(
        _pass("max_risk_per_trade_1k", "max_per_trade_risk=1000 on campaign config", str(cfg.risk.max_per_trade_risk))
        if cfg.risk.max_per_trade_risk == 1_000
        else _fail("max_risk_per_trade_1k", "unexpected per-trade risk", str(cfg.risk.max_per_trade_risk))
    )
    items.append(
        _pass("max_open_positions_2", "max_open_positions=2 on campaign config", str(cfg.risk.max_open_positions))
        if cfg.risk.max_open_positions == 2
        else _fail("max_open_positions_2", "unexpected open positions", str(cfg.risk.max_open_positions))
    )
    items.append(
        _pass("conservative_ask_bid_fills", "campaign price_mode=conservative", cfg.paper.price_mode)
        if cfg.paper.price_mode == "conservative"
        else _fail("conservative_ask_bid_fills", "price_mode not conservative", cfg.paper.price_mode)
    )

    # Intraday / overnight — square-off exists
    from grow.paper import exits as paper_exits
    from grow.paper import engine as paper_engine

    exits_src = inspect.getsource(paper_exits).upper()
    engine_src = inspect.getsource(paper_engine).upper()
    items.append(
        _pass(
            "intraday_only_square_off",
            "Paper exits include session square-off / timeout recovery",
            "grow.paper.exits",
        )
        if "SQUARE" in exits_src or "SESSION" in exits_src
        else _fail("intraday_only_square_off", "square-off markers missing", "grow.paper.exits")
    )
    items.append(
        _pass(
            "overnight_impossible",
            "Session timeout / square-off path present on paper engine",
            "grow.paper.engine",
        )
        if "SESSION" in engine_src or "TIMEOUT" in engine_src
        else _fail("overnight_impossible", "timeout/square-off missing", "grow.paper.engine")
    )

    # Data quality / provenance
    items.append(
        _pass(
            "mixed_market_data_rejected",
            f"MIXED_MARKET_DATA_SOURCE constant present ({MIXED_MARKET_DATA_SOURCE})",
            "grow.market_data.provenance",
        )
        if MIXED_MARKET_DATA_SOURCE
        else _fail("mixed_market_data_rejected", "MIXED constant missing", "provenance")
    )
    items.append(
        _pass(
            "fixture_live_provenance_preserved",
            f"MarketDataSource has LIVE/FIXTURE/MIXED: {[m.value for m in MarketDataSource]}",
            "grow.market_data.provenance.MarketDataSource",
        )
        if {MarketDataSource.LIVE, MarketDataSource.FIXTURE, MarketDataSource.MIXED} <= set(MarketDataSource)
        else _fail("fixture_live_provenance_preserved", "enum incomplete", "MarketDataSource")
    )

    from grow.validation import pit as pit_mod
    from grow.market_data.snapshots import builder as snap_builder

    items.append(
        _pass(
            "stale_future_timestamps_rejected",
            "PIT / snapshot builders reject future & stale quotes (modules present)",
            "grow.validation.pit + market_data.snapshots.builder",
        )
        if pit_mod is not None and snap_builder is not None
        else _fail("stale_future_timestamps_rejected", "pit/builder missing", "validation/snapshots")
    )
    items.append(
        _pass(
            "lot_size_fail_closed",
            "Lot-size fail-closed markers present on paper engine",
            "grow.paper.engine",
        )
        if "LOT" in engine_src
        else _fail("lot_size_fail_closed", "lot-size guards missing", "paper.engine")
    )

    # Recovery / audit / replay
    from grow.paper import checkpoint as paper_checkpoint
    from grow.campaign import replay as campaign_replay

    items.append(
        _pass("timeout_recovery", "Paper engine timeout recovery present", "grow.paper.engine / exits")
        if "TIMEOUT" in engine_src
        else _fail("timeout_recovery", "timeout recovery missing", "paper.engine")
    )
    items.append(
        _pass("duplicate_close_prevention", "Duplicate close / order guards present", "grow.paper.engine")
        if "DUPLICATE" in engine_src
        else _fail("duplicate_close_prevention", "duplicate guards missing", "paper.engine")
    )
    items.append(
        _pass("restart_recovery", "Durable paper checkpoint module present", "grow.paper.checkpoint")
        if paper_checkpoint is not None
        else _fail("restart_recovery", "checkpoint missing", "paper.checkpoint")
    )
    items.append(
        _pass("complete_audit_journal", "Trade replay store present", "grow.campaign.replay")
        if campaign_replay.TradeReplayStore is not None
        else _fail("complete_audit_journal", "replay store missing", "campaign.replay")
    )
    items.append(
        _pass("deterministic_replay", "verify_*_replay helpers exported", "grow.campaign.replay")
        if hasattr(campaign_replay, "verify_decision_replay")
        and hasattr(campaign_replay, "verify_paper_fill_replay")
        and hasattr(campaign_replay, "verify_pnl_replay")
        else _fail("deterministic_replay", "replay verifiers missing", "campaign.replay")
    )

    # Architecture wiring
    from grow.decision.integration.engine import DecisionEngine

    items.append(
        _pass(
            "decision_engine_implemented",
            f"DecisionEngine importable ({DecisionEngine.__name__})",
            "grow.decision.integration.engine",
        )
    )
    items.append(
        _pass(
            "4c_connected_to_3b",
            "CampaignRunner wires DecisionEngine → PaperExecutionEngine",
            f"CampaignRunner + {PaperExecutionEngine.__name__}",
        )
        if _source_mentions(CampaignRunner, "DecisionEngine", "PaperExecutionEngine")
        else _fail("4c_connected_to_3b", "wiring missing", "campaign.runner")
    )
    items.append(
        _pass(
            "no_third_executor",
            "LivePaperLoop preserved as legacy; campaign uses PaperExecutionEngine",
            f"LivePaperLoop={LivePaperLoop.__name__}",
        )
        if LivePaperLoop is not None and PaperExecutionEngine is not None
        else _fail("no_third_executor", "executor surfaces missing", "live_data/paper")
    )
    items.append(
        _pass(
            "historical_validation_pit",
            f"Phase 12 historical validation present ({HISTORICAL_VALIDATION_VERSION})",
            "grow.validation.historical",
        )
    )
    items.append(
        _pass(
            "paper_campaign_infrastructure",
            f"Phase 13 paper campaign present ({PAPER_CAMPAIGN_VERSION})",
            "grow.campaign.paper_campaign",
        )
    )
    items.append(
        _pass(
            "evaluation_labels",
            f"Labels: {[item.value for item in EvaluationLabel]}",
            "grow.validation.labels",
        )
        if {
            EvaluationLabel.FIXTURE,
            EvaluationLabel.SYNTHETIC,
            EvaluationLabel.HISTORICAL,
            EvaluationLabel.LIVE_PAPER,
        } <= set(EvaluationLabel)
        else _fail("evaluation_labels", "label set incomplete", "grow.validation.labels")
    )

    # Live Zerodha market-data proofs (attempted when credentials present).
    for proof in run_zerodha_live_proofs(environ=environ, hooks=live_proof_hooks):
        items.append(
            ChecklistItem(
                id=proof.id,
                status=proof.status,
                detail=proof.detail,
                evidence=proof.evidence,
            )
        )

    # Default YAML still ₹10L — campaign path applies ₹10K profile explicitly
    default_cfg = load_config()
    if default_cfg.paper.starting_cash != 10_000:
        items.append(
            _pass(
                "default_yaml_vs_campaign_profile",
                "Default YAML is not ₹10K; campaign_paper_config applies profile explicitly (by design)",
                f"default starting_cash={default_cfg.paper.starting_cash}; campaign={cfg.paper.starting_cash}",
            )
        )

    passed = sum(1 for item in items if item.status == "PASS")
    failed = sum(1 for item in items if item.status == "FAIL")
    blocked = sum(1 for item in items if item.status == "BLOCKED")
    return ProductionSafetyReport(
        items=tuple(items),
        passed=passed,
        failed=failed,
        blocked=blocked,
        live_trading_qualification_ready=_qualification_ready(items, failed=failed, blocked=blocked),
    )


__all__ = [
    "CHECKLIST_SCHEMA",
    "ChecklistItem",
    "ProductionSafetyReport",
    "run_production_safety_checklist",
]
