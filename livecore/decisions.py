"""Pure, fill-aware v53 decisions. This module never sends or books an order.

Public dict contracts
---------------------
``spec``: lot_contracts, min_contracts, max_contracts, tick, ct_value, mmr.
ct_value is BTC per contract (ctVal * ctMult); all position quantities are BTC.
``quote``: bid, ask, last, ts; prices must be positive and bid <= ask.
``cfg``: risk, leverage, max_notional_multiple, reserve, fee, slip, halt_dd,
cooldown_hours. Defaults retain v53's 6% full risk / 3% scout risk and 15x.
``frame`` is a frozen v53 completed-hour frame (entry may be None).
``position`` follows the original engine state, but MUST contain actual filled
quantity/average/fees/funding/margin and actual executed ``adds``. ``fees`` is
a signed cost: positive means paid fees, negative means net rebates. No pending
order may be counted as a fill. ``available`` is the LESSER of the strategy's
free cash and exchange available collateral, never the full demo balance.

plan_entry returns action=entry or hold + reason. plan_manage returns close,
manage (stop plus optional add), or hold. contracts and limit_price are decimal
strings ready for OKX; BTC quantity, stop, and monetary estimates are floats.
observe_hour returns a deep copy. None of these functions mutates its inputs.

The caller enforces timestamps/freshness, hour deduplication, one-minute delay,
intent expiry, cooldown, drawdown halt, persistence and exchange reconciliation.
It MUST confirm native stop coverage before submitting a returned add. A stop
crossed now causes a real exit intent, never a fabricated historical fill.

Execution differences from paper: orders use a bounded IOC limit derived from
the current bid/ask, rounded INSIDE the slippage bound. Partial/no fills are
possible; actual exchange fills alone change the ledger. Replay K-line opens,
intraminute extrema, and modeled liquidation are not actual executions. The
liquidation formula below is only the retained conservative sizing screen,
not a prediction or replacement for the exchange's liquidation calculation.
"""

from copy import deepcopy
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import math

HOUR = 3_600_000
DEFAULTS = dict(risk=.06, leverage=15., max_notional_multiple=5.,
                reserve=.5, fee=.0005, slip=.0003, halt_dd=.6,
                cooldown_hours=6)


def _number(value, name, *, positive=False, nonnegative=False):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {name}") from exc
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"Invalid {name}")
    return result


def _decimal(value):
    return Decimal(str(value))


def _text(value):
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def _round_price(value, tick, up):
    units = (_decimal(value) / _decimal(tick)).to_integral_value(
        rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return units * _decimal(tick)


def _config(cfg):
    out = dict(DEFAULTS, **cfg)
    for key in ("risk", "leverage", "max_notional_multiple"):
        out[key] = _number(out[key], key, positive=True)
    for key in ("reserve", "fee", "slip", "halt_dd", "cooldown_hours"):
        out[key] = _number(out[key], key, nonnegative=True)
    if out["risk"] > 1 or out["reserve"] >= 1 or out["slip"] >= 1 or out["fee"] >= 1 or out["halt_dd"] > 1:
        raise ValueError("Invalid risk, reserve, fee, slippage or drawdown fraction")
    return out


def validate_spec(spec):
    """Return a validated normalized copy; never assume the old paper lot size."""
    out = dict(spec)
    for key in ("lot_contracts", "min_contracts", "max_contracts", "tick", "ct_value"):
        out[key] = _number(out[key], key, positive=True)
    out["mmr"] = _number(out["mmr"], "mmr", nonnegative=True)
    if out["min_contracts"] > out["max_contracts"] or out["mmr"] >= 1:
        raise ValueError("Invalid instrument limits")
    return out


def validate_quote(quote):
    """Validate quote shape only. The caller must reject stale timestamps."""
    out = dict(quote)
    for key in ("bid", "ask", "last"):
        out[key] = _number(out[key], key, positive=True)
    out["ts"] = _number(out["ts"], "quote timestamp", nonnegative=True)
    if out["bid"] > out["ask"]:
        raise ValueError("Crossed bid/ask")
    return out


def _direction(value):
    if value not in (-1, 1):
        raise ValueError("direction must be -1 or 1")
    return int(value)


def _contracts(quantity, spec):
    raw = _decimal(max(0., quantity)) / _decimal(spec["ct_value"])
    steps = (raw / _decimal(spec["lot_contracts"])).to_integral_value(rounding=ROUND_FLOOR)
    return steps * _decimal(spec["lot_contracts"])


def _quantity(contracts, spec):
    return float(contracts * _decimal(spec["ct_value"]))


def _ioc_price(direction, quote, spec, cfg):
    if direction == 1:
        bound = _decimal(quote["ask"]) * (1 + _decimal(cfg["slip"]))
        price = _round_price(bound, spec["tick"], False)
        return price if price >= _decimal(quote["ask"]) else None
    bound = _decimal(quote["bid"]) * (1 - _decimal(cfg["slip"]))
    price = _round_price(bound, spec["tick"], True)
    return price if 0 < price <= _decimal(quote["bid"]) else None


def _estimated_exit(raw, direction, spec, cfg):
    """Conservative exit estimate, not an exchange execution/IOC price."""
    side = -direction
    adjusted = _decimal(raw) * (1 + side * _decimal(cfg["slip"]))
    return float(_round_price(adjusted, spec["tick"], side == 1))


def modeled_stop_pnl(position, stop, cfg, spec):
    """Net whole-trade PNL at a hypothetical stop, including entry fees/funding."""
    cfg, spec = _config(cfg), validate_spec(spec)
    d = _direction(position["direction"])
    q = _number(position["quantity"], "position quantity", positive=True)
    avg = _number(position["average"], "average", positive=True)
    fees = _number(position["fees"], "fees")
    funding = _number(position["funding"], "funding")
    px = _estimated_exit(_number(stop, "stop", positive=True), d, spec, cfg)
    return d*q*(px-avg) - fees + funding - q*px*cfg["fee"]


def stop_for_net(position, target, cfg, spec):
    """Conservatively invert hypothetical stop PNL; stop trigger ticks tighten."""
    cfg, spec = _config(cfg), validate_spec(spec)
    d = _direction(position["direction"])
    q = _number(position["quantity"], "quantity", positive=True)
    target = _number(target, "protected target") + q*spec["tick"]*(1+cfg["fee"])
    avg = _number(position["average"], "average", positive=True)
    fees = _number(position["fees"], "fees")
    funding = _number(position["funding"], "funding")
    px = (target + d*q*avg + fees - funding)/(q*(d-cfg["fee"]))
    raw = px/(1-d*cfg["slip"])
    if raw <= 0 or not math.isfinite(raw):
        raise ValueError("Protected stop is invalid")
    return float(_round_price(raw, spec["tick"], d == 1))


def _liquidation(position, cfg, spec):
    d, q = position["direction"], position["quantity"]
    if spec["mmr"]+cfg["fee"] >= 1:
        raise ValueError("Invalid maintenance margin plus close fee")
    return (d*q*position["average"]-position["margin"])/(q*(d-spec["mmr"]-cfg["fee"]))


def _liq_screen(position, price, stop, cfg, spec):
    d = position["direction"]
    liq = _liquidation(position, cfg, spec)
    return d*(price-liq) > 0 and d*(price-stop) <= .6*d*(price-liq)


def _hold(reason):
    return dict(action="hold", reason=reason)


def plan_entry(frame, equity, available, quote, spec, cfg):
    """Plan one scout IOC. No position exists until actual fills are applied."""
    cfg, spec, quote = _config(cfg), validate_spec(spec), validate_quote(quote)
    equity = _number(equity, "strategy equity", positive=True)
    available = _number(available, "available strategy cash", nonnegative=True)
    signal = frame.get("entry")
    if signal is None:
        return _hold("no_entry_signal")
    d = _direction(signal["direction"])
    close = _number(signal["close"], "signal close", positive=True)
    atr = _number(signal["atr"], "signal ATR", positive=True)
    stop = float(_round_price(_number(signal["stop"], "signal stop", positive=True), spec["tick"], d == 1))
    cap = _ioc_price(d, quote, spec, cfg)
    if cap is None:
        return _hold("no_marketable_tick_within_slippage")
    price = float(cap)
    if d*(price-stop) <= 0 or d*((quote["bid"] if d == 1 else quote["ask"])-stop) <= 0:
        return _hold("invalid_stop")
    if abs(price-close) > .5*atr:
        return _hold("entry_gap")
    risk_budget = equity*cfg["risk"]
    scout_budget = risk_budget*.5
    exit_px = _estimated_exit(stop, d, spec, cfg)
    unit_risk = d*(price-exit_px) + (price+exit_px)*cfg["fee"]
    if unit_risk <= 0:
        return _hold("invalid_unit_risk")
    spending = max(0., available-cfg["reserve"]*equity)
    max_quantity = min(scout_budget/unit_risk,
                       spending/(price*(1/cfg["leverage"]+cfg["fee"])),
                       cfg["max_notional_multiple"]*equity/price,
                       spec["max_contracts"]*spec["ct_value"])
    contracts = _contracts(max_quantity, spec)
    if contracts < _decimal(spec["min_contracts"]):
        return _hold("minimum_lot_or_budget")
    q = _quantity(contracts, spec)
    p = dict(direction=d, quantity=q, average=price,
             margin=q*price/cfg["leverage"], fees=q*price*cfg["fee"], funding=0.)
    if not _liq_screen(p, price, stop, cfg, spec):
        return _hold("stop_too_near_liquidation")
    return dict(action="entry", reason="v53_scout", direction=d,
                contracts=_text(contracts), quantity=q, limit_price=_text(cap),
                stop=stop, risk_budget=risk_budget, scout_risk_budget=scout_budget,
                estimated_initial_risk=-modeled_stop_pnl(p, stop, cfg, spec),
                estimated_margin=p["margin"], estimated_fee=p["fees"],
                initial_gap=d*(price-stop), signal_known_at=signal["known_at"],
                reduce_only=False)


def observe_hour(position, frame, cfg, spec):
    """Snapshot the confirmed-position profit floor at a completed-hour close.

    Same/older observations are idempotent. The caller must persist this result
    BEFORE execution, and must not retroactively use fills occurring after the
    observation to claim that an unconfirmed scout already had a protected peak.
    """
    cfg, spec = _config(cfg), validate_spec(spec)
    out = deepcopy(position)
    known = int(_number(frame["ts"], "frame timestamp", nonnegative=True))
    if known % HOUR:
        raise ValueError("Expected a completed-hour frame")
    prior = out.get("protection_observed_at")
    if prior is not None and known <= prior:
        return out
    if known < _number(out["entry_ts"], "entry timestamp", nonnegative=True):
        raise ValueError("Observation predates actual entry")
    out["protection_observed_at"] = known
    if not out.get("add_count", 0):
        return out
    risk = _number(out["risk_budget"], "risk budget", positive=True)
    net = modeled_stop_pnl(out, frame["close"], cfg, spec)
    peak = max(_number(out.get("confirmed_peak_hourly_net", 0.), "confirmed peak", nonnegative=True), net)
    out["confirmed_peak_hourly_net"] = peak
    floor = _number(out.get("protected_floor", 0.), "protected floor", nonnegative=True)
    if peak >= 3*risk and .5*peak > floor:
        out["protected_floor"] = .5*peak
        out["protection_known_at"] = known
    return out


def exit_order(position, quote, spec, cfg, reason="requested_exit"):
    """Plan a reduce-only IOC. A partial exit remains pending until reconciled."""
    cfg, spec, quote = _config(cfg), validate_spec(spec), validate_quote(quote)
    direction = -_direction(position["direction"])
    quantity = _number(position["quantity"], "position quantity", positive=True)
    contracts = _contracts(quantity, spec)
    cap = _ioc_price(direction, quote, spec, cfg)
    if cap is None:
        return dict(action="hold", reason="exit_no_marketable_tick_within_slippage", exit_required=True)
    # Unlike opening/adds, closing a residual does not apply the minimum-open lot.
    if contracts <= 0:
        return dict(action="hold", reason="unrepresentable_exit_quantity", exit_required=True)
    residual = abs(quantity-_quantity(contracts, spec))
    if residual > max(1e-12, spec["ct_value"]*spec["lot_contracts"]*1e-7):
        raise ValueError("Position quantity is not an exact contract lot")
    return dict(action="close", reason=reason, direction=direction,
                quantity=_quantity(contracts, spec), contracts=_text(contracts),
                limit_price=_text(cap), reduce_only=True)


def _add_plan(position, frame, equity, available, quote, spec, cfg, stop):
    p = deepcopy(position)
    p["stop"] = stop
    d = _direction(p["direction"])
    known = int(_number(frame["ts"], "frame timestamp", nonnegative=True))
    if known-p["entry_ts"] < HOUR:
        return None, "confirmation_too_early"
    if frame["trend"] != d or not frame["strong"]:
        return None, "confirmation_trend_or_strength"
    initial_price = _number(p["initial_price"], "initial price", positive=True)
    gap = _number(p["initial_gap"], "initial gap", positive=True)
    close = _number(frame["close"], "frame close", positive=True)
    if d*(close-initial_price) < gap:
        return None, "confirmation_below_one_r"
    if not frame["fresh_high" if d == 1 else "fresh_low"]:
        return None, "confirmation_no_fresh_extreme"
    cap = _ioc_price(d, quote, spec, cfg)
    if cap is None:
        return None, "no_marketable_tick_within_slippage"
    price = float(cap)
    if d*(price-stop) <= 0:
        return None, "confirmation_invalid_stop"
    adds = p.get("adds", [])
    if p.get("add_count", 0) and not adds:
        raise ValueError("Actual addition history is missing")
    if adds:
        previous = adds[-1]
        if known//HOUR <= previous["known_at"]//HOUR:
            return None, "confirmation_hour_already_used"
        if d*(close-previous["price"]) <= 0 or d*(price-previous["price"]) <= 0:
            return None, "confirmation_not_better_price"
    if abs(price-close) > .5*_number(frame["atr"], "frame ATR", positive=True):
        return None, "confirmation_gap"
    tranche = 2 if min(d*(close-initial_price), d*(price-initial_price)) >= 2*gap else 1
    initial_q = _number(p["initial_quantity"], "initial quantity", positive=True)
    consumed = sum((_decimal(_number(a["quantity"], "actual added quantity", positive=True)) for a in adds), Decimal(0))
    capacity = _decimal(initial_q)*tranche
    remaining = capacity-consumed
    if remaining <= 0:
        return None, "confirmation_capacity_used"
    pre_stop = modeled_stop_pnl(p, stop, cfg, spec)
    floor = max(0., _number(p.get("protected_floor", 0.), "protected floor", nonnegative=True))
    risk = _number(p["risk_budget"], "risk budget", positive=True)
    capital = _number(p["capital_at_entry"], "capital at entry", positive=True)
    # Both the protected floor and maximum 2R giveback are linear quantity caps.
    required = max(floor, equity-capital-2*risk)
    exit_px = _estimated_exit(stop, d, spec, cfg)
    unit_loss = d*(price-exit_px) + (price+exit_px)*cfg["fee"]
    if unit_loss <= 0:
        return None, "confirmation_invalid_unit_risk"
    current_q = _number(p["quantity"], "position quantity", positive=True)
    spending = max(0., available-cfg["reserve"]*equity)
    upper = min(float(remaining),
                spending/(price*(1/cfg["leverage"]+cfg["fee"])),
                cfg["max_notional_multiple"]*equity/price-current_q,
                spec["max_contracts"]*spec["ct_value"]-current_q,
                (pre_stop-required)/unit_loss)
    contracts = _contracts(max(0., upper), spec)

    def candidate(lots):
        amount = _quantity(lots, spec)
        total = current_q+amount
        return dict(p, quantity=total,
                    average=(p["average"]*current_q+price*amount)/total,
                    margin=p["margin"]+amount*price/cfg["leverage"],
                    fees=p["fees"]+amount*price*cfg["fee"])

    if contracts < _decimal(spec["min_contracts"]):
        return None, "confirmation_risk_or_lot"
    if not _liq_screen(candidate(contracts), price, stop, cfg, spec):
        # Liquidation price is a ratio of affine quantities, hence monotonic in
        # added size. Search integer lots rather than iterating millions of lots.
        unit = _decimal(spec["lot_contracts"])
        low, high = 0, int(contracts/unit)
        if not _liq_screen(candidate(Decimal(0)), price, stop, cfg, spec):
            return None, "confirmation_liquidation_buffer"
        while low < high:
            mid = (low+high+1)//2
            if _liq_screen(candidate(unit*mid), price, stop, cfg, spec):
                low = mid
            else:
                high = mid-1
        contracts = unit*low
    if contracts < _decimal(spec["min_contracts"]):
        return None, "confirmation_risk_or_lot"
    q = _quantity(contracts, spec)
    added = candidate(contracts)
    after = modeled_stop_pnl(added, stop, cfg, spec)
    if after+1e-10 < required:
        return None, "confirmation_stop_floor"
    return dict(action="add", reason="v53_confirmation", direction=d,
                contracts=_text(contracts), quantity=q, limit_price=_text(cap),
                stop=stop, estimated_margin=q*price/cfg["leverage"],
                estimated_fee=q*price*cfg["fee"], estimated_stop_pnl=after,
                confirmation_stop_floor=floor, confirmation_tranche=tranche,
                confirmation_cumulative_quantity=float(consumed+_decimal(q)),
                confirmation_unlocked_capacity=float(capacity),
                signal_known_at=known, reduce_only=False), "confirmation_eligible"


def plan_manage(position, frame, equity, available, quote, spec, cfg):
    """Plan current-position exit or tightened stop plus optional confirmation.

    Feed a position previously updated by observe_hour for this frame. The
    returned add is conditional on protective stop confirmation by execution;
    it does not authorize placing two independent orders without reconciliation.
    """
    cfg, spec, quote = _config(cfg), validate_spec(spec), validate_quote(quote)
    equity = _number(equity, "strategy equity", positive=True)
    available = _number(available, "available strategy cash", nonnegative=True)
    p = deepcopy(position)
    d = _direction(p["direction"])
    _number(p["quantity"], "position quantity", positive=True)
    old_stop = _number(p["stop"], "position stop", positive=True)
    executable = quote["bid"] if d == 1 else quote["ask"]
    if d*(executable-old_stop) <= 0:
        return exit_order(p, quote, spec, cfg, "stop_crossed")
    if frame["exit_long" if d == 1 else "exit_short"]:
        return exit_order(p, quote, spec, cfg, "four_hour_trend_exit")
    trail = _number(frame["trail_long" if d == 1 else "trail_short"], "structural trail", positive=True)
    floor = _number(p.get("protected_floor", 0.), "protected floor", nonnegative=True)
    proposed = trail
    if floor > 0:
        protective = stop_for_net(p, floor, cfg, spec)
        proposed = max(proposed, protective) if d == 1 else min(proposed, protective)
    stop = max(old_stop, proposed) if d == 1 else min(old_stop, proposed)
    stop = float(_round_price(stop, spec["tick"], d == 1))
    if d*(executable-stop) <= 0:
        return exit_order(p, quote, spec, cfg, "trailing_stop_crossed")
    add, reason = _add_plan(p, frame, equity, available, quote, spec, cfg, stop)
    return dict(action="manage", reason=reason, stop=stop,
                stop_changed=d*(stop-old_stop) > 0, add=add,
                estimated_stop_pnl=modeled_stop_pnl(p, stop, cfg, spec))
