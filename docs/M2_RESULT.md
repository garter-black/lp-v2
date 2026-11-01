# M2 implementation and verification report

Tracking: [lp-v2#3](https://github.com/raymizzou/lp-v2/issues/3) and
[PR #9](https://github.com/raymizzou/lp-v2/pull/9).
Code/test baseline: [2ffe822988fc7ad80fde0ef0f7d88b5293596a07](https://github.com/raymizzou/lp-v2/commit/2ffe822988fc7ad80fde0ef0f7d88b5293596a07).
Evidence status: C12R1 authoring, 2026-11-02; later documentation commits do not change this implementation baseline.

PLAN steps 1–11 are implemented. Recorded C11 local verification passed **730 tests in 104.90 seconds**; the same source/test bytes are now in the baseline commit above. CI history remains mixed: the original C10 run timed out, R1 passed functionally but took 307 seconds overall, and R2's earlier-phase run took 262 seconds. Main has now verified C11 CI [41348615146](https://github.com/raymizzou/lp-v2/actions/runs/41348615146) at the baseline head: **SUCCESS, 730 collected and 730 passed**, with pytest step 152 seconds / job 166 seconds / run 169 seconds. Pytest itself reported 150.56 seconds, a different measure from the step duration. The final five document-commit runs remain pending and Main-owned; C11 success does not establish that five-run gate. Issue #3 remains open; no merge-ready or completed external-review claim is made.

## 1. Scope and operating boundaries

M2 supplies the CLI, pure scoring/quoting/hedging/capital calculations, an in-memory ledger, a synchronous decision engine, a scripted fake SDK and read-only status service. Business source totals **1,450 lines** under `src/lp_v2`, excluding tests. The [engine](../src/lp_v2/engine.py) produces intents; it does not execute an exchange trading loop.

The [CLI](../src/lp_v2/cli.py) validates a finite positive startup capital and freezes that amount and its UTC timestamp. It starts an **empty** ledger and the [status service](../src/lp_v2/status_server.py), bound only to literal `127.0.0.1:8765`. GET `/healthz` and `/summary` return versioned JSON; summary money uses exact Decimal strings. Other valid methods return 405; unknown GET targets return 404. There is no runtime configuration reload or mutation route.

The stdlib loopback listener is the deliberate status-only networking boundary. No real SDK adapter, exchange network client, signing, live orders/cancellations, dashboard, deployment or live-use acceptance is included. [SDKPort](../src/lp_v2/sdk_port.py) is an interface, not an integration. No OpenTrader code or runtime is imported or changed.

## 2. Automated behavior acceptance

The recorded C11 affected suite passed **98 tests in 0.10 seconds**. Its full run retained all 703 earlier cases and added 27 status/CLI cases, for 730 passing tests. This report-only change does not rerun or replace that evidence.

| Public checks | Verified behavior |
| --- | --- |
| [Quoting](../tests/test_quoting.py), [hedging](../tests/test_hedge.py) | Post-only BUY prices respect ticks, asks, minimum sizes and the 0.02 pair cushion. The literal example of 10 YES costing 4.10, with 0.01 remaining fee per pair, caps the NO hedge at 0.56. Existing/pending coverage is not counted twice. |
| [Capital](../tests/test_capital.py), [ledger](../tests/test_ledger.py) | Exact component sums; fills conserve occupation; UNKNOWN/unconfirmed cancellation and unreceived merges cannot release funds. Confirmed unused coverage or reconciled actual merge receipts can release it. Ownership, identity, atomic rejection and no-replay negatives remain enforced. |
| [Ordered scenarios](../tests/test_scenarios.py) | 14 named tests / 24 variants cover one-sided and partial fills, late fills, UNKNOWN, pending hedges, merge outcomes, rank loss, restart, the 60-second replacement rule, sequential admission and the 24-hour limit on new allocation while recovery continues. |
| [CLI](../tests/test_cli.py), [HTTP status](../tests/test_status_server.py) | Invalid input cannot construct a server; startup config stays frozen. Nonzero pending/filled examples and subsequent snapshots serialize exact amounts. Mutation-looking payloads/queries cannot change state. |

The [random invariant harness](../tests/test_capital_random.py) retains **500 fixed seeds, 20261102–20261601**, with **101,339 events**, **200–376 per seed**, three markets and at least two allocation generations per market. Every counted event checks capital components/cap, release restrictions and duplicate coverage/merge protection against actual fake exchange facts, including an independent inventory-cost-plus-live-BUY lower bound. Required event families and per-seed counts survived both timing repairs unchanged; failure tapes and exact seed replay are retained.

HTTP tests run the real stdlib parser/handler over memory streams with server-construction/serving boundary fakes. The unconditional [socket guard](../tests/conftest.py) and its [prohibition test](../tests/test_no_network.py) remain intact. **No actual TCP listener was started during testing**, including loopback; these results are not live transport acceptance.

## 3. Data, oracle and recovery provenance

[Market fixtures](../tests/data/markets_snapshot.json) contain **20 real political records and 17 explicitly labelled synthetic rows**. Capture metadata preserves source identifiers, timestamps, hashes and the fields changed in synthetic controls. Missing captured `inPlay` stays UNKNOWN/ineligible; these historical fixtures do not establish current live eligibility.

The [scoring implementation](../src/lp_v2/scoring.py) derives from
[warproxxx/poly-maker at 4f32103591c9582ccd012bdf10f77d86e5879444](https://github.com/warproxxx/poly-maker/blob/4f32103591c9582ccd012bdf10f77d86e5879444/src/polymaker/catalog/scoring.py).
The [frozen expectations](../tests/data/scores_expected.json) were generated once from the original AST: only the unavailable domain import was replaced with a data stand-in; all five function ASTs and MarketScore were unchanged, with no `lp_v2` imports. [Scoring tests](../tests/test_scoring.py) compare the independent frozen values and verify source/snapshot provenance. The complete upstream MIT notice is retained in [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md).

[Recovery](../src/lp_v2/ledger.py) requires authentic, complete ownership and intent history, fills, cumulative order proof, cancellation-attempt provenance and matching consumed-position/USDC merge receipts. A wallet delta alone is not settlement proof. Unresolved UNKNOWN exposure survives recovery, foreign/old LP holdings are excluded, and a lower restart cap blocks new allocation without discarding covered recovery. Fee assumptions in tests are explicit inputs, not verified live exchange fees. The CLI status process does not fetch or reconstruct exchange state itself.

## 4. Independent CI structure and timing evidence

The [workflow](../.github/workflows/ci.yml) has one `tests` job on Python 3.12, a 10-minute job timeout, PRs targeting `main` and pushes to `main`. It installs `.[test]` from [pyproject.toml](../pyproject.toml) and runs the full `pytest` suite. Declared runtime dependencies are `polymarket-client==0.2.0` and `websockets`, with `pytest` for tests. There is no OpenTrader workflow, test result or source dependency, and no timeout/trigger change was used to obtain faster results.

CI evidence supplied and verified by Main:

| Phase / source | Run | Functional result and timing |
| --- | --- | --- |
| C10 / `b85aeba` | [41344749051](https://github.com/raymizzou/lp-v2/actions/runs/41344749051) | Cancelled at the 10-minute limit; pytest consumed 600 seconds. Timeout is not an assertion-failure conclusion. |
| C10R1 / `aa2e58e` | [41346365023](https://github.com/raymizzou/lp-v2/actions/runs/41346365023) | SUCCESS, but job 303 seconds / run 307 seconds: the 300-second timing target still failed. |
| C10R2 / `6dcd69a` | [41347628777](https://github.com/raymizzou/lp-v2/actions/runs/41347628777) | SUCCESS; pytest 237 seconds / job 258 seconds / run 262 seconds. This is phase improvement, not the final five-run gate. |
| C11 / `2ffe822` | [41348615146](https://github.com/raymizzou/lp-v2/actions/runs/41348615146) | SUCCESS; 730 collected/passed. Pytest step 152 seconds / job 166 seconds / run 169 seconds; pytest-reported duration 150.56 seconds. |

The first three runs precede the full step11 suite. Main verified the C11 run against the full baseline head `2ffe822988fc7ad80fde0ef0f7d88b5293596a07`; its 730-case result is confirmed, not pending. Five consecutive complete-suite runs on the full 730-case implementation, each within 300 seconds, remain to be collected from meaningful report commits. Main owns those pushes/runs and may let the independent runs execute concurrently. Step13 will add verified links and timings; none are invented here.

Workflow independence is separate from mandatory merge enforcement. Main's readback at **2026-11-01T20:47:02.694540+00:00** found `main` **unprotected**, required status-check enforcement off and active rules `[]`. Consequently there is **no server-side required-check gate**. No repository settings were changed, and a workflow success alone does not authorize merge or deployment.

【LLM：Codex@Air｜模型=gpt-6-astra｜effort=xhigh｜fast=否】
