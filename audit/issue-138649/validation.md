# Telegram initialization lifecycle evidence

Issue: https://github.com/NousResearch/hermes-agent/issues/138649

Baseline: `73456e11739ff1bf84f6d39859a6e55edb1afb92`.

## Confirmed defects

PTB's builder retains the explicitly supplied general and getUpdates requests. The old retry ladder called `builder.build()` with those same objects, then awaited `_shutdown_abandoned_app(old_app)` without a deadline. A slow shutdown could prevent the next initialize call entirely; a late old-app shutdown could close requests belonging to its successor.

The report's claimed shield-held `_sticky_lock` mechanism is not established: production's lock critical sections contain no await after acquisition. Replacing locks or resetting `_primary_closed` does not isolate request ownership. Persistence across fresh gateway processes is outside what this local replay proves.

## RED and GREEN

Two invariant tests, with four collected cases, use real PTB 22.8 and HTTPX 0.28.1 with an asyncio loopback Bot API. Fault injection occurs at the transport/shutdown boundary. It does not replace the retry ladder or deadline helper.

| Contract | Baseline | Fixed |
|---|---|---|
| Cancellation-resistant pre-socket work and held cleanup cannot block the next attempt: direct, proxy, fallback request construction | 3 cleanup timeouts | 3 pass |
| A transient-error rebuild owns both requests; late old cleanup leaves the successor bot usable | Shared-request assertion fails | Pass |

The final selected suite passed **48 tests in six files**, with **zero retries**:

```sh
scripts/run_tests.sh -j 2 --file-retries 0 \
  tests/plugins/platforms/telegram/test_init_isolation.py \
  tests/gateway/test_telegram_init_deadline.py \
  tests/gateway/test_telegram_connect.py \
  tests/gateway/test_telegram_closewait_limits.py \
  tests/gateway/test_telegram_fallback_pool_release.py \
  tests/gateway/test_telegram_network_reconnect.py -q
```

Runs used disposable source snapshots and temporary Hermes homes. TEMP/TMP, bytecode, generated files, and runner output stayed under the sandbox. Each run's source and output were removed on completion. The test environment was built through PM with dev/test groups and the Telegram extra. Worker count was capped at two. No bot credentials or external Telegram service were used.

The initial gateway-directory harness was invalid because its conftest installs Telegram mocks. It was moved under the plugin test tree. The proxy case wraps HTTPX's selected proxy mount so it actually injects the pre-socket fault. Neither harness error is counted as defect reproduction. A neighboring test also exposed unnecessary successor allocation on a non-retryable error; the retry flag now applies only after the retryability guard.

The final harness uses two-second injected deadlines and a twelve-second outer bound, with event-based release, to leave headroom on busy CI workers. The same four baseline failures were reconfirmed after widening those bounds.

`python scripts/check --commit 29b66442 --base 73456e11` passed all **12 checks**, with **0 blocking and 0 advisory** code-health findings. The check runner's concurrency was capped at two. Native Git needed the checkout-local `core.longpaths=true` setting to materialize the isolated snapshot; no system setting changed. Final documentation and timing-only changes are checked again separately before publication.

## Related work and scope

PR https://github.com/NousResearch/hermes-agent/pull/122639 also introduces fresh retry requests and addresses cancellation/final-attempt cleanup. Its head `ad50c15d` still directly awaits old-app shutdown in retry `finally`. This draft adds the missing bounded-progress invariant and exercises late cleanup against real PTB clients. Maintainers can consolidate the overlapping request-isolation change with that PR.

The retry ladder lives in `plugins/platforms/telegram/telegram_connect.py`; the existing adapter method remains the entry point. The existing factory retains proxy, fallback, polling instrumentation, pool limits, and TLS construction. No core tools, prompt state, dependencies, or configuration settings are added.

Reporter-side replay on Linux aarch64 under the original pressure/reconnect conditions remains necessary to establish the original production trigger. This work is a draft patch for proven lifecycle faults, with that limit explicit.

## Capturing the missing production evidence

The gateway's existing faulthandler watchdog dumps thread stacks when the loop itself stops processing its deadline. That does not locate an asyncio wait while the loop remains healthy, which is the reported signature.

On an initialization timeout the retry ladder now emits `Telegram initialization await chain:` followed by up to 32 suspended Python frames from that initialization task. It captures them synchronously before cancellation or detached cleanup gets another loop tick. Each entry contains only the source basename, line number, and function name. Task representations, source lines, request URLs, frame locals, and other tasks are not inspected or emitted. Traversal ends at an opaque future rather than following unrelated tasks.

The existing real-PTB timeout invariant now also checks that the captured chain contains the injected transport and event wait, and excludes the dummy token and URL. On `a6dcf299`, the new diagnostic assertion failed after recovery succeeded; with the diagnostic, all **48 selected tests passed again**, with no retries. The direct, proxy, and fallback cases all verify the diagnostic.

Remaining evidence: the `Telegram initialization await chain:` line from a naturally failing attempt on the affected gateway. The controlled fault is not evidence that the production hang is in the same primitive. No new production stack or issue/PR reply was available when this follow-up was prepared.

## Infographic

Native-generated `infographic.png`, Japanese sumi-e theme. Visually reviewed; the recovery arrow was corrected to originate at the successful successor. Public immutable raw URL verified as HTTP 200, `image/png`, 2,623,507 bytes. The file is versioned in the contributor fork's separate documentation branch, keeping the upstream code PR below its 500 KiB binary limit and avoiding GitHub Assets uploads.

Diagnostic follow-up gates on `cc03c5b9`: ruff, code health, and blob-size all passed; code health reported zero blocking and zero advisory findings. The final ledger publication changes no tested production or test source.

## Review artifact placement

A PR comment by whyyagswhy independently confirmed the four isolation cases on `a6dcf299`, after installing the declared PTB dependency, and flagged these working reports in the code diff. The ledger and reports now live with the infographic in the contributor fork documentation branch, linked from the PR body. Local requested files remain available. This relocation changes no implementation or test source and does not add production evidence.

The follow-up publication update adds an explicit [View infographic](https://raw.githubusercontent.com/JoaoMarcos44/hermes-agent/2dd9a166f509e60979a6824c1992add6a8816eac/infographic.png) link to the PR body, retains its embedded preview, and normalizes the body's line endings to LF. The image remains pinned to artifact commit `2dd9a166f509e60979a6824c1992add6a8816eac` on `docs/telegram-init-138649-infographic`; it was never committed on the PR code branch. Reports were removed from the final code diff by `17924ff5`, while their earlier commits remain intact. The supporting documentation explicitly records this distinction.

Publication acceptance checks: PR remains open and draft at code head `17924ff5cadd479c069adf9281fec941f14d0d8a`; its diff contains only `plugins/platforms/telegram/adapter.py`, `plugins/platforms/telegram/telegram_connect.py`, and `tests/plugins/platforms/telegram/test_init_isolation.py`; direct image and documentation URLs resolve publicly; the GitHub-rendered body includes the image and explicit link. The image blob and all production/test blobs stay unchanged. These are publication checks; the previously recorded 48-test result still applies to the unchanged source.
