"""Persisted user settings — PRD §7.3 FR-3.8, §8.6."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any

from trading_architect.config.adaptive_risk import AdaptiveRiskConfig
from trading_architect.config.options_selector import (
    IvAssumption,
    OptionSelectorConfig,
    ScoringWeights,
)
from trading_architect.config.sizing import DEFAULT_SIZING_CONFIG, EquityTier, SizingConfig
from trading_architect.models.entities import Silo


@dataclass
class OverrideLogEntry:
    timestamp: datetime
    parameter: str
    old_value: str
    new_value: str
    source: str = "settings_ui"


@dataclass
class AppSettings:
    """All user-overridable parameters for engines and UI."""

    starting_equity_stock_options: float = 100_000.0
    starting_equity_futures: float = 50_000.0
    schwab_account_hashes: dict[str, str] = field(default_factory=dict)
    refresh_interval_min: int = 15
    monthly_income_after_tax: float = 0.0
    monthly_expenses: float = 0.0
    cash_reserve_months: int = 6
    capital_base_mode: str = "deployable"
    include_forward_income: bool = False
    capital_base_blend_pct: float = 0.5
    cluster_stress_pct: float = -0.15
    sizing: SizingConfig = field(default_factory=lambda: SizingConfig())
    adaptive_risk: AdaptiveRiskConfig = field(default_factory=lambda: AdaptiveRiskConfig())
    option_selector: OptionSelectorConfig = field(default_factory=lambda: OptionSelectorConfig())

    def starting_equity(self) -> dict[Silo, float]:
        return {
            Silo.STOCK_OPTIONS: self.starting_equity_stock_options,
            Silo.FUTURES: self.starting_equity_futures,
        }


DEFAULT_APP_SETTINGS = AppSettings()


def _enum_value(obj: Any) -> Any:
    if hasattr(obj, "value"):
        return obj.value
    return obj


def sizing_config_to_dict(cfg: SizingConfig) -> dict[str, Any]:
    data = asdict(cfg)
    return data


def sizing_config_from_dict(data: dict[str, Any]) -> SizingConfig:
    tiers = [EquityTier(**t) for t in data.get("equity_tiers", [])]
    known = {f.name for f in fields(SizingConfig)}
    filtered = {k: v for k, v in data.items() if k in known and k != "equity_tiers"}
    return SizingConfig(**filtered, equity_tiers=tiers or DEFAULT_SIZING_CONFIG.equity_tiers)


def adaptive_config_to_dict(cfg: AdaptiveRiskConfig) -> dict[str, Any]:
    data = asdict(cfg)
    data["kelly_ladder"] = list(data["kelly_ladder"])
    data["base_f_ladder"] = list(data["base_f_ladder"])
    return data


def adaptive_config_from_dict(data: dict[str, Any]) -> AdaptiveRiskConfig:
    known = {f.name for f in fields(AdaptiveRiskConfig)}
    filtered = {k: v for k, v in data.items() if k in known}
    if "kelly_ladder" in filtered:
        filtered["kelly_ladder"] = tuple(filtered["kelly_ladder"])
    if "base_f_ladder" in filtered:
        filtered["base_f_ladder"] = tuple(filtered["base_f_ladder"])
    return AdaptiveRiskConfig(**filtered)


def option_selector_to_dict(cfg: OptionSelectorConfig) -> dict[str, Any]:
    data = asdict(cfg)
    data["iv_assumption"] = _enum_value(cfg.iv_assumption)
    data["scoring_weights"] = asdict(cfg.scoring_weights)
    return data


def option_selector_from_dict(data: dict[str, Any]) -> OptionSelectorConfig:
    weights_data = data.get("scoring_weights", {})
    weights = ScoringWeights(**weights_data) if weights_data else ScoringWeights()
    iv_raw = data.get("iv_assumption", IvAssumption.CONSTANT.value)
    iv = IvAssumption(iv_raw) if isinstance(iv_raw, str) else iv_raw
    known = {f.name for f in fields(OptionSelectorConfig)}
    filtered = {
        k: v
        for k, v in data.items()
        if k in known and k not in ("scoring_weights", "iv_assumption")
    }
    return OptionSelectorConfig(**filtered, iv_assumption=iv, scoring_weights=weights)


def app_settings_to_dict(settings: AppSettings) -> dict[str, Any]:
    return {
        "starting_equity_stock_options": settings.starting_equity_stock_options,
        "starting_equity_futures": settings.starting_equity_futures,
        "schwab_account_hashes": dict(settings.schwab_account_hashes),
        "refresh_interval_min": settings.refresh_interval_min,
        "monthly_income_after_tax": settings.monthly_income_after_tax,
        "monthly_expenses": settings.monthly_expenses,
        "cash_reserve_months": settings.cash_reserve_months,
        "capital_base_mode": settings.capital_base_mode,
        "include_forward_income": settings.include_forward_income,
        "capital_base_blend_pct": settings.capital_base_blend_pct,
        "cluster_stress_pct": settings.cluster_stress_pct,
        "sizing": sizing_config_to_dict(settings.sizing),
        "adaptive_risk": adaptive_config_to_dict(settings.adaptive_risk),
        "option_selector": option_selector_to_dict(settings.option_selector),
    }


def app_settings_from_dict(data: dict[str, Any]) -> AppSettings:
    defaults = DEFAULT_APP_SETTINGS
    sizing = sizing_config_from_dict(data.get("sizing", sizing_config_to_dict(defaults.sizing)))
    adaptive = adaptive_config_from_dict(
        data.get("adaptive_risk", adaptive_config_to_dict(defaults.adaptive_risk))
    )
    # Live f is owned by sizing config (PRD single source of truth).
    adaptive.current_base_f = sizing.base_risk_f
    adaptive.current_kelly_fraction = sizing.kelly_fraction
    raw_hashes = data.get("schwab_account_hashes") or {}
    schwab_hashes = (
        {str(k): str(v) for k, v in raw_hashes.items()} if isinstance(raw_hashes, dict) else {}
    )

    return AppSettings(
        starting_equity_stock_options=float(
            data.get("starting_equity_stock_options", defaults.starting_equity_stock_options)
        ),
        starting_equity_futures=float(
            data.get("starting_equity_futures", defaults.starting_equity_futures)
        ),
        schwab_account_hashes=schwab_hashes or defaults.schwab_account_hashes,
        refresh_interval_min=int(data.get("refresh_interval_min", defaults.refresh_interval_min)),
        monthly_income_after_tax=float(
            data.get("monthly_income_after_tax", defaults.monthly_income_after_tax)
        ),
        monthly_expenses=float(data.get("monthly_expenses", defaults.monthly_expenses)),
        cash_reserve_months=int(data.get("cash_reserve_months", defaults.cash_reserve_months)),
        capital_base_mode=str(data.get("capital_base_mode", defaults.capital_base_mode)),
        include_forward_income=bool(
            data.get("include_forward_income", defaults.include_forward_income)
        ),
        capital_base_blend_pct=float(
            data.get("capital_base_blend_pct", defaults.capital_base_blend_pct)
        ),
        cluster_stress_pct=float(data.get("cluster_stress_pct", defaults.cluster_stress_pct)),
        sizing=sizing,
        adaptive_risk=adaptive,
        option_selector=option_selector_from_dict(
            data.get("option_selector", option_selector_to_dict(defaults.option_selector))
        ),
    )


def app_settings_to_json(settings: AppSettings) -> str:
    return json.dumps(app_settings_to_dict(settings))


def app_settings_from_json(raw: str) -> AppSettings:
    return app_settings_from_dict(json.loads(raw))


def diff_app_settings(
    old: AppSettings,
    new: AppSettings,
    *,
    source: str = "settings_ui",
) -> list[OverrideLogEntry]:
    """Build override log entries for changed top-level parameters."""
    entries: list[OverrideLogEntry] = []
    now = datetime.now()

    scalar_fields = [
        (
            "starting_equity_stock_options",
            old.starting_equity_stock_options,
            new.starting_equity_stock_options,
        ),
        ("starting_equity_futures", old.starting_equity_futures, new.starting_equity_futures),
        (
            "monthly_income_after_tax",
            old.monthly_income_after_tax,
            new.monthly_income_after_tax,
        ),
        ("monthly_expenses", old.monthly_expenses, new.monthly_expenses),
        ("cash_reserve_months", old.cash_reserve_months, new.cash_reserve_months),
        ("capital_base_mode", old.capital_base_mode, new.capital_base_mode),
        ("include_forward_income", old.include_forward_income, new.include_forward_income),
        (
            "capital_base_blend_pct",
            old.capital_base_blend_pct,
            new.capital_base_blend_pct,
        ),
        ("cluster_stress_pct", old.cluster_stress_pct, new.cluster_stress_pct),
    ]
    for name, old_val, new_val in scalar_fields:
        if old_val != new_val:
            entries.append(
                OverrideLogEntry(
                    timestamp=now,
                    parameter=name,
                    old_value=str(old_val),
                    new_value=str(new_val),
                    source=source,
                )
            )

    sizing_fields = [
        ("sizing.base_risk_f", old.sizing.base_risk_f, new.sizing.base_risk_f),
        ("sizing.kelly_fraction", old.sizing.kelly_fraction, new.sizing.kelly_fraction),
        ("sizing.heat_cap", old.sizing.heat_cap, new.sizing.heat_cap),
        ("sizing.leverage_cap", old.sizing.leverage_cap, new.sizing.leverage_cap),
        ("sizing.drawdown_soft", old.sizing.drawdown_soft, new.sizing.drawdown_soft),
        (
            "sizing.drawdown_throttle_start",
            old.sizing.drawdown_throttle_start,
            new.sizing.drawdown_throttle_start,
        ),
        ("sizing.drawdown_hard", old.sizing.drawdown_hard, new.sizing.drawdown_hard),
    ]
    for name, old_val, new_val in sizing_fields:
        if old_val != new_val:
            entries.append(
                OverrideLogEntry(
                    timestamp=now,
                    parameter=name,
                    old_value=str(old_val),
                    new_value=str(new_val),
                    source=source,
                )
            )

    adaptive_fields = [
        (
            "adaptive_risk.min_trades_for_step_up",
            old.adaptive_risk.min_trades_for_step_up,
            new.adaptive_risk.min_trades_for_step_up,
        ),
    ]
    for name, old_val, new_val in adaptive_fields:
        if old_val != new_val:
            entries.append(
                OverrideLogEntry(
                    timestamp=now,
                    parameter=name,
                    old_value=str(old_val),
                    new_value=str(new_val),
                    source=source,
                )
            )

    return entries
