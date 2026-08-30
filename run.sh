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

# Run the FastAPI app with uvicorn (venv first, then system python)
if [ -x ".venv/bin/python" ]; then
    ./.venv/bin/python -m uvicorn app.main:app --port "$PORT"
elif command -v python3 &> /dev/null; then
    python3 -m uvicorn app.main:app --port "$PORT"
elif command -v python &> /dev/null; then
    python -m uvicorn app.main:app --port "$PORT"
else
    echo "[ERROR] Python was not found in your PATH."
    echo "Please install Python and the dependencies in requirements.txt."
    exit 1
fi
