#!/bin/bash
# Railway / generic production start script
set -euo pipefail

echo "=== Starting NASDAQ Signal Bot ==="
echo "Python: $(python --version)"
echo "Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# Fail fast on syntax errors before starting the web process.
echo "Running Python compile check..."
python -m compileall -q src

# Verify the ASGI app imports cleanly before binding the port.
echo "Verifying ASGI app import..."
python -c "import src.webapp"

# Railway injects PORT automatically. Fall back to 8000 for local runs.
echo "Starting web server on port ${PORT:-8000}..."
exec uvicorn src.webapp:app --host 0.0.0.0 --port "${PORT:-8000}"
