"""
PDF Redactor — selective PII redaction for text-based PDFs.

How redaction works
-------------------
This tool uses SELECTIVE redaction, not full-page flattening. The distinction
matters for downstream AI use:

- Selective: only the bounding-box rectangles that contain detected PII are
  removed from the content stream. The rest of the text layer stays intact.
  An AI reading the output can still extract text, parse tables, and follow
  document structure with full fidelity.

- Full-page flatten (not used here): the entire page is rasterised to an image.
  All structure is lost; any reader must OCR to get text back, which degrades
  accuracy on complex layouts.

PyMuPDF's page.apply_redactions() performs selective redaction — it removes
text only within the annotated rectangles and leaves everything else in the
content stream. This is the right approach for documents that need to remain
machine-readable after redaction.

Supported PII types (default)
------------------------------
  PERSON, EMAIL_ADDRESS, PHONE_NUMBER, DATE_TIME, LOCATION,
  NRP (national/regional/political), URL, IP_ADDRESS,
  DE_STREET_ADDRESS, DE_POSTAL_ADDRESS  (German-specific, when lang=de)

Usage
-----
  python redactor.py input.pdf output.pdf
  python redactor.py input.pdf output.pdf --language en
  python redactor.py input.pdf output.pdf --verify --verbose
  python redactor.py input.pdf output.pdf --threshold 0.6
  python redactor.py input.pdf output.pdf --entities PERSON EMAIL_ADDRESS
  python redactor.py --list-entities
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from typing import Optional

import fitz  # PyMuPDF

from presidio_analyzer import (
    AnalyzerEngine,
    Pattern,
    PatternRecognizer,
    RecognizerRegistry,
)
from presidio_analyzer.nlp_engine import NlpEngineProvider

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ENTITIES = [
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "DATE_TIME",
    "LOCATION",
    "NRP",
    "URL",
    "IP_ADDRESS",
]

GERMAN_EXTRA_ENTITIES = [
    "DE_STREET_ADDRESS",
    "DE_POSTAL_ADDRESS",
]

# Opaque black redaction fill
REDACT_FILL = (0, 0, 0)

# ---------------------------------------------------------------------------
# German custom recognizers
# ---------------------------------------------------------------------------


def _make_german_recognizers() -> list[PatternRecognizer]:
    """Return custom Presidio recognizers for German-specific PII patterns."""

    street_pattern = Pattern(
        name="de_street",
        regex=r"\b[A-ZÄÖÜ][a-zäöüß]+(straße|str\.|gasse|weg|allee|platz|ring|damm|ufer|chaussee)"
              r"\s+\d+\s*[a-zA-Z]?\b",
        score=0.65,
    )
    street_rec = PatternRecognizer(
        supported_entity="DE_STREET_ADDRESS",
        patterns=[street_pattern],
        supported_language="de",
    )

    postal_pattern = Pattern(
        name="de_postal",
        regex=r"\b\d{5}\s+[A-ZÄÖÜ][a-zäöüß]+(?:[\s-][A-ZÄÖÜ][a-zäöüß]+)*\b",
        score=0.70,
    )
    postal_rec = PatternRecognizer(
        supported_entity="DE_POSTAL_ADDRESS",
        patterns=[postal_pattern],
        supported_language="de",
    )

    return [street_rec, postal_rec]


# ---------------------------------------------------------------------------
# Analyzer factory
# ---------------------------------------------------------------------------


def build_analyzer(language: str = "de") -> AnalyzerEngine:
    """
    Build and return a Presidio AnalyzerEngine with spaCy NLP for the given
    language (plus English, which is always loaded).

    Loading spaCy models takes ~30 seconds the first time. Cache the returned
    analyzer and reuse it across calls for interactive use.
    """
    log.info("Building PII analyzer for languages: %s", ["en", language])

    models = [
        {"lang_code": "en", "model_name": "en_core_web_lg"},
    ]
    if language == "de":
        models.append({"lang_code": "de", "model_name": "de_core_news_lg"})

    provider = NlpEngineProvider(nlp_configuration={
        "nlp_engine_name": "spacy",
        "models": models,
    })
    nlp_engine = provider.create_engine()

    supported_langs = ["en"] if language == "en" else ["en", language]

    registry = RecognizerRegistry(supported_languages=supported_langs)
    registry.load_predefined_recognizers(nlp_engine=nlp_engine)

    if language == "de":
        for rec in _make_german_recognizers():
            registry.add_recognizer(rec)

    return AnalyzerEngine(
        registry=registry,
        nlp_engine=nlp_engine,
        supported_languages=supported_langs,
    )


# ---------------------------------------------------------------------------
# LLM-based PII detection (Ollama)
# ---------------------------------------------------------------------------

_LLM_PROMPT = {
    "de": """\
Du bist ein präziser PII-Detektor für deutsche Dokumente.

Analysiere den folgenden Text und identifiziere ALLE personenbezogenen Daten.
Gib NUR ein JSON-Array mit den exakten Zeichenketten aus dem Text zurück.

Einzuschließen:
- Namen echter Personen (vollständig oder teilweise)
- E-Mail-Adressen
- Telefonnummern (alle Formate)
- Physische Adressen (Straße, Stadt, Postleitzahl)
- Geburtsdaten von Personen
- Ausweis-, Pass- oder Steuernummern
- IBAN / Kontonummern
- Kfz-Kennzeichen

NICHT einzuschließen:
- Allgemeine Ortsnamen, die nicht Teil einer persönlichen Adresse sind
- Allgemeine Daten oder Zeiträume (z.B. „Januar 2024" als Berichtsdatum)
- Tabellenüberschriften, Spaltennamen oder sonstige Strukturelemente
- Währungsbeträge, Prozentsätze, allgemeine Zahlen
- Firmenbezeichnungen (außer sie identifizieren eine Einzelperson)

Text:
{text}

Antworte NUR mit einem gültigen JSON-Array, ohne Erklärung.
Beispiel: ["Max Mustermann", "max@email.de", "Musterstraße 12, 80331 München"]""",

    "en": """\
You are a precise PII (personally identifiable information) detector.

Analyze the following text and identify ALL personally identifiable information.
Return ONLY a JSON array of exact strings from the text.

Include:
- Names of real people (full or partial)
- Email addresses
- Phone numbers (any format)
- Physical addresses (street, city, postal code)
- Dates of birth
- ID numbers, passport numbers, tax IDs
- Bank account / IBAN numbers
- License plate numbers

Do NOT include:
- General location names not part of a personal address
- General dates or time periods (e.g. "January 2024" as a report date)
- Table headers, column names, or structural elements
- Currency amounts, percentages, or general numbers
- Company names (unless they identify a specific private individual)

Text:
{text}

Reply with ONLY a valid JSON array, no explanation.
Example: ["John Smith", "john@email.com", "123 Main St, Springfield"]""",
}


def check_ollama(model: str = "qwen2.5:7b") -> tuple[bool, str]:
    """
    Return (True, "") if Ollama is running and the model is available.
    Return (False, reason) otherwise.
    """
    try:
        import ollama as _ollama
        models_resp = _ollama.list()
        available = [m.model for m in models_resp.models]
        if not any(model in m for m in available):
            return False, (
                f"Model '{model}' not found in Ollama. "
                f"Run:  ollama pull {model}"
            )
        return True, ""
    except Exception as exc:
        return False, f"Ollama not reachable: {exc}"


def _llm_pii_strings(text: str, language: str = "de", model: str = "qwen2.5:7b") -> list[str]:
    """
    Send *text* to a local Ollama model and return the list of PII strings it identifies.
    Returns an empty list on any failure.
    """
    import json
    import ollama as _ollama

    prompt = _LLM_PROMPT.get(language, _LLM_PROMPT["en"]).format(text=text)

    try:
        response = _ollama.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0},
        )
        content = response.message.content.strip()
    except Exception as exc:
        log.warning("LLM call failed: %s", exc)
        return []

    # Extract JSON array from response (model may wrap it in markdown fences)
    match = re.search(r"\[.*?\]", content, re.DOTALL)
    if not match:
        log.warning("LLM returned no JSON array: %r", content[:200])
        return []

    try:
        items = json.loads(match.group())
        return [s for s in items if isinstance(s, str) and s.strip()]
    except json.JSONDecodeError as exc:
        log.warning("LLM JSON parse error: %s — raw: %r", exc, content[:200])
        return []


def llm_redact_page(
    page: fitz.Page,
    language: str = "de",
    model: str = "qwen2.5:7b",
    verbose: bool = False,
) -> tuple[int, set[str]]:
    """
    Detect PII on *page* using a local LLM and apply redaction annotations.

    Returns (redaction_count, set_of_redacted_strings).
    The caller must call page.apply_redactions() to commit the changes.
    """
    text = page.get_text()
    if not text.strip():
        return 0, set()

    pii_strings = _llm_pii_strings(text, language=language, model=model)
    if verbose:
        log.debug("  LLM identified %d PII item(s): %s", len(pii_strings), pii_strings)

    count = 0
    redacted_texts: set[str] = set()

    for pii in pii_strings:
        rects = page.search_for(pii)
        if not rects:
            # Try case-insensitive fallback
            rects = page.search_for(pii, flags=fitz.TEXT_DEHYPHENATE)
        if not rects:
            continue
        for rect in rects:
            annot = page.add_redact_annot(rect, fill=REDACT_FILL)
            if annot:
                count += 1
                redacted_texts.add(pii)
                if verbose:
                    log.debug("  [LLM] %r → %s", pii, rect)

    return count, redacted_texts


# ---------------------------------------------------------------------------
# Core redaction logic
# ---------------------------------------------------------------------------


def redact_page(
    page: fitz.Page,
    analyzer: AnalyzerEngine,
    language: str = "de",
    entities: Optional[list[str]] = None,
    score_threshold: float = 0.4,
    verbose: bool = False,
) -> tuple[int, set[str]]:
    """
    Detect PII on *page* and apply redaction annotations.

    Returns (redaction_count, set_of_redacted_strings).
    The caller must call page.apply_redactions() to commit the changes.
    """
    text = page.get_text()
    if not text.strip():
        return 0, set()

    if entities is None:
        entities = list(DEFAULT_ENTITIES)
        if language == "de":
            entities = entities + GERMAN_EXTRA_ENTITIES

    results = analyzer.analyze(
        text=text,
        language=language,
        entities=entities,
        score_threshold=score_threshold,
    )

    count = 0
    redacted_texts: set[str] = set()

    for result in results:
        entity_text = text[result.start:result.end].strip()
        if not entity_text:
            continue

        rects = page.search_for(entity_text)
        if not rects:
            continue

        for rect in rects:
            annot = page.add_redact_annot(rect, fill=REDACT_FILL)
            if annot:
                count += 1
                redacted_texts.add(entity_text)
                if verbose:
                    log.debug(
                        "  [%s %.2f] %r → %s",
                        result.entity_type,
                        result.score,
                        entity_text,
                        rect,
                    )

    return count, redacted_texts


def _verify_page(
    page: fitz.Page,
    redacted_texts: set[str],
    original_char_count: int,
) -> tuple[bool, str]:
    """
    Verify that:
      1. All redacted strings are gone from the text stream.
      2. The text layer was not catastrophically degraded (image-based PDF check).

    Returns (passed, message).
    """
    remaining_text = page.get_text()
    remaining_chars = len(remaining_text)

    # Check retention ratio
    if original_char_count > 0:
        retention = remaining_chars / original_char_count
    else:
        retention = 1.0

    if original_char_count > 50 and retention < 0.05:
        return False, (
            f"text layer retention dropped to {retention:.1%} "
            f"({original_char_count} → {remaining_chars} chars) — "
            "check whether the document was already image-based."
        )

    # Check PII is gone
    still_present = [t for t in redacted_texts if t in remaining_text]
    if still_present:
        return False, (
            f"PII string(s) still found in text stream after redaction: "
            f"{still_present}"
        )

    return True, (
        f"text layer {retention:.1%} retained "
        f"({original_char_count} → {remaining_chars} chars)"
    )


# ---------------------------------------------------------------------------
# Document-level entry point
# ---------------------------------------------------------------------------


def redact_pdf(
    input_path: str,
    output_path: str,
    analyzer: Optional[AnalyzerEngine] = None,
    language: str = "de",
    entities: Optional[list[str]] = None,
    score_threshold: float = 0.4,
    keep_metadata: bool = False,
    verify: bool = False,
    verbose: bool = False,
    llm_model: Optional[str] = None,
) -> dict:
    """
    Redact PII from *input_path* and write the result to *output_path*.

    If *analyzer* is None it is built internally (slow first call).
    Pass a pre-built analyzer to avoid re-loading spaCy models between calls.

    Returns a summary dict:
      {
        "total_redactions": int,
        "pages": int,
        "verification_passed": bool | None,
        "warnings": list[str],
      }

    Pass *llm_model* (e.g. "qwen2.5:7b") to use a local Ollama LLM instead of
    Presidio for PII detection. Requires Ollama to be running locally.
    """
    use_llm = llm_model is not None
    if not use_llm and analyzer is None:
        analyzer = build_analyzer(language)

    log.info("Opening: %s", input_path)
    doc = fitz.open(input_path)
    page_count = doc.page_count

    total = 0
    warnings: list[str] = []
    verification_passed: Optional[bool] = None

    for page_num in range(page_count):
        page = doc[page_num]
        log.info("Page %d/%d …", page_num + 1, page_count)

        original_text = page.get_text()
        original_char_count = len(original_text)

        if use_llm:
            count, redacted_texts = llm_redact_page(
                page,
                language=language,
                model=llm_model,
                verbose=verbose,
            )
        else:
            count, redacted_texts = redact_page(
                page,
                analyzer,
                language=language,
                entities=entities,
                score_threshold=score_threshold,
                verbose=verbose,
            )
        log.info("  %d redaction(s)", count)

        page.apply_redactions()
        total += count

        if verify:
            passed, msg = _verify_page(page, redacted_texts, original_char_count)
            log.info("  Verification: %s", msg)
            if not passed:
                w = f"Page {page_num + 1}: {msg}"
                log.warning("WARNING: %s", w)
                warnings.append(w)
                if verification_passed is not False:
                    verification_passed = False
            else:
                if verification_passed is None:
                    verification_passed = True

    if not keep_metadata:
        doc.set_metadata({})
        log.info("PDF metadata cleared")

    doc.save(output_path, garbage=4, clean=True, deflate=True)
    doc.close()

    log.info("Total redactions applied: %d", total)
    log.info("Output written to: %s", output_path)

    if verify and not warnings:
        log.info("Verification passed — PII removed, text layer intact.")

    return {
        "total_redactions": total,
        "pages": page_count,
        "verification_passed": verification_passed,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _list_entities(language: str = "de") -> None:
    entities = list(DEFAULT_ENTITIES)
    if language == "de":
        entities += GERMAN_EXTRA_ENTITIES
    print("Supported entity types:")
    for e in entities:
        print(f"  {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Selectively redact PII from text-based PDFs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", nargs="?", help="Input PDF path")
    parser.add_argument("output", nargs="?", help="Output PDF path")
    parser.add_argument(
        "--language", "-l",
        default="de",
        choices=["de", "en"],
        help="Primary document language (default: de)",
    )
    parser.add_argument(
        "--entities", "-e",
        nargs="+",
        metavar="ENTITY",
        help="Entity types to redact (default: all). Use --list-entities to see options.",
    )
    parser.add_argument(
        "--threshold", "-t",
        type=float,
        default=0.4,
        metavar="SCORE",
        help="Minimum confidence score (0.0–1.0, default: 0.4). "
             "Lower = more aggressive, higher = fewer false positives.",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify text layer integrity after each page redaction.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Log each detected entity with type, score, and bounding box.",
    )
    parser.add_argument(
        "--keep-metadata",
        action="store_true",
        help="Do not clear PDF metadata (author, title, dates, etc.).",
    )
    parser.add_argument(
        "--list-entities",
        action="store_true",
        help="List supported entity types and exit.",
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_entities:
        _list_entities(args.language)
        sys.exit(0)

    if not args.input or not args.output:
        parser.error("input and output PDF paths are required.")

    redact_pdf(
        input_path=args.input,
        output_path=args.output,
        language=args.language,
        entities=args.entities,
        score_threshold=args.threshold,
        keep_metadata=args.keep_metadata,
        verify=args.verify,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
