#!/bin/bash
# Railway / generic production start script
set -euo pipefail

echo "=== Starting NASDAQ Signal Bot ==="
echo "Python: $(python --version)"
echo "Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# The YAML intentionally leaves the main symbols list empty because the
# mid-cap scanner discovers its own universe. The dashboard/indicator loop,
# however, still needs a small always-on market context. Respect an explicit
# Railway SYMBOLS variable; otherwise provide safe defaults.
export SYMBOLS="${SYMBOLS:-NQ=F,QQQ,SPY,IWM,NVDA,AMD}"
echo "Core symbols: ${SYMBOLS}"

# Fail fast on syntax errors before starting the web process.
echo "Running Python compile check..."
python -m compileall -q src

# Verify the Railway entrypoint (including Weekly Focus membership lock) imports
# cleanly before binding the port.
echo "Verifying Railway ASGI entrypoint..."
python -c "import src.railway_entry"

# Railway injects PORT automatically. Fall back to 8000 for local runs.
echo "Starting web server on port ${PORT:-8000}..."
exec uvicorn src.railway_entry:app --host 0.0.0.0 --port "${PORT:-8000}"
