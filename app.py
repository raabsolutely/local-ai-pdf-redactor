"""
Streamlit UI for the PDF Redactor.

Run with:  streamlit run app.py
"""

from __future__ import annotations

import io
import logging
import os
import tempfile

import streamlit as st

# ---------------------------------------------------------------------------
# Page config (must be first Streamlit call)
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="PDF Redactor",
    page_icon="🔏",
    layout="centered",
)

# ---------------------------------------------------------------------------
# Import redactor — provide a friendly error if dependencies are missing
# ---------------------------------------------------------------------------

try:
    from redactor import (
        build_analyzer, redact_pdf, check_ollama,
        DEFAULT_ENTITIES, GERMAN_EXTRA_ENTITIES,
    )
except ImportError as exc:
    st.error(
        f"Missing dependency: {exc}\n\n"
        "Run `pip install -r requirements.txt` and download the spaCy models:\n"
        "```\npython -m spacy download en_core_web_lg\n"
        "python -m spacy download de_core_news_lg\n```"
    )
    st.stop()

# ---------------------------------------------------------------------------
# Suppress noisy logs in the UI (they still go to the terminal)
# ---------------------------------------------------------------------------

logging.getLogger("presidio_analyzer").setLevel(logging.WARNING)
logging.getLogger("presidio_anonymizer").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Cached model loading — runs once per session
# ---------------------------------------------------------------------------


@st.cache_resource(show_spinner="Loading NLP models (first run only, ~30 s) …")
def get_analyzer(language: str):
    return build_analyzer(language)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

st.title("🔏 PDF Redactor")
st.caption("Runs entirely on your machine. No data leaves your computer.")

# --- File upload ---
uploaded = st.file_uploader("Drop a PDF here or click Browse", type=["pdf"])

st.divider()

# --- Mode selector ---
OLLAMA_MODELS = ["qwen2.5:7b", "qwen2.5:14b", "mistral:7b", "llama3.1:8b"]

mode = st.radio(
    "Detection mode",
    options=["standard", "ai"],
    format_func=lambda x: "Standard — fast, rule-based NLP" if x == "standard"
                          else "AI — local LLM, context-aware (requires Ollama)",
    horizontal=True,
    help="AI mode uses a local language model running in Ollama. "
         "It understands context, catches names that NLP misses, "
         "and avoids false positives in tables.",
)

llm_model: str | None = None
if mode == "ai":
    ollama_ok, ollama_msg = check_ollama(OLLAMA_MODELS[0])
    if not ollama_ok:
        st.warning(
            f"Ollama not ready: {ollama_msg}\n\n"
            "Install from **ollama.com**, then run `ollama pull qwen2.5:7b` in Terminal."
        )
    llm_model = st.selectbox("Model", OLLAMA_MODELS, index=0)

st.divider()

# --- Language + threshold ---
col1, col2 = st.columns(2)

with col1:
    language = st.radio(
        "Document language",
        options=["de", "en"],
        format_func=lambda x: "German" if x == "de" else "English",
        horizontal=True,
    )

with col2:
    threshold = st.slider(
        "Confidence threshold",
        min_value=0.1,
        max_value=1.0,
        value=0.4,
        step=0.05,
        disabled=(mode == "ai"),
        help="Only used in Standard mode. "
             "Lower = more aggressive, higher = fewer false positives.",
    )

# --- Entity selector (Standard mode only) ---
all_entities = list(DEFAULT_ENTITIES)
if language == "de":
    all_entities += GERMAN_EXTRA_ENTITIES

if mode == "standard":
    with st.expander("Entity types to redact (click to customise)"):
        selected_entities = st.multiselect(
            "Select entity types",
            options=all_entities,
            default=all_entities,
            label_visibility="collapsed",
        )
else:
    selected_entities = all_entities  # not used in AI mode

# --- Options row ---
col3, col4 = st.columns(2)
with col3:
    verify = st.checkbox("Verify text layer after redaction", value=True)
with col4:
    keep_metadata = st.checkbox("Keep PDF metadata", value=False)

st.divider()

# --- Redact button ---
redact_clicked = st.button(
    "Redact PDF",
    type="primary",
    disabled=uploaded is None,
    use_container_width=True,
)

# --- Processing ---
if redact_clicked and uploaded is not None:
    if mode == "standard" and not selected_entities:
        st.error("Select at least one entity type.")
        st.stop()

    if mode == "ai":
        analyzer = None
    else:
        analyzer = get_analyzer(language)

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, "input.pdf")
        output_path = os.path.join(tmpdir, "output.pdf")

        with open(input_path, "wb") as f:
            f.write(uploaded.getbuffer())

        log_lines: list[str] = []

        # Capture log output for the detail panel
        class _StreamHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                log_lines.append(self.format(record))

        handler = _StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)

        spinner_msg = (
            f"Redacting with {llm_model} … (LLM processes each page, allow ~10–30 s/page)"
            if mode == "ai" else "Redacting …"
        )

        try:
            with st.spinner(spinner_msg):
                summary = redact_pdf(
                    input_path=input_path,
                    output_path=output_path,
                    analyzer=analyzer,
                    language=language,
                    entities=selected_entities if selected_entities != all_entities else None,
                    score_threshold=threshold,
                    keep_metadata=keep_metadata,
                    verify=verify,
                    verbose=False,
                    llm_model=llm_model if mode == "ai" else None,
                )
        finally:
            root_logger.removeHandler(handler)

        # --- Results ---
        total = summary["total_redactions"]
        pages = summary["pages"]
        warnings = summary["warnings"]

        if warnings:
            for w in warnings:
                st.warning(w)

        if total == 0:
            st.info(
                f"No PII detected across {pages} page(s). "
                "If the document is scanned (image-based), add an OCR layer first."
            )
        else:
            if verify and summary["verification_passed"]:
                st.success(
                    f"✓ {total} redaction(s) applied across {pages} page(s). "
                    "Text layer verified intact."
                )
            else:
                st.success(f"✓ {total} redaction(s) applied across {pages} page(s).")

        # --- Download ---
        with open(output_path, "rb") as f:
            redacted_bytes = f.read()

        stem = os.path.splitext(uploaded.name)[0]
        st.download_button(
            label="⬇ Download redacted PDF",
            data=redacted_bytes,
            file_name=f"{stem}_redacted.pdf",
            mime="application/pdf",
            use_container_width=True,
        )

        # --- Log detail ---
        if log_lines:
            with st.expander("Details"):
                st.code("\n".join(log_lines), language=None)
