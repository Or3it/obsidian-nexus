# P2c-T01 — Error Classification + Real Health Check

## Goal

Fix the root cause of the 2026-05-29 incident: dead models passing the health check and 404s being reported as "rate-limited." After this ticket, the OpenRouter adapter correctly distinguishes three failure modes and the health check actually verifies a specific model responds.

## Read first

- `nexus/orchestrator/adapters/openrouter.py` (lines ~250-257 classify errors; line ~173 health_check)
- `nexus/orchestrator/adapters/base.py` (the exception types: AdapterUnavailable, AdapterRateLimit, AdapterQuotaExhausted)
- `OPEN_QUESTIONS.md` — the cascade-final entry and the 2026-05-29 incident (add an entry for this if not present)

## The three failure modes

| Upstream response | Meaning | Exception | Cooldown |
|---|---|---|---|
| 404, or 200 with body `"Model not found"` / `"Provider returned error"` | This specific model/provider is down | `AdapterModelUnavailable` (new) | per-model, short (handled by ModelHealth in T03) |
| 429 with `quota`/`daily`/`limit` in body | Daily free quota exhausted (account-wide) | `AdapterQuotaExhausted` | shared key `openrouter_shared`, long |
| 429 without those keywords | Per-minute throttle | `AdapterRateLimit` | per-model, ~45s |
| 5xx | Transient server error | `AdapterRateLimit` (treat as brief) | per-model, ~30s |

Note the subtlety found on 2026-05-29: OpenRouter can return **HTTP 200 with an error object in the body** (`{"error":{"message":"Provider returned error","code":404,...}}`). The classifier must inspect the body, not only the HTTP status.

## Files to modify

```
nexus/orchestrator/adapters/base.py          ← add AdapterModelUnavailable
nexus/orchestrator/adapters/openrouter.py    ← fix _classify_error + health_check
nexus/tests/test_openrouter_adapter.py       ← new classification + health tests
```

## `base.py` — new exception

```python
class AdapterModelUnavailable(AdapterError):
    """The specific model/provider is unavailable (404 / provider error).
    Distinct from rate-limit and quota: the model itself is down, not throttled.
    Carries an optional cooldown_seconds for how long to skip this model."""
    def __init__(self, message: str, cooldown_seconds: int = 300):
        super().__init__(message)
        self.cooldown_seconds = cooldown_seconds
```

## `openrouter.py` — error classification

Replace the current status-only logic. Inspect both HTTP status and body:

```python
def _classify_error(self, status_code: int, body: dict | str) -> AdapterError:
    """Classify an OpenRouter failure. Inspects HTTP status AND response body,
    because OpenRouter returns 200-with-error-object for some provider failures."""
    # Normalise the error message and any embedded code from the body
    msg, embedded_code = _extract_error(body)   # helper below
    lowered = msg.lower()

    effective_status = embedded_code or status_code

    # Model/provider unavailable — 404 anywhere, or provider-error text
    if effective_status == 404 or "model not found" in lowered \
            or "provider returned error" in lowered or "no allowed providers" in lowered:
        return AdapterModelUnavailable(
            f"OpenRouter model unavailable: {msg}", cooldown_seconds=300
        )

    # Quota exhausted (account-wide, shared)
    if effective_status == 429 and ("quota" in lowered or "daily" in lowered or "limit" in lowered):
        return AdapterQuotaExhausted(
            f"OpenRouter daily quota exhausted: {msg}", cooldown_key=_COOLDOWN_KEY
        )

    # Per-minute rate limit
    if effective_status == 429:
        return AdapterRateLimit(f"OpenRouter rate limited: {msg}", cooldown_seconds=45)

    # Transient server error
    if 500 <= effective_status < 600:
        return AdapterRateLimit(f"OpenRouter server error {effective_status}: {msg}",
                                cooldown_seconds=30)

    return AdapterUnavailable(f"OpenRouter error {effective_status}: {msg}")


def _extract_error(body: dict | str) -> tuple[str, int | None]:
    """Pull a human message and any embedded numeric code out of an OpenRouter
    error body. Handles both {'error': {...}} and provider 'metadata.raw' JSON."""
    if isinstance(body, str):
        return body, None
    err = body.get("error", {}) if isinstance(body, dict) else {}
    msg = err.get("message", "") or ""
    code = err.get("code")
    # Provider sometimes nests the real reason in metadata.raw (a JSON string)
    meta = err.get("metadata", {})
    raw = meta.get("raw")
    if raw:
        try:
            import json
            inner = json.loads(raw)
            inner_msg = inner.get("error", {}).get("message")
            if inner_msg:
                msg = f"{msg} ({inner_msg})" if msg else inner_msg
        except Exception:
            msg = f"{msg} {raw}".strip()
    return msg, (int(code) if isinstance(code, int) else None)
```

Make sure the streaming and non-streaming completion paths both route failures through `_classify_error`, including the **200-with-error-body** case (check the parsed body for an `"error"` key even on HTTP 200 before treating it as success).

## `openrouter.py` — real health check

The current `health_check` does `GET /models` and reports healthy if the API is reachable. Change it to verify the **specific configured model** with a minimal probe — but make the probe optional/cheap so it isn't run constantly:

```python
async def health_check(self, *, deep: bool = False) -> HealthStatus:
    """
    Shallow (default): GET /models, confirm the API is reachable AND the
    configured model id appears in the catalogue. Cheap, no completion quota.
    Deep (deep=True): send a 1-token completion to confirm the provider
    actually serves it. Costs one request — only call on the recovery edge (T05).
    """
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"{self._base_url}/models",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        if r.status_code != 200:
            return HealthStatus(healthy=False, detail=f"models endpoint {r.status_code}")
        ids = {m["id"] for m in r.json().get("data", [])}
        if self.model not in ids:
            return HealthStatus(healthy=False, detail=f"{self.model} not in catalogue")
    except Exception as e:
        return HealthStatus(healthy=False, detail=f"unreachable: {e}")

    if not deep:
        return HealthStatus(healthy=True, detail="catalogue-listed")

    # Deep probe — 1 token, confirms the provider serves it right now
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            r = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.model,
                      "messages": [{"role": "user", "content": "hi"}],
                      "max_tokens": 1},
            )
        body = r.json()
        if "error" in body:
            err = self._classify_error(r.status_code, body)
            return HealthStatus(healthy=False, detail=str(err))
        return HealthStatus(healthy=True, detail="probe-ok")
    except Exception as e:
        return HealthStatus(healthy=False, detail=f"probe failed: {e}")
```

Important: the shallow check catching "model not in catalogue" would NOT have caught the 2026-05-29 case (the slug *was* in the catalogue but the provider 404'd on completion). That gap is exactly why deep probing exists and why the lazy mark-on-failure approach (T03/T04) is the primary mechanism — the health check is a secondary signal, not the source of truth.

## Tests

`tests/test_openrouter_adapter.py`:
- 404 status → `AdapterModelUnavailable`.
- HTTP 200 with `{"error":{"code":404,"message":"Provider returned error"}}` → `AdapterModelUnavailable` (the regression test for the 2026-05-29 bug).
- 200 with nested `metadata.raw` containing `"Model not found"` → `AdapterModelUnavailable`, message includes the inner reason.
- 429 + "daily limit reached" → `AdapterQuotaExhausted` with `openrouter_shared`.
- 429 + "rate limit exceeded" (no quota words) → `AdapterRateLimit`, cooldown_seconds≈45.
- 503 → `AdapterRateLimit`, cooldown_seconds≈30.
- `health_check(deep=False)`: model in catalogue → healthy; model absent → unhealthy.
- `health_check(deep=True)` with mocked error body → unhealthy with the classified reason.
- `_extract_error` handles str body, dict body, and nested metadata.raw.

## Definition of done

- [ ] `AdapterModelUnavailable` exists in base.py.
- [ ] `_classify_error` inspects body + status and handles 200-with-error-object.
- [ ] `health_check` supports shallow (default) and deep modes.
- [ ] All existing tests pass + new classification/health tests.
- [ ] Manual: re-run the 2026-05-29 probe against `minimax/minimax-m2.5:free`; confirm the adapter now classifies it as model-unavailable, not rate-limit. (If the provider has recovered, temporarily point at a known-bad slug to force the path.)
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't wire backoff/health-ledger yet — T03.
- Don't change selection logic yet — T04.
- Don't add the pool schema yet — T02.
- Don't call deep health checks anywhere automatically — that's T05's quota-sane edge case only.
