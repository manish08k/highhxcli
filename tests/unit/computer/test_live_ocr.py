"""Real tesseract OCR of the real screen. Opt-in: HIGHHX_TEST_OCR=1 (needs tesseract, a
screenshot tool and, on macOS, Screen Recording permission for the terminal)."""

from __future__ import annotations

import os
import shutil

import pytest

from highhx.computer.desktop import TesseractOCR

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HIGHHX_TEST_OCR") and shutil.which("tesseract")),
    reason="set HIGHHX_TEST_OCR=1 with tesseract installed to run real OCR",
)


def test_real_screen_ocr_reads_text() -> None:
    ocr = TesseractOCR()
    assert ocr.capability().available
    observation = ocr.read_screen()
    assert observation.elements, "OCR returned no text for the current screen"
    assert all(e.role == "text" and e.source == "ocr" for e in observation.elements)
