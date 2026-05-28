from __future__ import annotations

from .base import BaseExtractor, ExtractedFile, ExtractionResult
from .fallback import FallbackExtractor
from .govuk_generic import GovUKGenericExtractor

_GOVUK_ROOT = "https://www.gov.uk/"

# Singletons — extractors carry no per-instance state
_govuk    = GovUKGenericExtractor()
_fallback = FallbackExtractor()


def select_extractor(publication) -> BaseExtractor:
    """Return the appropriate extractor for a StatPublication."""
    if publication.url.startswith(_GOVUK_ROOT):
        return _govuk
    return _fallback


__all__ = [
    "select_extractor",
    "BaseExtractor",
    "ExtractedFile",
    "ExtractionResult",
    "GovUKGenericExtractor",
    "FallbackExtractor",
]
