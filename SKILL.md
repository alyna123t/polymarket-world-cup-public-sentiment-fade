---
name: polymarket-world-cup-public-sentiment-fade
description: Fade public-hype World Cup markets by buying NO on crowded YES pricing with patient limit orders.
metadata:
  author: Alyna + Hermes
  version: "0.1.0"
  displayName: Polymarket World Cup Public Sentiment Fade
  difficulty: beginner
---

# Polymarket World Cup Public Sentiment Fade

A non-technical World Cup strategy:
- popular teams/players can get overhyped,
- YES price gets crowded,
- skill fades that by buying NO with patient limits.

## Signals used

- market includes a crowd-name entity (e.g. Messi, Brazil, England)
- YES price already high (default 72%+)
- recent 1h upward momentum (hype continuation)

The skill trades only when enough signals stack.

## Controls

- daily budget cap
- max trades per run
- spread/slippage quality filters
- per-market cooldown
- laddered GTC limits (patient fills)

## Run

```bash
cd skills/polymarket-world-cup-public-sentiment-fade
python public_sentiment_fade.py --config
python public_sentiment_fade.py
python public_sentiment_fade.py --live
```

## Tune

```bash
python public_sentiment_fade.py --set hype_yes_threshold=0.75
python public_sentiment_fade.py --set min_signals=3
python public_sentiment_fade.py --set max_position_usd=15
python public_sentiment_fade.py --set daily_budget_usd=50
```

## Notes

- This is a sentiment-heuristic strategy, not a fundamental football model.
- Best used alongside your other WC skills as a diversification sleeve.
- Start in dry-run and calibrate thresholds before going live.

## Deterministic spec (Skill Builder style)

### Signal
- Crowd-hype proxy from stacked conditions:
  - crowd entity present
  - YES already expensive
  - short-term YES momentum positive

### Entry logic
- Require `signals >= min_signals`
- Buy NO with patient GTC ladder near current NO price
- Enter only when spread/slippage/cooldown/budget gates pass

### Exit logic
- v0.1 is an entry-focused fade sleeve
- Exit automation can be added with explicit take-profit/time-stop rules

### Market selection
- Active Polymarket-imported World Cup/FIFA markets containing team/player entities

### Position sizing
- Fixed per-market cap `max_position_usd`
- Ladder split from `limit_splits`

### Risk controls
- `max_spread`, `max_slippage_pct`
- `cooldown_hours`
- `max_trades_per_run`
- `daily_budget_usd`
- optional context safeguards (disable with `--no-safeguards`)
