from __future__ import annotations

import logging

import requests

from .base import BaseExtractor, ExtractionResult

log = logging.getLogger(__name__)


class FallbackExtractor(BaseExtractor):
    """
    Used for any producer whose URL pattern has no dedicated extractor.
    Marks the publication not_extractable without making any HTTP request.
    """

    def extract(self, url: str, http_session: requests.Session) -> ExtractionResult:
        log.info("No extractor for %s — marking not_extractable", url)
        return ExtractionResult(status="not_extractable")
