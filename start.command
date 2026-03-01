#!/bin/bash
# PDF Redactor launcher

cd "$(dirname "$0")"

VENV="$HOME/.venvs/pdf-redactor"
PYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"
STREAMLIT="$VENV/bin/streamlit"

echo "=== PDF Redactor ==="

# Create venv if missing (e.g. after OS update)
if [ ! -f "$PYTHON" ]; then
    echo "Setting up environment (one-time)..."
    arch -arm64 /usr/bin/python3 -m venv "$VENV"
    "$PIP" install --upgrade pip -q
fi

# Install/update packages if requirements changed
"$PIP" install -q -r requirements.txt

# Download spaCy models if missing
"$PYTHON" -c "import spacy; spacy.load('en_core_web_lg')" 2>/dev/null || {
    echo "Downloading English NLP model (one-time)..."
    "$PYTHON" -m spacy download en_core_web_lg
}
"$PYTHON" -c "import spacy; spacy.load('de_core_news_lg')" 2>/dev/null || {
    echo "Downloading German NLP model (one-time)..."
    "$PYTHON" -m spacy download de_core_news_lg
}

echo ""
echo "Starting PDF Redactor at http://localhost:8501"
echo "Press Ctrl+C or close this window to stop."
echo ""

"$STREAMLIT" run app.py --server.headless false --browser.gatherUsageStats false
