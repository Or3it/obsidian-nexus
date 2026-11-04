# P2a-T04 — Ollama Adapter (Rung 5 — Local Sovereignty Floor)

## Goal

Local Rung 5. When all four upstream rungs are exhausted, the conversation continues on a model running on the user's own machine via Ollama. No API key, no rate limits, no network — just compute.

## Read first

- `shared/adapter-protocol.md`
- `shared/cascade-contract.md`
- `OPEN_QUESTIONS.md`

## Verification step

1. Confirm Ollama is running locally:
   ```bash
   curl http://localhost:11434/api/tags
   ```
   Should return JSON listing installed models. If it fails, the user needs to run `ollama serve` (or set up the Ollama LaunchAgent — already done on this machine per Phase 1 testing).

2. Confirm available models:
   ```bash
   ollama list
   ```
   On the dev machine at the time: `gemma4:latest` and `qwen2.5-coder:7b`. The cascade.yaml default should pick whichever is best for general chat — `gemma4` is the safer default for general use.

3. Confirm streaming format. Send a streaming request:
   ```bash
   curl http://localhost:11434/api/chat -d '{
     "model":"gemma4:latest",
     "messages":[{"role":"user","content":"hi"}],
     "stream":true
   }' | head -5
   ```
   Output is line-delimited JSON, one object per line, final one has `"done":true`.

4. No external network calls. The adapter speaks only to `localhost:11434`.

Append findings to `OPEN_QUESTIONS.md`.

## Files to create or replace

```
nexus/orchestrator/adapters/ollama.py
nexus/tests/test_ollama_adapter.py
nexus/tests/fixtures/ollama_stream.txt
```

## `nexus/orchestrator/adapters/ollama.py`

```python
import asyncio
import json
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from ..types import NexusRequest, NexusResponse, Token, HealthStatus
from .base import (
    AdapterUnavailable,
    AdapterTimeout,
    AdapterContextOverflow,
)


class OllamaAdapter:
    name = "ollama"

    def __init__(
        self,
        model: str,                                # e.g. "gemma4:latest"
        base_url: str = "http://localhost:11434",
        context_window: int = 32_000,              # per-model; gemma4 is ~128K but practical use is lower
        max_output_tokens: int = 4096,
        timeout_seconds: int = 300,                # local can be slow, especially cold starts
        keep_alive: str = "-1",                    # keep model loaded across calls
        chars_per_token: float = 3.9,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.context_window = context_window
        self.max_output_tokens = max_output_tokens
        self.timeout = timeout_seconds
        self.keep_alive = keep_alive
        self.chars_per_token = chars_per_token

    async def complete_stream(self, request: NexusRequest) -> AsyncIterator[Token]:
        """
        POST /api/chat with stream=true. Newline-delimited JSON response.

        Trigger mapping:
          - Connection refused → AdapterUnavailable ("Ollama not running")
          - HTTP 404 with model-not-found body → AdapterUnavailable ("model not pulled")
          - HTTP 5xx → AdapterUnavailable
          - Timeout → AdapterTimeout
          - Prompt too large → AdapterContextOverflow
        """
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {
                "num_predict": request.max_output_tokens or self.max_output_tokens,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                async with client.stream("POST", f"{self.base_url}/api/chat", json=payload) as r:
                    if r.status_code == 404:
                        raise AdapterUnavailable(
                            f"Ollama model '{self.model}' not found. "
                            f"Run `ollama pull {self.model}` to install it."
                        )
                    if r.status_code >= 500:
                        raise AdapterUnavailable(f"Ollama returned {r.status_code}")
                    if r.status_code != 200:
                        body = await r.aread()
                        raise AdapterUnavailable(f"Ollama returned {r.status_code}: {body[:200]}")
                    async for line in r.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if "error" in chunk:
                            if "context" in chunk["error"].lower() or "too long" in chunk["error"].lower():
                                raise AdapterContextOverflow(chunk["error"])
                            raise AdapterUnavailable(chunk["error"])
                        content = chunk.get("message", {}).get("content", "")
                        if content:
                            yield Token(text=content, is_final=False)
                        if chunk.get("done"):
                            yield Token(text="", is_final=True)
                            return
        except httpx.ConnectError as e:
            raise AdapterUnavailable(
                f"Cannot reach Ollama at {self.base_url}. Is `ollama serve` running?"
            ) from e
        except httpx.TimeoutException as e:
            raise AdapterTimeout(str(e)) from e

    async def complete(self, request: NexusRequest) -> NexusResponse:
        chunks: list[str] = []
        started = datetime.now(timezone.utc)
        async for token in self.complete_stream(request):
            chunks.append(token.text)
        content = "".join(chunks)
        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        return NexusResponse(
            content=content,
            model_id=f"ollama:{self.model}",
            auth_mode="local",
            estimated_tokens=self.count_tokens(content),
            duration_ms=duration_ms,
            finish_reason="stop",
        )

    def count_tokens(self, text: str) -> int:
        return max(1, int(len(text) / self.chars_per_token))

    def available_context(self) -> int:
        return self.context_window

    def estimated_cost(self, request: NexusRequest) -> dict:
        return {"pro_quota_pct": 0.0, "usd": 0.0}

    async def health_check(self) -> HealthStatus:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                r = await client.get(f"{self.base_url}/api/tags")
            if r.status_code != 200:
                return HealthStatus(
                    healthy=False,
                    last_checked=datetime.now(timezone.utc),
                    detail=f"GET /api/tags returned {r.status_code}",
                )
            models = r.json().get("models", [])
            model_names = [m.get("name", "") for m in models]
            base_name = self.model.split(":")[0]
            if not any(self.model == n or n.startswith(f"{base_name}:") for n in model_names):
                return HealthStatus(
                    healthy=False,
                    last_checked=datetime.now(timezone.utc),
                    detail=f"Model '{self.model}' not pulled. Available: {model_names}",
                )
            return HealthStatus(healthy=True, last_checked=datetime.now(timezone.utc))
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            return HealthStatus(
                healthy=False,
                last_checked=datetime.now(timezone.utc),
                detail=f"Ollama unreachable: {e}",
            )
```

## Critical implementation notes

1. **`keep_alive: "-1"` keeps the model resident.** Otherwise Ollama unloads after 5 minutes of idle and the next request pays the cold-start tax (10–60s for a 9GB model). For Rung 5 — which is the fallback when nothing else works — that latency is the difference between "the system feels okay" and "the system feels broken." Worth the RAM cost.

2. **Health check should be cheap.** `GET /api/tags` returns instantly and tells us (a) Ollama is up, (b) the configured model is pulled. Don't actually generate tokens for a health check.

3. **No retries on connection errors.** If Ollama isn't running, it's not running. Raise immediately. The cascade engine doesn't have any more rungs to try after this one, but at least the error is honest.

4. **`Token(text="", is_final=True)` at end.** Match the contract other adapters use.

5. **Don't shell out to the `ollama` CLI.** Use the HTTP API. The CLI adds startup overhead and depends on the user's PATH.

## Tests

`tests/test_ollama_adapter.py` — use `respx`:

- `complete_stream` yields tokens in order from fixture line-delimited JSON.
- Connection refused raises `AdapterUnavailable` mentioning "ollama serve".
- HTTP 404 (model not pulled) raises `AdapterUnavailable` mentioning `ollama pull`.
- HTTP 5xx raises `AdapterUnavailable`.
- Streamed `{"error":"context length exceeded"}` raises `AdapterContextOverflow`.
- Timeout raises `AdapterTimeout`.
- `count_tokens("hello")` returns a sensible number.
- `health_check` returns `healthy=True` when /api/tags lists the model.
- `health_check` returns `healthy=False` with a useful detail when the model isn't pulled.

## Definition of done

- [ ] Verification step completed.
- [ ] `ollama.py` exists, conforms to the protocol.
- [ ] Tests pass against mocked HTTP.
- [ ] Manual smoke test against the real local Ollama with `gemma4:latest`:
  ```bash
  python -c "
  import asyncio
  from orchestrator.adapters.ollama import OllamaAdapter
  from orchestrator.types import NexusRequest, NexusMessage, StyleProfile
  a = OllamaAdapter(model='gemma4:latest')
  r = NexusRequest(thread_id='test', messages=[NexusMessage(role='user', content='Say hello in 5 words.')], style_profile=StyleProfile())
  print(asyncio.run(a.complete(r)))
  "
  ```
  Receives a streamed Gemma response.
- [ ] `ruff` and `mypy` clean.

## Definition of NOT done

- Don't auto-pull missing models. If the user's model isn't available, fail loudly with the `ollama pull X` instruction; don't silently download multi-GB weights.
- Don't add to `cascade.yaml` yet — P2a-T08.
- Don't implement the hardware-detected picker for choosing which Ollama model to use. That's Phase 5 onboarding work. Phase 2a just reads the model string from config.
