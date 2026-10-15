# Telegram initialization recovery

![Japanese sumi-e infographic showing shared request state blocking recovery, separate request pairs per attempt, and recovery with bounded old-app cleanup](https://raw.githubusercontent.com/JoaoMarcos44/hermes-agent/2dd9a166f509e60979a6824c1992add6a8816eac/infographic.png)

The gateway builds a Telegram application using two HTTP requests: one for API calls and one for polling. PTB's builder retains those request objects. Rebuilding only the application therefore carries the failed attempt's connection state into its successor. Waiting indefinitely for the old application's shutdown can also stop the retry ladder before the next attempt starts.

Each retry now gets fresh API and polling requests through the existing factory. Old-app shutdown uses the existing deadline helper, so recovery can advance while old work is still waiting. Delayed cleanup only touches the old request pair.

Two invariant tests exercise real PTB and HTTPX against a loopback Bot API. The timeout case injects a cancellation-resistant wait before a socket and holds old-app shutdown; it covers direct, proxy, and fallback request construction. The transient-error case checks that late cleanup leaves the new bot usable. All four cases fail on main `73456e11` and pass with the fix; the selected six-file suite passes 48 tests.

This proves the retry lifecycle defects and recovery under controlled faults. The original production trigger, its recurrence after process restarts, and the reporter's Linux aarch64 environment still require reporter-side confirmation.

The native image generator produced `infographic.png` using a Japanese sumi-e theme. The image is versioned on a separate documentation branch of the contributor fork, with the URL pinned to its commit. No GitHub Assets or release attachments are used. The prompt and local helper are stored beside the image in that branch. The code branch carries the report and decision ledger without adding a large binary to upstream clones.

See [the decision ledger](audit/issue-138649/decision-ledger.jsonl) for accepted and rejected hypotheses and replay evidence.
