"""Submitting a prepared form somewhere. By default nothing is submitted anywhere: Formline prepares and verifies
the PDF, and a partner organization submits it. A provider exists so a supported integration can be added later,
and so tests can check the submitted / failed / unknown states.

A provider must say whether the agency confirmed the submission (`verified`). Formline says "submitted" only then.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol


@dataclass
class SubmissionResult:
    ok: bool  # the request went through without an error
    verified: bool  # the receiving system confirmed it (with a reference)
    reference: Optional[str] = None
    error: Optional[str] = None


class SubmissionProvider(Protocol):
    name: str

    def submit(self, *, form_id: str, pdf_path: Path, task_id: int) -> SubmissionResult: ...


_provider: Optional[SubmissionProvider] = None


def get_provider() -> Optional[SubmissionProvider]:
    """None: forms are prepared, never submitted. Real government systems are never wired in this prototype."""
    return _provider


def set_provider(provider: Optional[SubmissionProvider]) -> None:
    global _provider
    _provider = provider
