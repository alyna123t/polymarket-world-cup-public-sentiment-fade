#!/usr/bin/env python3
"""
Polymarket World Cup Public Sentiment Fade

Non-technical thesis:
- Public/fan hype can overprice popular teams/players.
- We fade crowded YES pricing by buying NO when hype signals stack.
- Use patient GTC limit orders in thin books.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from simmer_sdk.skill import load_config, update_config, get_config_path

sys.stdout.reconfigure(line_buffering=True)

CONFIG_SCHEMA = {
    "scan_limit": {"env": "SIMMER_WCPSF_SCAN_LIMIT", "default": 400, "type": int, "help": "Markets to scan"},
    "import_source": {"env": "SIMMER_WCPSF_IMPORT_SOURCE", "default": "polymarket", "type": str, "help": "Market source"},
    "hype_yes_threshold": {"env": "SIMMER_WCPSF_HYPE_YES", "default": 0.72, "type": float, "help": "YES price considered crowded"},
    "hype_momentum_threshold": {"env": "SIMMER_WCPSF_HYPE_MOM", "default": 0.02, "type": float, "help": "1h YES momentum required"},
    "min_signals": {"env": "SIMMER_WCPSF_MIN_SIGNALS", "default": 2, "type": int, "help": "Minimum hype signals to trade"},
    "max_spread": {"env": "SIMMER_WCPSF_MAX_SPREAD", "default": 0.04, "type": float, "help": "Skip if spread wider"},
    "max_slippage_pct": {"env": "SIMMER_WCPSF_MAX_SLIPPAGE", "default": 0.05, "type": float, "help": "Skip if slippage higher"},
    "max_position_usd": {"env": "SIMMER_WCPSF_MAX_POSITION", "default": 10.0, "type": float, "help": "Max per market"},
    "daily_budget_usd": {"env": "SIMMER_WCPSF_DAILY_BUDGET", "default": 35.0, "type": float, "help": "Daily budget"},
    "max_trades_per_run": {"env": "SIMMER_WCPSF_MAX_TRADES", "default": 3, "type": int, "help": "Max entries per run"},
    "cooldown_hours": {"env": "SIMMER_WCPSF_COOLDOWN_H", "default": 24, "type": int, "help": "Per-market cooldown"},
    "limit_offsets_cents": {"env": "SIMMER_WCPSF_LIMIT_OFFSETS", "default": "2,1", "type": str, "help": "NO limit offsets below current NO, cents"},
    "limit_splits": {"env": "SIMMER_WCPSF_LIMIT_SPLITS", "default": "0.45,0.55", "type": str, "help": "Allocation split per rung"},
}

cfg = load_config(CONFIG_SCHEMA, __file__, slug="polymarket-world-cup-public-sentiment-fade")

SKILL_SLUG = "polymarket-world-cup-public-sentiment-fade"
TRADE_SOURCE = "sdk:world-cup-public-sentiment-fade"
BASE = Path(__file__).parent
SPEND_FILE = BASE / "daily_spend.json"
COOLDOWN_FILE = BASE / "cooldown_state.json"

CROWD_ENTITIES = {
    "argentina", "brazil", "england", "france", "spain", "portugal",
    "germany", "netherlands", "usa", "mexico", "italy", "uruguay",
    "messi", "mbappe", "ronaldo", "bellingham", "vinicius", "pulisic",
}

_client = None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))


def load_daily_spend() -> Dict[str, float]:
    today = now_utc().strftime("%Y-%m-%d")
    data = load_json(SPEND_FILE, {"date": today, "spent": 0.0, "trades": 0})
    if data.get("date") != today:
        data = {"date": today, "spent": 0.0, "trades": 0}
    return data


def get_client(live: bool):
    global _client
    if _client is None:
        from simmer_sdk import SimmerClient
        key = os.environ.get("SIMMER_API_KEY")
        if not key:
            print("Error: SIMMER_API_KEY not set")
            sys.exit(1)
        _client = SimmerClient(api_key=key, venue="polymarket", live=live)
    return _client


def get_positions(client) -> List[dict]:
    try:
        from dataclasses import asdict

        positions = client.get_positions(venue="polymarket")
        return [asdict(p) for p in positions]
    except Exception as e:
        print(f"Error fetching positions: {e}")
        return []


def check_context_safeguards(context: dict):
    if not context:
        return True, []

    reasons = []
    warnings = context.get("warnings", [])
    discipline = context.get("discipline", {})

    for warning in warnings:
        if "MARKET RESOLVED" in str(warning).upper():
            return False, ["Market already resolved"]

    warning_level = discipline.get("warning_level", "none")
    if warning_level == "severe":
        return False, [f"Severe flip-flop warning: {discipline.get('flip_flop_warning', '')}"]
    if warning_level == "mild":
        reasons.append("Mild flip-flop warning (proceed with caution)")

    return True, reasons


def parse_csv_floats(s: str) -> List[float]:
    out = []
    for x in s.split(','):
        x = x.strip()
        if x:
            out.append(float(x))
    return out


def is_world_cup_market(question: str) -> bool:
    q = question.lower()
    return "world cup" in q or "fifa" in q


def extract_entity_tokens(question: str) -> List[str]:
    q = re.sub(r"[^a-zA-Z0-9\s]", " ", question.lower())
    return [t for t in q.split() if len(t) >= 3]


def has_crowd_entity(question: str) -> bool:
    toks = set(extract_entity_tokens(question))
    return len(CROWD_ENTITIES & toks) > 0


def price_momentum_1h(client, market_id: str) -> float:
    pts = client.get_price_history(market_id)
    if len(pts) < 2:
        return 0.0
    parsed = []
    for p in pts:
        ts = p.get("timestamp")
        py = p.get("price_yes")
        if ts is None or py is None:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            parsed.append((dt, float(py)))
        except Exception:
            pass
    if len(parsed) < 2:
        return 0.0
    parsed.sort(key=lambda x: x[0])
    now = parsed[-1][0]
    hour_ago = now.timestamp() - 3600
    older = [p for p in parsed if p[0].timestamp() <= hour_ago]
    if not older:
        base = parsed[0][1]
    else:
        base = older[-1][1]
    return parsed[-1][1] - base


def safe_spread(ctx: dict, market_obj) -> Optional[float]:
    m = (ctx or {}).get("market") or {}
    try:
        if m.get("spread") is not None:
            return float(m.get("spread"))
    except Exception:
        pass
    try:
        s = getattr(market_obj, "spread", None)
        if s is not None:
            return float(s)
    except Exception:
        pass
    return None


def max_slippage(ctx: dict) -> float:
    est = (ctx.get("slippage") or {}).get("estimates") or []
    vals = []
    for e in est:
        try:
            vals.append(float(e.get("slippage_pct", 0.0)))
        except Exception:
            pass
    return max(vals) if vals else 0.0


def run(live: bool, quiet: bool = False, positions_only: bool = False, use_safeguards: bool = True) -> int:
    client = get_client(live)

    if positions_only:
        print(json.dumps(get_positions(client), indent=2))
        return 0

    spend = load_daily_spend()
    cooldown = load_json(COOLDOWN_FILE, {})
    tnow = now_utc().timestamp()

    offsets = parse_csv_floats(str(cfg["limit_offsets_cents"]))
    splits = parse_csv_floats(str(cfg["limit_splits"]))
    if len(offsets) != len(splits) or abs(sum(splits) - 1.0) > 1e-6:
        print("Invalid ladder config")
        return 2

    markets = client.get_markets(status="active", import_source=str(cfg["import_source"]), limit=int(cfg["scan_limit"]))
    cands = [m for m in markets if is_world_cup_market(m.question)]

    if not quiet:
        print("📣 World Cup Public Sentiment Fade")
        print(f"scanned={len(markets)} world_cup={len(cands)}")

    placed = []
    spent_run = 0.0

    ranked = []
    for m in cands:
        yes = float(m.current_probability)
        mom = price_momentum_1h(client, m.id)
        signals = 0
        reasons = []
        if has_crowd_entity(m.question):
            signals += 1
            reasons.append("crowd_entity")
        if yes >= float(cfg["hype_yes_threshold"]):
            signals += 1
            reasons.append("high_yes_price")
        if mom >= float(cfg["hype_momentum_threshold"]):
            signals += 1
            reasons.append("recent_up_momentum")

        score = signals + max(0.0, mom * 10)
        ranked.append((score, signals, reasons, yes, mom, m))

    ranked.sort(key=lambda x: x[0], reverse=True)

    for score, signals, reasons, yes, mom, m in ranked:
        if len(placed) >= int(cfg["max_trades_per_run"]):
            break
        if signals < int(cfg["min_signals"]):
            continue
        if spend["spent"] + spent_run >= float(cfg["daily_budget_usd"]):
            break

        last = float(cooldown.get(m.id, 0.0))
        if tnow - last < float(cfg["cooldown_hours"]) * 3600:
            continue

        ctx = client.get_market_context(m.id, venue="polymarket") or {}
        if use_safeguards:
            should_trade, reasons_sg = check_context_safeguards(ctx)
            if not should_trade:
                continue
            if reasons_sg and not quiet:
                print(f"safeguard: {m.question[:64]}... -> {'; '.join(reasons_sg)}")
        spread = safe_spread(ctx, m)
        slip = max_slippage(ctx)
        if spread is not None and spread > float(cfg["max_spread"]):
            continue
        if slip > float(cfg["max_slippage_pct"]):
            continue

        current_no = max(0.001, min(0.999, round(1.0 - yes, 3)))
        total = float(cfg["max_position_usd"])

        any_ok = False
        for off, split in zip(offsets, splits):
            px = max(0.001, round(current_no - off / 100.0, 3))
            amt = round(total * split, 2)
            if amt < 1.0:
                continue
            if spend["spent"] + spent_run + amt > float(cfg["daily_budget_usd"]):
                continue

            note = f"WC sentiment fade | signals={signals} reasons={','.join(reasons)} yes={yes:.3f} mom1h={mom:.3f}"
            if live:
                res = client.trade(
                    market_id=m.id,
                    side="no",
                    amount=amt,
                    action="buy",
                    venue="polymarket",
                    order_type="GTC",
                    price=px,
                    reasoning=note,
                    source=TRADE_SOURCE,
                    skill_slug=SKILL_SLUG,
                    allow_rebuy=False,
                    signal_data={
                        "signals": signals,
                        "reasons": ",".join(reasons),
                        "yes_price": round(yes, 5),
                        "no_price": round(current_no, 5),
                        "mom1h": round(mom, 5),
                        "spread": None if spread is None else round(spread, 5),
                        "slippage_pct": round(slip, 5),
                    },
                )
                ok = bool(getattr(res, "success", False))
                oid = getattr(res, "order_id", None)
            else:
                ok = True
                oid = "dry-run"

            if ok:
                any_ok = True
                spent_run += amt
                placed.append({
                    "question": m.question,
                    "amount": amt,
                    "price": px,
                    "order_id": oid,
                    "signals": signals,
                    "yes": round(yes, 3),
                    "mom1h": round(mom, 3),
                })

        if any_ok:
            cooldown[m.id] = tnow

    spend["spent"] = round(float(spend["spent"]) + spent_run, 2)
    spend["trades"] = int(spend.get("trades", 0)) + len(placed)
    save_json(SPEND_FILE, spend)
    save_json(COOLDOWN_FILE, cooldown)

    if placed:
        print(f"Placed {len(placed)} fade entries")
        for p in placed:
            print(f"- ${p['amount']:.2f} @ {p['price']:.3f} | yes={p['yes']:.3f} mom1h={p['mom1h']:.3f} | {p['order_id']}")
    else:
        print("No eligible sentiment-fade entries this run.")
    print(f"Daily spent: ${spend['spent']:.2f} / ${float(cfg['daily_budget_usd']):.2f}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="World Cup public sentiment fade trader")
    ap.add_argument("--live", action="store_true", help="Execute real orders")
    ap.add_argument("--positions", action="store_true", help="Show current positions and exit")
    ap.add_argument("--no-safeguards", action="store_true", help="Disable context safeguards")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--config", action="store_true")
    ap.add_argument("--set", action="append", default=[], help="key=value")
    args = ap.parse_args()

    if args.set:
        updates = {}
        for item in args.set:
            if "=" not in item:
                print(f"Invalid --set: {item}")
                return 2
            k, v = item.split("=", 1)
            k = k.strip()
            if k not in CONFIG_SCHEMA:
                print(f"Unknown config key: {k}")
                return 2
            t = CONFIG_SCHEMA[k]["type"]
            try:
                updates[k] = t(v)
            except Exception as e:
                print(f"Parse failed {k}: {e}")
                return 2
        update_config(updates, __file__)
        print(f"Updated config at {get_config_path(__file__)}")
        return 0

    if args.config:
        print(json.dumps(cfg, indent=2))
        return 0

    return run(
        live=args.live,
        quiet=args.quiet,
        positions_only=args.positions,
        use_safeguards=not args.no_safeguards,
    )


if __name__ == "__main__":
    raise SystemExit(main())
