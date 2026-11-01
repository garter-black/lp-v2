# M2 implementation and verification report

Tracking: [lp-v2#3](https://github.com/raymizzou/lp-v2/issues/3) and
[PR #9](https://github.com/raymizzou/lp-v2/pull/9).
Code/test baseline: [2ffe822988fc7ad80fde0ef0f7d88b5293596a07](https://github.com/raymizzou/lp-v2/commit/2ffe822988fc7ad80fde0ef0f7d88b5293596a07).
Evidence status: C13 final evidence, 2026-11-02. The five report commits end at [dfaebf409a6cdfe29bbf2203989c34ffa57c91bd](https://github.com/raymizzou/lp-v2/commit/dfaebf409a6cdfe29bbf2203989c34ffa57c91bd), with the same source/tests as the implementation baseline above.

PLAN steps 1–11 are implemented, and the observed step12 five-run timing window **PASS** is documented below. All five runs passed the complete **730-case suite**; whole-run times were **255 / 296 / 294 / 297 / 290 seconds**. Recorded C11 local verification passed **730 tests in 104.90 seconds**. Historical timeout/overrun evidence remains in section 4. Mandatory merge enforcement is **NOT CLAIMED** and external review is pending; issue #3 remains open. These results do not establish merge readiness, approval, deployment or live use.

## 1. Scope and operating boundaries

M2 supplies the CLI, pure scoring/quoting/hedging/capital calculations, an in-memory ledger, a synchronous decision engine, a scripted fake SDK and read-only status service. `wc -l src/lp_v2/*.py` reports **1,450 total lines**, excluding tests, within the approximately 3,000-line business-source target. The [engine](../src/lp_v2/engine.py) produces intents; it does not execute an exchange trading loop.

The [CLI](../src/lp_v2/cli.py) validates a finite positive startup capital and freezes that amount and its UTC timestamp. It starts an **empty** ledger and the [status service](../src/lp_v2/status_server.py), bound only to literal `127.0.0.1:8765`. GET `/healthz` and `/summary` return versioned JSON; summary money uses exact Decimal strings. Other valid methods return 405; unknown GET targets return 404. There is no runtime configuration reload or mutation route.

The stdlib loopback listener is the deliberate status-only networking boundary. No real SDK adapter, exchange network client, signing, live orders/cancellations, dashboard, deployment or live-use acceptance is included. [SDKPort](../src/lp_v2/sdk_port.py) is an interface, not an integration. No OpenTrader code or runtime is imported or changed.

## 2. Acceptance checklist ①–⑤

The recorded C11 affected suite passed **98 tests in 0.10 seconds**. Its full run retained all 703 earlier cases and added 27 status/CLI cases, for 730 passing tests. The final five CI runs use those same source/test bytes. PASS below applies to the measured automated contract; live validation and unconfigured enforcement are excluded explicitly.

- [x] **① CI structure — PASS.** One Python 3.12 `tests` job, 10-minute timeout, editable test install and full pytest. Declared dependencies remain `polymarket-client==0.2.0`, `websockets` and test dependency `pytest`; no added dependency or cross-repository job.
- [x] **① Five-run timing — PASS.** Five consecutive report-only commits each collected/passed 730 cases; both job and entire-run durations were ≤300 seconds. Exact heads, links and durations are in section 4.
- [x] **① Independence and test isolation — PASS.** No OpenTrader source/test/workflow dependency. The unconditional [socket guard](../tests/conftest.py) covers all tests, with a [prohibition test](../tests/test_no_network.py).
- [ ] **① Mandatory merge enforcement — NOT CLAIMED.** `main` is unprotected and active rules are empty. The single workflow is not an enforced server-side required check; this subitem is not PASS and no settings were changed.
- [x] **② Independent scoring — PASS.** [20 real political records plus 17 labelled synthetic controls](../tests/data/markets_snapshot.json), original-source AST-generated [frozen expectations](../tests/data/scores_expected.json), exact score/component comparisons, hard-filter controls and retained MIT attribution. Source identity and UNKNOWN limitations are in section 3.
- [x] **③ Ordered scenarios — PASS.** [14 named tests / 24 variants](../tests/test_scenarios.py) assert actual intents and exact occupation for one-sided fills, both-side partial fills, late fills after cancellation, UNKNOWN cancellation, existing/pending opposite coverage, merge success/failure/timeout, rank loss and restart.
- [x] **③ Quote/hedge/engine constraints — PASS.** [Quotes](../tests/test_quoting.py) are post-only BUY, tick-aligned, below asks and respect reward/exchange minimums and the 0.02 pair cushion. [Hedges](../tests/test_hedge.py) use fee-inclusive cost and unique existing/pending coverage: 10 YES costing 4.10 with 0.01 remaining pair fee caps NO at 0.56. Scenarios preserve 60-second replacement spacing, cancellation proof before replacement, one-hour order lifetime, sequential admission and the 24-hour stop on new allocation while covered recovery continues.
- [x] **④ Capital conservation/admission — PASS.** [Capital](../tests/test_capital.py) and [ledger](../tests/test_ledger.py) checks preserve exact component sums, no double counting, no release on fills/uncertain cancellation/unreceived merges, receipt-based settlement and idempotent identities. Admission enforces total funding and per-market/global unpaired limits. Lower-cap restart prevents new opening while retaining covered recovery; foreign/old LP inventory is not attributed by token alone.
- [x] **④ Random invariants — PASS.** [500 fixed seeds 20261102–20261601](../tests/test_capital_random.py), **101,339 events**, **200–376 per seed**, three markets and at least two allocation generations per market. Every counted event checks components/cap, release restrictions and duplicate coverage/merge protection, including an independent actual inventory-cost-plus-live-BUY lower bound. Required event families, per-seed counts, failure tapes and exact seed replay remain preserved.
- [x] **⑤ Startup validation/config — PASS.** [CLI tests](../tests/test_cli.py) reject missing, nonnumeric, zero, negative, NaN and infinity capital with nonzero exits before server construction. Startup capital and UTC time are frozen; changing a setup file cannot reload the running config.
- [x] **⑤ Read-only HTTP — PASS within memory/boundary tests.** [Status tests](../tests/test_status_server.py) verify literal 127.0.0.1:8765 construction, GET health/summary 200 with current nonzero snapshot amounts, other valid methods 405 (including HEAD/arbitrary tokens), unknown GET 404, no mutation and clean interruption.

HTTP tests run the real stdlib parser/handler over memory streams with server-construction/serving boundary fakes. **No actual TCP listener was started during testing**, including loopback. Live socket, exchange/network, signing and trading acceptance are **NOT CLAIMED**; no real SDK adapter is wired.

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

The first three runs precede the full step11 suite. C11 verified all 730 cases at `2ffe822988fc7ad80fde0ef0f7d88b5293596a07`; it is phase evidence, separate from the final five consecutive runs below.

**Final five-run window: PASS.** Main verified consecutive branch runs, exact heads, parent chain, report-only diffs and the 730 collected/passed log lines. Each head uses identical source, tests, workflow and dependency declarations. Durations distinguish pytest's own timer from job and entire-run wall time.

| # | Exact commit | Run | Collected/passed | Pytest-reported s | Job s | Entire run s |
| --- | --- | --- | --- | ---: | ---: | ---: |
| 1 | [010e7b1](https://github.com/raymizzou/lp-v2/commit/010e7b18e62a7eb0782ea885d6016f19b79d8407) | [41350185858](https://github.com/raymizzou/lp-v2/actions/runs/41350185858) | 730 PASS | 234.88 | 251 | 255 |
| 2 | [569d60b](https://github.com/raymizzou/lp-v2/commit/569d60b7819797b5487481470e598b05dfb92d68) | [41350209685](https://github.com/raymizzou/lp-v2/actions/runs/41350209685) | 730 PASS | 274.29 | 292 | 296 |
| 3 | [da6ae1e](https://github.com/raymizzou/lp-v2/commit/da6ae1e851980fa87ed4bc178a7a1f89b8389e51) | [41350229152](https://github.com/raymizzou/lp-v2/actions/runs/41350229152) | 730 PASS | 271.86 | 290 | 294 |
| 4 | [adebecd](https://github.com/raymizzou/lp-v2/commit/adebecd890075016a94dd04ea69add8058e22c20) | [41350251165](https://github.com/raymizzou/lp-v2/actions/runs/41350251165) | 730 PASS | 272.36 | 292 | 297 |
| 5 | [dfaebf4](https://github.com/raymizzou/lp-v2/commit/dfaebf409a6cdfe29bbf2203989c34ffa57c91bd) | [41350276200](https://github.com/raymizzou/lp-v2/actions/runs/41350276200) | 730 PASS | 268.76 | 286 | 290 |

Source tree: `b6db5530a203e60eff341fc4745d183205cbb968`; tests tree: `b13cea8c010639fa1f289736432baa32b3c2f939`. The largest observed entire-run duration was **297 seconds**, leaving **3 seconds** of the 300-second budget. This proves the measured window, not a guarantee for every future runner. Earlier failures remain above rather than being replaced by this successful window.

Main owns the final documentation commit's separate exact-head CI and will record it in the task result/PR body, avoiding a self-referential report-commit loop.

Workflow independence is separate from mandatory merge enforcement. Main's readback at **2026-11-01T20:47:02.694540+00:00** found `main` **unprotected**, required status-check enforcement off and active rules `[]`. Consequently there is **no server-side required-check gate**. No repository settings were changed, and a workflow success alone does not authorize merge or deployment.

## 5. Reproduction and remaining review

The recorded local runner was Python **3.12.12** / pytest **8.4.2**. From the repository root, create an environment if needed and run:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

The focused CLI/status/accounting/guard scope is:

```sh
.venv/bin/python -m pytest -q tests/test_cli.py tests/test_status_server.py tests/test_capital.py tests/test_ledger.py tests/test_no_network.py
```

A single deterministic financial sequence can be reproduced with:

```sh
.venv/bin/python -m pytest -q 'tests/test_capital_random.py::test_random_capital_invariants[20261102]'
```

`.venv/bin/lp-v2 run --capital-usdc 100` starts only the blocking local status service described above, with an empty ledger; it does not start trading. That listener was not exercised over live TCP in this evidence.

The five-run evidence and final README/report are complete. Main owns the final documentation commit, its CI observation and conversion/publication as a normal reviewable PR for cloud Claude. External review remains pending. This report does not claim cloud Claude has reviewed it, that all gates are complete, or that the PR is merge-ready. The approved stopping point is a reviewable PR awaiting that external review, with issue #3 still open. Merge, deployment, live use and issue closure are outside this delivery.

【LLM：Codex@Air｜模型=gpt-6-astra｜effort=xhigh｜fast=否】
