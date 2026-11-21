"""Plain text from an uploaded resume (PDF, Word .docx or text), read on the user's own computer.

Uploads are untrusted: the size is capped, the type is decided by the file's first bytes (not its name), PDFs
are read for at most a few pages, and .docx text is pulled out with a pattern instead of an XML parser
(no entity expansion, no external references). The file itself is never stored.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from enum import StrEnum

from pypdf import PdfReader
from pypdf.errors import PdfReadError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PDF_PAGES = 10
MAX_DOCX_XML_BYTES = 5 * 1024 * 1024
MAX_TEXT_CHARS = 50_000
MIN_TEXT_CHARS = 50  # fewer than any real resume: probably a scanned image
_DOCX_BODY = "word/document.xml"
_DOCX_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>")
_DOCX_BREAK = re.compile(r"</w:p>|<w:br\s*/>|<w:tab\s*/>")
_SPACES = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")


class ResumeError(ValueError):
    """The upload cannot be read as a resume; the message is safe to show the user."""


class ResumeKind(StrEnum):
    """Supported upload types."""

    PDF = "pdf"
    DOCX = "docx"
    TEXT = "text"


def detect(data: bytes) -> ResumeKind:
    """The upload type from its first bytes. Raises ``ResumeError`` for anything else."""
    if data.startswith(b"%PDF-"):
        return ResumeKind.PDF
    if data.startswith(b"PK\x03\x04"):
        return ResumeKind.DOCX
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        raise ResumeError("Please send a PDF, a Word (.docx) file or plain text.") from None
    return ResumeKind.TEXT


def _tidy(text: str) -> str:
    lines = (_SPACES.sub(" ", line).strip() for line in text.replace("\r", "\n").split("\n"))
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()[:MAX_TEXT_CHARS]


def _pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ResumeError("This PDF is password-protected. Please send an unlocked copy.")
        return "\n".join(page.extract_text() or "" for page in reader.pages[:MAX_PDF_PAGES])
    except (PdfReadError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, ResumeError):
            raise
        raise ResumeError("I couldn't read this PDF. Try exporting it again, or send a .docx.") from None


def _docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo(_DOCX_BODY)
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise ResumeError("This Word file is too large to read.")
            xml = archive.read(_DOCX_BODY).decode("utf-8", errors="replace")
    except (zipfile.BadZipFile, KeyError):
        raise ResumeError("I couldn't read this Word file. Please send a .docx or PDF.") from None
    with_breaks = _DOCX_BREAK.sub("<w:t>\n</w:t>", xml)  # paragraph ends become text runs holding a newline
    return html.unescape("".join(_DOCX_TEXT.findall(with_breaks)))


def extract_text(data: bytes) -> str:
    """Readable text from an uploaded resume. Raises ``ResumeError`` with a message for the user."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ResumeError("That file is over 5 MB. Please send a smaller resume.")
    kind = detect(data)
    if kind is ResumeKind.PDF:
        raw = _pdf_text(data)
    elif kind is ResumeKind.DOCX:
        raw = _docx_text(data)
    else:
        raw = data.decode("utf-8", errors="replace")
    text = _tidy(raw)
    if len(text) < MIN_TEXT_CHARS:
        raise ResumeError(
            "I found almost no text. If it's a scanned image, please send a text-based PDF or .docx."
        )
    return text
