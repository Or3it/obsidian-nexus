#!/usr/bin/env bash
# smoke.sh — Quick Phase 1 acceptance smoke test.
# Usage: bash scripts/smoke.sh [--vault /path/to/vault]
# Defaults to a temp vault if --vault is not given.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEXUS_DIR="$(dirname "$SCRIPT_DIR")"
PORT=7778

# Parse args
VAULT=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vault) VAULT="$2"; shift 2;;
    *) echo "Unknown arg: $1"; exit 1;;
  esac
done

if [[ -z "$VAULT" ]]; then
  VAULT="$(mktemp -d)/nexus-smoke-vault"
  CLEANUP_VAULT=1
else
  CLEANUP_VAULT=0
fi
mkdir -p "$VAULT"

echo "==> Nexus Phase 1 Smoke Test"
echo "    Vault: $VAULT"
echo "    Port:  $PORT"
echo ""

# ── Step 1: unit tests ──────────────────────────────────────────────────────
echo "[1/5] Running unit tests..."
cd "$NEXUS_DIR"
pytest tests/ -q --tb=short 2>&1
echo ""

# ── Step 2: write cascade.yaml ───────────────────────────────────────────────
echo "[2/5] Setting up vault config..."
mkdir -p "$VAULT/Orchestrator/Config"
cat > "$VAULT/Orchestrator/Config/cascade.yaml" <<'EOF'
schema_version: 2
rungs:
  - position: 1
    adapter: anthropic
    auth_mode: subscription
    use_ruflo: false
    models:
      default: claude-sonnet-4-6
      premium: claude-opus-4-7
      summariser: claude-haiku-4-5
jurisdictions: {}
EOF
echo "    cascade.yaml written (use_ruflo: false)"

# ── Step 3: start orchestrator ───────────────────────────────────────────────
echo "[3/5] Starting orchestrator on port $PORT..."
python -m orchestrator --vault "$VAULT" --port "$PORT" &
ORCH_PID=$!
trap "kill $ORCH_PID 2>/dev/null || true; [[ $CLEANUP_VAULT -eq 1 ]] && rm -rf \"$(dirname $VAULT)\"" EXIT

# Wait for health
for i in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:$PORT/health" > /dev/null 2>&1; then
    echo "    Orchestrator healthy (${i}x0.5s)"
    break
  fi
  sleep 0.5
  if [[ $i -eq 30 ]]; then
    echo "    ERROR: orchestrator did not become healthy"
    exit 1
  fi
done

TOKEN=$(cat "$VAULT/Orchestrator/Config/auth.token")

# ── Step 4: send a turn ──────────────────────────────────────────────────────
echo "[4/5] Sending test turn to Sonnet 4.6..."
RESPONSE=$(curl -sf \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user_message":"Say exactly: smoke test passed"}' \
  "http://127.0.0.1:$PORT/turn")

echo "    Raw SSE (first 500 chars):"
echo "${RESPONSE:0:500}"
echo ""

if echo "$RESPONSE" | grep -q "smoke test passed"; then
  echo "    [OK] Expected phrase found in response"
else
  echo "    [WARN] Expected phrase not found — check full response above"
fi

# ── Step 5: verify vault artefacts ──────────────────────────────────────────
echo "[5/5] Verifying vault artefacts..."

LIVE_COUNT=$(find "$VAULT/Orchestrator/Live" -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
LOG_COUNT=$(find "$VAULT/Orchestrator/Logs" -name "*.jsonl" 2>/dev/null | wc -l | tr -d ' ')

echo "    Live/*.md files:   $LIVE_COUNT"
echo "    Logs/*.jsonl files: $LOG_COUNT"

[[ "$LIVE_COUNT" -gt 0 ]] && echo "    [OK] Vault markdown written" || echo "    [FAIL] No markdown in Live/"
[[ "$LOG_COUNT" -gt 0 ]]  && echo "    [OK] JSONL log written"     || echo "    [FAIL] No JSONL in Logs/"

echo ""
echo "==> Smoke test complete. Run the full integration suite with:"
echo "    pytest tests/integration/ --real-claude -v"
