# lp-v2

Independent Polymarket liquidity-provider service (MVP).

Tracking parent: [open_trader#503](https://github.com/raymizzou/open_trader/issues/503).
M2: [lp-v2#3](https://github.com/raymizzou/lp-v2/issues/3), [PR #9](https://github.com/raymizzou/lp-v2/pull/9), and the [verification report](docs/M2_RESULT.md).
Original cloud-local roadmap reference: `~/ray-docs/任务流/PM-LP-V2-ROADMAP-t713u/PLAN_S10_LP_V2_MVP_t713u.md` (not bundled in this repository).

## What M2 implements

Four pure computations handle [scoring](src/lp_v2/scoring.py), [dual BUY quotes](src/lp_v2/quoting.py), [cost-based hedges](src/lp_v2/hedge.py) and [capital accounting](src/lp_v2/capital.py). They consume explicit data rather than contacting an exchange. The [ledger](src/lp_v2/ledger.py) tracks owned facts, uncertainty and recovery provenance. One [engine](src/lp_v2/engine.py) round returns immutable place/cancel/merge intents; it does not execute them.

[SDKPort](src/lp_v2/sdk_port.py) preserves the M2 execution contract for fakes. M3 adds a separate [read-only adapter](src/lp_v2/readonly_sdk.py) and [runner](src/lp_v2/readonly_runtime.py). The runner uses Engine.decide only for simulation and never submits its intents.

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

`--capital-usdc` is required, finite and positive. Startup freezes the cap and
UTC timestamp and serves **127.0.0.1:8765**. Stop with Ctrl-C. The independent
read-only runtime reads market data and proposes pairs every ten seconds;
scores refresh every ten minutes and wallet reads every thirty seconds.
Live execution is a separate explicit opt-in; configuration remains frozen during each run.

- GET `/dashboard` (or `/`): standalone page, polling `/summary` every five seconds.
- GET `/dashboard?demo=1`: explicit offline four-state demo.
- GET `/summary`: immediate schema 1 in-memory snapshot, exact money strings,
  real occupation separated from simulated proposal reserves, UNKNOWN for missing facts.
- GET `/healthz`: code SHA, frozen capital, wallet tail and region identity.
- Non-GET returns 405; unknown paths return 404. No arbitrary file serving.

Account configuration is explicit and optional; missing credentials leave wallet
UNKNOWN. The default read-only path does not load a private key or sign orders. No credential discovery is supported.
Read the [M3 runtime and deployment draft guide](docs/M3_READ_ONLY.md) for
conservative market exclusions, authentication, freshness and future operator
steps. Deployment artifacts are drafts; no live run or 24-hour acceptance is
claimed. Browser visual QA remains separate from offline behavior verification.

## M4 explicit live entry (draft)

The separate `run --live --market <condition_id>` path (repeat `--market` for up to three approved markets) connects the existing engine
and ledger to the pinned SDK through an append-only SQLite journal. It includes
owned-order drain/stop, restart reconciliation, receipt-gated Proxy merge, and
actual UTC income in the existing dashboard. Read the [M4 operator guide](docs/M4_LIVE.md)
for the explicit signing/L2/relayer file boundaries, Linux process requirements,
accounting definitions, UNKNOWN behavior and remaining operational verification.
The [live unit](deploy/lp-v2-live.service) and [launcher](deploy/run-live) are drafts;
they provision nothing and do not replace the read-only deployment path.
The M5 extension freezes one to three markets in one executor, preserves M4 state and global limits,
and records per-market occupation and income. The read-only `lp-v2 report --state-dir … --day YYYY-MM-DD --out …`
command writes daily CSV/JSON with explicit UNKNOWN reasons and safety evidence; see the [M5 report guide](docs/M5_REPORT.md).
No deployment, live operation, key provisioning or field acceptance is claimed.

Do not place trades or deploy until Ray explicitly authorizes each milestone.

【LLM：Codex@Air｜模型=gpt-6-astra｜effort=xhigh｜fast=否】
