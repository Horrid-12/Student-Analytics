#!/usr/bin/env bash

# Navigate to script directory
cd "$(dirname "$0")"

PORT=8001

echo "==================================================="
echo " Starting GitHub Student Analytics Dashboard..."
echo " Opening browser at: http://localhost:$PORT"
echo "==================================================="
echo ""

# Automatically open browser on Mac / Linux in background
if [[ "$OSTYPE" == "darwin"* ]]; then
    (sleep 2 && open "http://localhost:$PORT") &
elif command -v xdg-open &> /dev/null; then
    (sleep 2 && xdg-open "http://localhost:$PORT") &
fi

# Run the FastAPI app with a usable runtime (venv first, then system Python).
PYTHON_CMD=""
if [ -x ".venv/bin/python" ] && .venv/bin/python -c "import uvicorn" >/dev/null 2>&1; then
    PYTHON_CMD=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1 && python3 -c "import uvicorn" >/dev/null 2>&1; then
    PYTHON_CMD="python3"
elif command -v python >/dev/null 2>&1 && python -c "import uvicorn" >/dev/null 2>&1; then
    PYTHON_CMD="python"
fi

if [ -z "$PYTHON_CMD" ]; then
    echo "[ERROR] A Python runtime with uvicorn was not found."
    echo "Install the dependencies with: python -m pip install -r requirements.txt"
    exit 1
fi

echo "Using Python runtime: $PYTHON_CMD"
exec "$PYTHON_CMD" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --reload
