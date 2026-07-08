"""Best-effort discovery of a model's context window from the inference endpoint.

The demo's local `omlx` server exposes each model's context window on the OpenAI-shape
`GET /v1/models` list as a non-standard `max_model_len` field. This probe reads it so the
history compactor's budget tracks the real window instead of a hand-set guess.

It is best-effort and degrades to the configured fallback on any failure: endpoint down,
model not listed, the window field absent or null, or an endpoint that doesn't serve the
field at all. The probe is therefore tightly coupled to the assumption that inference is
`omlx`: `omlx` keys the window as `max_model_len`, and the model-metadata layer is
OpenAI-shaped (there is no Anthropic-style models endpoint even though the same server
speaks the Anthropic *messages* API). The candidate key list also covers Anthropic's
`max_input_tokens` and other local servers' spellings, but a stock OpenAI endpoint that
omits the field always falls back to `AIVFS_CONTEXT_TOKENS`.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

#: Context-window field names seen across API families and local servers, most
#: authoritative first. `omlx` uses `max_model_len`; Anthropic's Models API uses
#: `max_input_tokens`.
_WINDOW_KEYS = ("max_input_tokens", "max_model_len", "max_context_length", "context_length", "context_window")


def resolve_context_window(
    base_url: str,
    model: str,
    api_key: str,
    *,
    fallback: int,
    timeout: float = 5.0,
) -> tuple[int, str]:
    """Return `(window_tokens, source)` for `model`, falling back to `fallback` on any failure.

    Probes the OpenAI-shape `GET {base_url}/models` list -- retrieve-by-id is not assumed,
    since the demo's server serves only the list -- and reads the first positive integer
    among `_WINDOW_KEYS` on the matching entry. `source` labels the origin for startup
    logging: `"endpoint:<key>"` on a hit, or `"fallback:<reason>"` otherwise.
    """
    try:
        response = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        response.raise_for_status()
        entries = response.json().get("data", [])
    except (httpx.HTTPError, ValueError) as exc:  # network error, non-2xx, or non-JSON body
        logger.info("context_window.probe_failed error=%r fallback=%d", exc, fallback)
        return fallback, f"fallback:{type(exc).__name__}"

    entry = next((e for e in entries if e.get("id") == model), None)
    if entry is None:
        return fallback, "fallback:model-not-listed"
    for key in _WINDOW_KEYS:
        value = entry.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value, f"endpoint:{key}"
    return fallback, "fallback:no-window-field"
