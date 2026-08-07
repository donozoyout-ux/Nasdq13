#!/bin/bash
# Render start script
set -e

echo "=== Starting NASDAQ Signal Bot ==="
echo "Python: $(python --version)"
echo "Time: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# Install dependencies if needed
if [ ! -f ".deps_installed" ]; then
    echo "Installing dependencies..."
    pip install --no-cache-dir -r requirements.txt
    touch .deps_installed
fi

# Start the bot
echo "Starting signal bot..."
python -m src.main
