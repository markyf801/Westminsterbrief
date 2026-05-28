from __future__ import annotations

from .base import BaseExtractor, ExtractedFile, ExtractionResult
from .fallback import FallbackExtractor
from .govuk_generic import GovUKGenericExtractor
from .ons_api import OnsApiExtractor

_GOVUK_ROOT         = "https://www.gov.uk/"
_ONS_DATASETS_ROOT  = "https://www.ons.gov.uk/datasets/"

# Singletons — extractors carry no per-instance state
_govuk    = GovUKGenericExtractor()
_ons      = OnsApiExtractor()
_fallback = FallbackExtractor()


def select_extractor(publication) -> BaseExtractor:
    """Return the appropriate extractor for a StatPublication."""
    if publication.url.startswith(_GOVUK_ROOT):
        return _govuk
    if publication.url.startswith(_ONS_DATASETS_ROOT):
        return _ons
    return _fallback


__all__ = [
    "select_extractor",
    "BaseExtractor",
    "ExtractedFile",
    "ExtractionResult",
    "GovUKGenericExtractor",
    "OnsApiExtractor",
    "FallbackExtractor",
]
