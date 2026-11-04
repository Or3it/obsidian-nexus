# Manual Smoke Test — Anthropic Adapter (P1-T05)

Run from `nexus/nexus/` with the venv active and `claude` authenticated via `claude /login`.

## Single-shot `complete()`

```python
import asyncio
from orchestrator.adapters.anthropic import AnthropicAdapter
from orchestrator.types import NexusMessage, NexusRequest, StyleProfile

adapter = AnthropicAdapter()  # use_ruflo=False, model=claude-sonnet-4-6

request = NexusRequest(
    thread_id="smoke-001",
    messages=[NexusMessage(role="user", content="Reply with only the word: pong")],
    style_profile=StyleProfile(),
)

response = asyncio.run(adapter.complete(request))
print(response.content)        # expected: pong
print(response.model_id)       # claude-sonnet-4-6
print(response.auth_mode)      # subscription
print(response.estimated_tokens)
print(response.duration_ms)
```

One-liner form:

```bash
python -c "
import asyncio
from orchestrator.adapters.anthropic import AnthropicAdapter
from orchestrator.types import NexusMessage, NexusRequest, StyleProfile
a = AnthropicAdapter()
r = NexusRequest(thread_id='s', messages=[NexusMessage(role='user', content='Reply: pong')], style_profile=StyleProfile())
resp = asyncio.run(a.complete(r))
print(repr(resp.content))
"
```

## Streaming `complete_stream()`

```python
import asyncio
from orchestrator.adapters.anthropic import AnthropicAdapter
from orchestrator.types import NexusMessage, NexusRequest, StyleProfile

async def stream():
    adapter = AnthropicAdapter()
    request = NexusRequest(
        thread_id="smoke-002",
        messages=[NexusMessage(role="user", content="Count to 3, one number per line.")],
        style_profile=StyleProfile(),
    )
    async for token in adapter.complete_stream(request):
        if not token.is_final:
            print(token.text, end="", flush=True)
    print()  # newline after stream

asyncio.run(stream())
```

## Health check

```python
import asyncio
from orchestrator.adapters.anthropic import AnthropicAdapter
print(asyncio.run(AnthropicAdapter().health_check()))
# expected: healthy=True, detail=''
```

## Expected results

- `complete()` returns the model's reply in `content`; `auth_mode="subscription"`.
- `complete_stream()` streams tokens; the final `Token(text="", is_final=True)` terminates the loop.
- `health_check()` returns `HealthStatus(healthy=True, ...)` when `claude` is on PATH and authenticated.
- `apiKeySource: "none"` in the first stream-json event confirms Pro subscription mode (check with `--verbose` output visible in logs at DEBUG level).

---

# Manual Smoke Test — Google Adapter (P2a-T02)

Run from `nexus/nexus/` with the venv active and `GOOGLE_AI_STUDIO_API_KEY` exported (or present in `.env`).

**Note:** `gemini-3.1-pro-preview` (the default Rung-2 model) has free-tier quota=0 on this account and will raise `AdapterQuotaExhausted` immediately. Use `gemini-flash-latest` for smoke tests until a paid tier or different project with pro quota is available. See OPEN_QUESTIONS.md [2026-05-16].

## single-shot `complete()` — gemini-flash-latest

```python
import asyncio, os
from orchestrator.adapters.google import GoogleAdapter
from orchestrator.types import NexusRequest, NexusMessage, StyleProfile

# Load .env if running interactively
with open('.env') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k] = v

a = GoogleAdapter(model='gemini-flash-latest')
r = NexusRequest(
    thread_id='smoke-google-001',
    messages=[NexusMessage(role='user', content='Say hello in 5 words.')],
    style_profile=StyleProfile(),
)
resp = asyncio.run(a.complete(r))
print('content:', repr(resp.content))
print('model_id:', resp.model_id)
print('auth_mode:', resp.auth_mode)
print('finish_reason:', resp.finish_reason)
```

## Observed output (2026-05-16, google-genai v2.3.0)

```
content: 'Hello, how are you today?'
model_id: gemini-flash-latest
auth_mode: api_key_free_tier
estimated_tokens: 8
finish_reason: stop
duration_ms: 2997
```

## quota-exhausted path (default model)

```python
import asyncio, os
from orchestrator.adapters.google import GoogleAdapter
from orchestrator.adapters.base import AdapterQuotaExhausted
from orchestrator.types import NexusRequest, NexusMessage, StyleProfile

with open('.env') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k] = v

a = GoogleAdapter()  # default: gemini-3.1-pro-preview, quota=0 on free tier
r = NexusRequest(
    thread_id='smoke-google-002',
    messages=[NexusMessage(role='user', content='hi')],
    style_profile=StyleProfile(),
)
try:
    asyncio.run(a.complete(r))
except AdapterQuotaExhausted as e:
    print('Expected AdapterQuotaExhausted:', str(e)[:120])
```

## Expected results

- `complete()` with `gemini-flash-latest` returns a real Gemini response, `auth_mode="api_key_free_tier"`.
- `complete()` with default model (`gemini-3.1-pro-preview`) raises `AdapterQuotaExhausted` (free-tier quota=0).
- `health_check()` with default model returns `healthy=False` with the quota error in `detail`.
- `health_check()` with `gemini-flash-latest` returns `healthy=True`.

---

# Manual Smoke Tests — OpenRouter Adapter (P2a-T03)

Run from `nexus/nexus/` with the venv active and `OPENROUTER_API_KEY` exported.

**Current verified slugs (2026-05-16):**
- Rung 2: `minimax/minimax-m2.5:free`
- Rung 3: `z-ai/glm-4.5-air:free`
- Rung 4: `qwen/qwen3-coder:free`

## Rung 2 — MiniMax M2.5 (streaming)

```python
import asyncio, os
from orchestrator.adapters.openrouter import OpenRouterAdapter
from orchestrator.types import NexusRequest, NexusMessage, StyleProfile

with open('.env') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k] = v

a = OpenRouterAdapter(model='minimax/minimax-m2.5:free')
r = NexusRequest(
    thread_id='smoke-or-rung2',
    messages=[NexusMessage(role='user', content='Say hello in exactly 5 words.')],
    style_profile=StyleProfile(),
    max_output_tokens=200,
)

async def stream():
    async for token in a.complete_stream(r):
        if not token.is_final:
            print(token.text, end='', flush=True)
    print()

asyncio.run(stream())
```

## Rung 3 — GLM 4.5 Air (single-shot)

```python
import asyncio, os
from orchestrator.adapters.openrouter import OpenRouterAdapter
from orchestrator.types import NexusRequest, NexusMessage, StyleProfile

with open('.env') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k] = v

a = OpenRouterAdapter(model='z-ai/glm-4.5-air:free', max_output_tokens=500)
r = NexusRequest(
    thread_id='smoke-or-rung3',
    messages=[NexusMessage(role='user', content='Say hello in exactly 5 words.')],
    style_profile=StyleProfile(),
    max_output_tokens=500,
)
resp = asyncio.run(a.complete(r))
print('content:', repr(resp.content))
print('model_id:', resp.model_id)
print('auth_mode:', resp.auth_mode)
print('finish_reason:', resp.finish_reason)
```

**Note:** GLM 4.5 Air is a reasoning model. With `max_tokens` below ~200, the model may be cut off
during internal reasoning before producing visible content (`content: null`). Use `max_output_tokens=500`.

## Rung 4 — Qwen3-Coder (single-shot)

```python
import asyncio, os
from orchestrator.adapters.openrouter import OpenRouterAdapter
from orchestrator.types import NexusRequest, NexusMessage, StyleProfile

with open('.env') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k] = v

a = OpenRouterAdapter(model='qwen/qwen3-coder:free', max_output_tokens=200)
r = NexusRequest(
    thread_id='smoke-or-rung4',
    messages=[NexusMessage(role='user', content='Write a one-line Python hello world.')],
    style_profile=StyleProfile(),
    max_output_tokens=200,
)
resp = asyncio.run(a.complete(r))
print('content:', repr(resp.content))
print('finish_reason:', resp.finish_reason)
```

## Fallback model smoke test

```python
import asyncio, os
from orchestrator.adapters.openrouter import OpenRouterAdapter
from orchestrator.adapters.base import AdapterUnavailable
from orchestrator.types import NexusRequest, NexusMessage, StyleProfile

with open('.env') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k] = v

# Primary slug is invalid; should fall through to the real fallback
a = OpenRouterAdapter(
    model='invalid/slug-that-does-not-exist:free',
    fallback_models=['minimax/minimax-m2.5:free'],
)
r = NexusRequest(
    thread_id='smoke-or-fallback',
    messages=[NexusMessage(role='user', content='hi')],
    style_profile=StyleProfile(),
    max_output_tokens=50,
)

async def stream():
    async for token in a.complete_stream(r):
        if not token.is_final:
            print(token.text, end='', flush=True)
    print()

asyncio.run(stream())
# Expected: response from minimax/minimax-m2.5:free (the fallback)
```

## Health check (Rung 2)

```python
import asyncio, os
from orchestrator.adapters.openrouter import OpenRouterAdapter

with open('.env') as f:
    for line in f:
        line = line.strip()
        if '=' in line and not line.startswith('#'):
            k, v = line.split('=', 1)
            os.environ[k] = v

a = OpenRouterAdapter(model='minimax/minimax-m2.5:free')
print(asyncio.run(a.health_check()))
# Expected: healthy=True, detail=''
```

## Expected results

- Rung 2 (MiniMax M2.5): streams visible content only; reasoning tokens (`delta.reasoning`) are
  silently skipped and never appear in output. `auth_mode="api_key_free_tier"`.
- Rung 3 (GLM 4.5 Air): same reasoning-token suppression; use `max_output_tokens>=500`.
- Rung 4 (Qwen3-Coder): streams content; may also produce reasoning tokens (suppress as above).
- Fallback: when primary slug is invalid (HTTP 400), the adapter falls through to `fallback_models[0]`
  and produces a real response without raising.
- `health_check()` probes `GET /models` and returns `healthy=True` when the key is valid.
- Rate-limit (HTTP 429 without quota keyword) → `AdapterRateLimit` (transient).
- Daily quota (HTTP 429 with "limit"/"daily"/"quota") → `AdapterQuotaExhausted` with
  `cooldown_key="openrouter_shared"` (cascade will cool all three OpenRouter rungs together).

---

# Manual Smoke Tests — Ollama Adapter (P2a-T04)

Run from `nexus/nexus/` with the venv active. Requires Ollama running at `http://localhost:11434`.

## Rung 5 — gemma4:latest (single-shot)

```python
import asyncio
from orchestrator.adapters.ollama import OllamaAdapter
from orchestrator.types import NexusRequest, NexusMessage, StyleProfile

a = OllamaAdapter(model='gemma4:latest')
r = NexusRequest(
    thread_id='smoke-ol-01',
    messages=[NexusMessage(role='user', content='Say hello in 5 words.')],
    style_profile=StyleProfile(),
    max_output_tokens=30,
)
resp = asyncio.run(a.complete(r))
print('content:', repr(resp.content))
print('model_id:', resp.model_id)
print('auth_mode:', resp.auth_mode)
print('finish_reason:', resp.finish_reason)
print('estimated_tokens:', resp.estimated_tokens)
print('duration_ms:', resp.duration_ms)
```

**Observed output (2026-05-16, warm model):**
```
content: 'Hello there, how are you doing?'
model_id: ollama:gemma4:latest
auth_mode: local
finish_reason: stop
estimated_tokens: 7
duration_ms: 2482
```

## Rung 5 — gemma4:latest (streaming)

```python
import asyncio
from orchestrator.adapters.ollama import OllamaAdapter
from orchestrator.types import NexusRequest, NexusMessage, StyleProfile

async def stream():
    a = OllamaAdapter(model='gemma4:latest')
    r = NexusRequest(
        thread_id='smoke-ol-02',
        messages=[NexusMessage(role='user', content='Count to 3, one number per line.')],
        style_profile=StyleProfile(),
        max_output_tokens=50,
    )
    async for token in a.complete_stream(r):
        if not token.is_final:
            print(token.text, end='', flush=True)
    print()

asyncio.run(stream())
```

## Health check

```python
import asyncio
from orchestrator.adapters.ollama import OllamaAdapter
a = OllamaAdapter(model='gemma4:latest')
print(asyncio.run(a.health_check()))
# Expected: healthy=True, detail=''
```

## Model-not-pulled error path

```python
import asyncio
from orchestrator.adapters.ollama import OllamaAdapter
from orchestrator.adapters.base import AdapterUnavailable
from orchestrator.types import NexusRequest, NexusMessage, StyleProfile

a = OllamaAdapter(model='nonexistent-model:latest')
r = NexusRequest(
    thread_id='smoke-ol-err',
    messages=[NexusMessage(role='user', content='hi')],
    style_profile=StyleProfile(),
)
try:
    asyncio.run(a.complete(r))
except AdapterUnavailable as e:
    print('Expected AdapterUnavailable:', str(e))
    # Expected message mentions 'ollama pull nonexistent-model:latest'
```

## Expected results

- `complete()` returns model response in `content`; `auth_mode="local"`, `model_id="ollama:gemma4:latest"`.
- `complete_stream()` streams NDJSON tokens; final `Token(text="", is_final=True)` terminates the loop.
- `health_check()` returns `healthy=True` when `gemma4:latest` is pulled.
- `health_check()` returns `healthy=False` with "ollama pull" hint when model is missing.
- Model-not-pulled → `AdapterUnavailable` with `ollama pull <model>` instruction.
- Ollama not running (ConnectError) → `AdapterUnavailable` with "ollama serve" instruction.
- Cold start (first request after restart): ~25s latency due to loading 9.6 GB model.
  With `keep_alive=-1` (default), subsequent requests are warm (~2.5s).
- **Note:** `keep_alive` must be integer `-1`, NOT string `"-1"`. Ollama's Go parser requires
  a unit suffix for string durations; integer -1 maps to "never unload".
