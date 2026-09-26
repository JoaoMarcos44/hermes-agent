# Timeout vs Connection Classification

## Failure chain to inspect

```text
stream/provider
  -> exception type + status + message
  -> auxiliary transport classifier
  -> summary failure classifier
  -> terminal flags / cooldown counters
  -> compress abort or fallback
  -> overflow / session lifecycle
```

## Precedence rule

Transport code may intentionally treat a timeout as connection-like for failover. Compression policy must not infer that every connection-like exception is a terminal network failure.

Compute the canonical timeout class first, then exclude it from the terminal connection class:

```python
timeout = canonical_timeout_classifier(exc) or existing_summary_timeout_cases
streaming_closed = canonical_connection_classifier(exc) and not timeout
```

Do not add broad words such as `stalled` to the timeout taxonomy when the actual owner raises a typed `TimeoutError`.

## Regression matrix

- typed no-progress `TimeoutError`: timeout=true, terminal-network=false;
- real connection drop: timeout=false, terminal-network=true;
- legacy `timed out` text: stays timeout-class;
- real compressor state: timeout streak increments, terminal network flag remains false;
- default compression policy: no terminal abort solely because the timeout helper also counts as connection-like.

## Review smell

Collection-time workarounds for one contributor checkout (preloading stdlib modules, private Pydantic plugin loaders, global environment mutation) are test-isolation defects, not product fixes. Move the regression into a focused test surface instead.
