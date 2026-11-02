# M3 implementation verification

This candidate implements the independent read-only observer and accepted-mock
dashboard for issue #4. It preserves the M2 execution Protocol and all M2
financial/accounting behavior; the new adapter has no order, cancel, merge,
credential-derivation or signing client surface. Engine outputs remain proposals.
Real occupation and adapter-origin order/cancel counts stay zero in the offline
24-hour simulation.

Batch 02 corrects Main's reproduced scope-local gaps: sixty-second minimum safe
repricing with immediate unsafe withdrawal, known excluded scores and original
reasons, bounded single-flight dashboard polling and independent wall-clock
freshness, early in-memory health identity during a blocked catalog read, and
the obsolete Mock meta CSP that blocked the new same-origin script in Chrome.
The CSP remains restrictive; no external scripts or inline script execution
were enabled. Known metadata scores can survive a bad executable book, while
that market still has no proposal. Missing fee/reward score inputs remain UNKNOWN.

Verification commands use the assigned Python 3.12 venv:

```sh
.venv/bin/python -m pytest -q tests/test_status_server.py tests/test_cli.py tests/test_readonly_sdk.py tests/test_readonly_runtime.py tests/test_dashboard.py tests/test_deploy.py
.venv/bin/python -m pytest -q
bash -n deploy/deploy-lp-v2
git diff --check
```

The focused run covers the actual HTTP parser with memory streams, typed SDK
models/pagination through fake HTTP boundaries, stdlib-verified synthetic L2
HMAC, a full simulated day, offline Node browser-boundary harnesses, and fake
external deployment commands. The tests prohibit real socket networking.
Each new behavior case has retained RED/GREEN logs under the task worktree's
ignored `.task-evidence/`; batch 01 evidence applies to its prior candidate,
and batch 02 evidence to the corrected candidate.

Final full-gate result is recorded in the batch 02 handoff. Browser visual QA
and independent acceptance remain Main-owned; offline CSP assertions alone do
not claim a real browser pass. No actual exchange runtime, credentials, Git
publication, host deployment, live 24-hour acceptance, trading or issue closure
was performed by this implementation worker. Deployment files remain drafts;
see [runtime and operator limits](M3_READ_ONLY.md).
