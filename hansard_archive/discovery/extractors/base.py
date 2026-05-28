from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

import requests


@dataclass
class ExtractedFile:
    url: str
    file_type: Optional[str] = None        # normalised short form: xlsx/ods/csv/pdf/zip/json/xml
    title: Optional[str] = None
    file_size_bytes: Optional[int] = None
    classification: Optional[str] = None   # main_release/supporting_tables/technical_docs/other/None
    display_order: int = 0


@dataclass
class ExtractionResult:
    status: str                            # extracted/no_files_found/not_extractable/fetch_failed
    files: list[ExtractedFile] = field(default_factory=list)
    ees_url: Optional[str] = None
    error: Optional[str] = None
    sub_page_urls: list[str] = field(default_factory=list)  # piece 2b diagnostic


class BaseExtractor(ABC):
    @abstractmethod
    def extract(self, url: str, http_session: requests.Session) -> ExtractionResult:
        ...
