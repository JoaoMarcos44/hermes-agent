from __future__ import annotations


def _coerce_timeout(raw: object, *, allow_zero: bool = False) -> float | None:
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return None
    if timeout < 0 or (timeout == 0 and not allow_zero):
        return None
    return timeout


def _lookup_provider_timeout(
    provider_id: str,
    model: str | None,
    model_key: str,
    provider_key: str,
    *,
    allow_zero: bool = False,
) -> float | None:
    """Read a timeout from ``providers.<id>`` config, model override first."""
    if not provider_id:
        return None

    try:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    except Exception:
        return None

    providers = config.get("providers", {}) if isinstance(config, dict) else {}
    provider_config = (
        providers.get(provider_id, {}) if isinstance(providers, dict) else {}
    )
    if not isinstance(provider_config, dict):
        return None

    model_config = _get_model_config(provider_config, model)
    if model_config is not None:
        timeout = _coerce_timeout(
            model_config.get(model_key), allow_zero=allow_zero
        )
        if timeout is not None:
            return timeout

    return _coerce_timeout(
        provider_config.get(provider_key), allow_zero=allow_zero
    )


def get_provider_request_timeout(
    provider_id: str, model: str | None = None
) -> float | None:
    """Return a configured provider request timeout in seconds, if any."""
    return _lookup_provider_timeout(
        provider_id, model, "timeout_seconds", "request_timeout_seconds"
    )


def get_provider_stale_timeout(
    provider_id: str, model: str | None = None
) -> float | None:
    """Return a configured non-stream stale timeout in seconds, if any."""
    return _lookup_provider_timeout(
        provider_id, model, "stale_timeout_seconds", "stale_timeout_seconds"
    )


def get_provider_max_call_timeout(
    provider_id: str, model: str | None = None
) -> float | None:
    """Return a configured wall-clock ceiling for one streaming call, if any.

    Unlike the stale timeouts above — which measure *silence* and reset on
    every scrap of provider activity — this bounds the total time a single
    request may run.  A provider that keeps dripping bytes outlives every
    activity-based guard we have (issue #83657: one 1239s call).
    """
    return _lookup_provider_timeout(
        provider_id,
        model,
        "max_call_seconds",
        "max_call_seconds",
        allow_zero=True,
    )


def _get_model_config(
    provider_config: dict[str, object], model: str | None
) -> dict[str, object] | None:
    if not model:
        return None

    models = provider_config.get("models", {})
    model_config = models.get(model, {}) if isinstance(models, dict) else {}
    if isinstance(model_config, dict):
        return model_config
    return None
