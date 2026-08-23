#!/usr/bin/env bash

# Navigate to script directory
cd "$(dirname "$0")"

echo "==================================================="
echo " Starting GitHub Student Analytics Dashboard..."
echo " Opening browser at: http://localhost:8501"
echo "==================================================="
echo ""

# Automatically open browser on Mac / Linux in background
if [[ "$OSTYPE" == "darwin"* ]]; then
    (sleep 2 && open "http://localhost:8501") &
elif command -v xdg-open &> /dev/null; then
    (sleep 2 && xdg-open "http://localhost:8501") &
fi

# Run Streamlit with live console logs & non-headless mode
if command -v streamlit &> /dev/null; then
    streamlit run app.py --server.headless false --server.port 8501
elif command -v python3 &> /dev/null; then
    python3 -m streamlit run app.py --server.headless false --server.port 8501
elif command -v python &> /dev/null; then
    python -m streamlit run app.py --server.headless false --server.port 8501
else
    echo "[ERROR] Neither Streamlit nor Python was found in your PATH."
    exit 1
fi
