"""prenly-dl: hämta tidningsnummer från Prenly-plattformen (Textalk)."""

from .api import (
    Credentials,
    build_session,
    download_media,
    get_catalogue_issues,
    get_context_token,
    get_issue,
    get_page_checksums,
    get_titles,
)
from .downloader import DownloadResult, download_issue, merge_pdfs, safe_filename
from .errors import (
    PrenlyApiError,
    PrenlyAuthError,
    PrenlyError,
    PrenlyHTTPError,
    PrenlySubscriptionError,
)

__all__ = [
    "Credentials",
    "DownloadResult",
    "PrenlyApiError",
    "PrenlyAuthError",
    "PrenlyError",
    "PrenlyHTTPError",
    "PrenlySubscriptionError",
    "build_session",
    "download_issue",
    "download_media",
    "get_catalogue_issues",
    "get_context_token",
    "get_issue",
    "get_page_checksums",
    "get_titles",
    "merge_pdfs",
    "safe_filename",
]

__version__ = "0.2.0"
