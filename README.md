# lp-v2

Independent Polymarket liquidity-provider service (MVP).

Tracking parent: [open_trader#503](https://github.com/raymizzou/open_trader/issues/503).
M2: [lp-v2#3](https://github.com/raymizzou/lp-v2/issues/3), [PR #9](https://github.com/raymizzou/lp-v2/pull/9), and the [verification report](docs/M2_RESULT.md).
Original cloud-local roadmap reference: `~/ray-docs/任务流/PM-LP-V2-ROADMAP-t713u/PLAN_S10_LP_V2_MVP_t713u.md` (not bundled in this repository).

## What M2 implements

Four pure computations handle [scoring](src/lp_v2/scoring.py), [dual BUY quotes](src/lp_v2/quoting.py), [cost-based hedges](src/lp_v2/hedge.py) and [capital accounting](src/lp_v2/capital.py). They consume explicit data rather than contacting an exchange. The [ledger](src/lp_v2/ledger.py) tracks owned facts, uncertainty and recovery provenance. One [engine](src/lp_v2/engine.py) round returns immutable place/cancel/merge intents; it does not execute them.

[SDKPort](src/lp_v2/sdk_port.py) defines the boundary, and tests use a scripted fake. No real SDK adapter, signing or trading loop is wired. Startup exposes only a read-only local status service with an empty ledger.

## Install and test

Use Python **3.12+**. From the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

Tests prohibit socket networking, including loopback. HTTP tests exercise the actual parser/handler through memory streams and server boundary fakes. The [report](docs/M2_RESULT.md) records the 730-case suite, financial invariants, independent scoring oracle, five observed CI timings and remaining limits.

## Run the read-only status service

```sh
.venv/bin/lp-v2 run --capital-usdc 100
```

`--capital-usdc` is required and must be finite and positive. Startup freezes that value and its UTC timestamp, initializes an empty ledger, and blocks while serving at fixed **127.0.0.1:8765**. Stop with Ctrl-C. No configuration file is reloaded and no exchange state is fetched.

- GET `http://127.0.0.1:8765/healthz`: versioned health JSON.
- GET `http://127.0.0.1:8765/summary`: frozen startup capital/time and current capital components as exact Decimal strings.
- Other valid methods return 405; unknown GET targets return 404. There are no mutation routes or external-bind option.

The loopback listener is the intentional production networking boundary; it was not started over real TCP during automated verification. Live exchange/network/trading validation is not claimed. CI timing success does not imply mandatory merge enforcement: `main` is unprotected, and external cloud Claude review remains pending. See the report for the exact evidence and scope.

Do not place trades or deploy until Ray explicitly authorizes each milestone.

【LLM：Codex@Air｜模型=gpt-6-astra｜effort=xhigh｜fast=否】
