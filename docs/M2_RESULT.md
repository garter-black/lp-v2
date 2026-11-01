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

【LLM：Codex@Air｜模型=gpt-6-astra｜effort=xhigh｜fast=否】
