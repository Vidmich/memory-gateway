"""What a file actually is, decided from its first bytes.

The rule this module exists to enforce: **the extension and the client's ``Content-Type``
are hints, the bytes are evidence.** A browser sends whatever the OS told it; a scripted
upload sends whatever the script hard-coded; a renamed file lies outright. Trusting any of
those means a JPEG named ``notes.txt`` is decoded as mojibake and embedded — a document
that reports ``indexed`` and poisons every retrieval that comes near it.

So the order is: a known binary signature wins outright, whatever the name says. Only when
the bytes look like text does the extension get a say, and then only to choose *which*
text type — which is the one thing bytes cannot settle, since Markdown, CSV and Python are
all just characters.

The consequence worth stating: this is what makes ``skipped`` honest. A ``.mov`` is
recognised as video from its ``ftyp`` box rather than from four characters of its name,
and lands as ``skipped: unsupported`` with a reason a person can read, instead of as a
``failed`` decode of something nobody expected to work.
"""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT_TEXT_TYPE = "text/plain"
DEFAULT_BINARY_TYPE = "application/octet-stream"

#: How much of the head is enough. Every signature below is shorter than 32 bytes; the
#: rest is for the text/binary judgement, where a larger sample means fewer false
#: negatives on a file that happens to start with an ASCII header.
SNIFF_BYTES = 8192

#: Magic numbers, longest first so a prefix never shadows a longer match. Only formats
#: worth *naming* are here — the value of the table is the readable ``skipped`` reason,
#: not exhaustive coverage.
SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"SQLite format 3\x00", "application/vnd.sqlite3"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"Rar!\x1a\x07", "application/vnd.rar"),
    (b"%PDF-", "application/pdf"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\x1aE\xdf\xa3", "video/x-matroska"),
    (b"\x7fELF", "application/x-executable"),
    (b"OggS", "audio/ogg"),
    (b"fLaC", "audio/flac"),
    (b"PK\x03\x04", "application/zip"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"ID3", "audio/mpeg"),
    (b"\x1f\x8b", "application/gzip"),
    (b"BM", "image/bmp"),
    (b"MZ", "application/vnd.microsoft.portable-executable"),
)

#: The Office media types, spelled once. They are long enough that a typo in one of them
#: would be a format that silently never matches anything.
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF = "application/pdf"

#: ZIP is a container, so the signature alone cannot tell an Office document from a
#: backup archive. This is the one place an extension is allowed to refine a *binary*
#: verdict, and only within the container it already proved itself to be.
ZIP_CONTAINERS: Mapping[str, str] = {
    ".docx": DOCX,
    ".pptx": PPTX,
    ".xlsx": XLSX,
    ".epub": "application/epub+zip",
}

#: ISO base-media brands. ``ftyp`` sits at offset 4, not 0, which is why this is checked
#: separately rather than folded into :data:`SIGNATURES`.
ISO_BRANDS: Mapping[bytes, str] = {
    b"qt  ": "video/quicktime",
    b"M4A ": "audio/mp4",
    b"M4V ": "video/x-m4v",
    b"heic": "image/heic",
    b"avif": "image/avif",
}

#: Text types by extension. The keys of this mapping and of
#: :data:`app.services.extraction.EXTRACTORS` are two different lists on purpose: this
#: one says what a file *is*, the other says what can be read out of it.
TEXT_EXTENSIONS: Mapping[str, str] = {
    # SPEC §9.2, text group
    ".txt": "text/plain",
    ".text": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".rst": "text/x-rst",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    # SPEC §9.2, code group
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".ts": "text/x-typescript",
    ".tsx": "text/x-tsx",
    ".jsx": "text/x-jsx",
    ".go": "text/x-go",
    ".java": "text/x-java",
    ".rb": "text/x-ruby",
    ".rs": "text/x-rust",
    ".sql": "application/sql",
    ".sh": "application/x-sh",
    ".bash": "application/x-sh",
    ".c": "text/x-c",
    ".h": "text/x-c",
    ".cpp": "text/x-c++",
    ".hpp": "text/x-c++",
    ".cc": "text/x-c++",
    ".cs": "text/x-csharp",
    ".php": "text/x-php",
    ".kt": "text/x-kotlin",
    ".swift": "text/x-swift",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
}

#: Every media type this module will ever report for something that decodes to text.
#: Used to decide whether an *extension* is allowed a say — see
#: :meth:`app.services.extraction.ExtractorRegistry.find`.
TEXT_MEDIA_TYPES = frozenset(TEXT_EXTENSIONS.values()) | {DEFAULT_TEXT_TYPE}

_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

#: Control characters that never appear in text anyone meant to write. Tab, newline,
#: carriage return, form feed and escape are excluded — the last because ANSI-coloured
#: log files are text, and rejecting them would skip a genuinely useful source.
_TEXT_CONTROL = frozenset(range(32)) - {0x09, 0x0A, 0x0C, 0x0D, 0x1B}


def extension_of(name: str) -> str:
    """The lowercased final extension, or ``""``. ``archive.tar.gz`` is ``.gz``."""
    tail = name.rsplit("/", 1)[-1]
    _, dot, suffix = tail.rpartition(".")
    return f".{suffix.lower()}" if dot and suffix else ""


def bom_encoding(head: bytes) -> str | None:
    """The encoding a byte-order mark declares, if there is one."""
    for marker, encoding in _BOMS:
        if head.startswith(marker):
            return encoding
    return None


def looks_like_text(head: bytes) -> bool:
    """Whether a sample reads as text.

    A BOM settles it immediately — UTF-16 is full of NUL bytes and would otherwise fail
    the very next check, which is precisely the false negative that makes naive sniffers
    reject half the world's exported spreadsheets.
    """
    if not head:
        # An empty file is not binary. It extracts to nothing and is reported as an empty
        # document, which is a truthful answer; "binary" would not be.
        return True
    if bom_encoding(head) is not None:
        return True
    if b"\x00" in head:
        return False
    suspicious = sum(1 for byte in head if byte in _TEXT_CONTROL)
    # One stray control byte in a long file is a corrupt line, not a binary format. More
    # than a trickle of them is not something anyone typed.
    return suspicious / len(head) < 0.02


def sniff(head: bytes, *, name: str = "") -> str:
    """The MIME type of a file, from its first bytes and — only as a tiebreak — its name."""
    for marker, media_type in SIGNATURES:
        if head.startswith(marker):
            if media_type == "application/zip":
                return ZIP_CONTAINERS.get(extension_of(name), media_type)
            return media_type

    if len(head) >= 12 and head[4:8] == b"ftyp":
        return ISO_BRANDS.get(head[8:12], "video/mp4")

    if looks_like_text(head):
        return TEXT_EXTENSIONS.get(extension_of(name), DEFAULT_TEXT_TYPE)

    return DEFAULT_BINARY_TYPE


def is_text(media_type: str) -> bool:
    """Whether a sniffed type is one whose content is characters.

    This is what stops a filename from overruling the bytes. A JPEG called ``notes.txt``
    sniffs as ``image/jpeg``; without this check the extractor lookup would fall through
    to the ``.txt`` extension, decode the JPEG as mojibake, and index it — which is
    exactly the failure sniffing exists to prevent.
    """
    return media_type.startswith("text/") or media_type in TEXT_MEDIA_TYPES


#: Media type to metric label. A *closed* map with a fallback, because these become
#: Prometheus label values and a label taken straight from a sniffed media type is a
#: cardinality bomb waiting for the first customer who uploads something exotic. The
#: groups are the ones a per-format failure rate is worth reading for: the four heavy
#: formats individually, since each has its own parser and its own way of going wrong,
#: and everything textual collapsed into a handful.
FORMAT_LABELS: Mapping[str, str] = {
    PDF: "pdf",
    DOCX: "docx",
    PPTX: "pptx",
    XLSX: "xlsx",
    "text/markdown": "markdown",
    "text/html": "html",
    "text/csv": "csv",
    "text/tab-separated-values": "csv",
    "application/json": "json",
    "application/x-ndjson": "json",
    "text/plain": "text",
}


#: Every value :func:`format_label` can return, which makes it the closed set a per-format
#: chunking override may be keyed by. Sorted for a stable order in an error message and in
#: the UI; ``other`` last because it is the fallback rather than a format anybody picks.
FORMAT_KINDS: tuple[str, ...] = (
    *sorted(set(FORMAT_LABELS.values()) | {"code"}),
    "other",
)


#: Every media type :func:`format_label` classifies as something other than ``other``.
#: What a store needs to express a format kind as a ``WHERE`` clause (task 104): a kind
#: is a set of media types, and ``other`` is everything outside this set.
KNOWN_MEDIA_TYPES: frozenset[str] = frozenset(FORMAT_LABELS) | TEXT_MEDIA_TYPES


def media_types_of(kind: str) -> frozenset[str]:
    """The media types that classify as ``kind`` — the inverse of :func:`format_label`,
    for the one place a format has to be named in SQL rather than in Python: marking every
    document of a format stale in one ``UPDATE`` (task 104). Empty for ``other``, which
    is *not* a set of types but the complement of :data:`KNOWN_MEDIA_TYPES`; the caller
    spells that out as ``NOT IN``."""
    if kind == "other":
        return frozenset()
    if kind == "code":
        return frozenset(TEXT_MEDIA_TYPES - set(FORMAT_LABELS))
    return frozenset(media for media, label in FORMAT_LABELS.items() if label == kind)


def format_label(media_type: str) -> str:
    """A bounded label for the extraction metrics, and the key a chunking override uses.

    Never the raw media type — see :data:`FORMAT_LABELS`. That it serves both purposes is
    deliberate: a second extension list for chunking is how a file comes to be classified
    as code by one part of the pipeline and as text by another.
    """
    found = FORMAT_LABELS.get(media_type)
    if found is not None:
        return found
    return "code" if media_type in TEXT_MEDIA_TYPES else "other"


#: Media type to language, for the ``code`` chunking strategy. Deliberately short: the
#: strategy claims structural splitting, and a claim is only worth making for a language
#: something here can actually parse. Everything else classified as ``code`` still chunks
#: — recursively — which is a worse cut and never a failed document.
CODE_LANGUAGES: Mapping[str, str] = {
    "text/x-python": "python",
    "text/javascript": "javascript",
    "text/x-jsx": "javascript",
    "text/x-typescript": "typescript",
    "text/x-tsx": "typescript",
    "text/x-go": "go",
}

#: The sentence the connector screen shows next to the ``code`` strategy. Written out
#: rather than generated from the mapping above so it reads as English, and kept honest by
#: ``tests/test_chunking_code.py``: "code-aware" is not a claim anybody can check, and
#: "code-aware for Python, JavaScript, TypeScript and Go" is.
CODE_LANGUAGE_NAMES = ("Python", "JavaScript", "TypeScript", "Go")


def language_of(media_type: str) -> str | None:
    """The language the ``code`` strategy can split structurally, or ``None``."""
    return CODE_LANGUAGES.get(media_type)


def describe(media_type: str) -> str:
    """A short phrase for an error message: ``a PDF``, ``a video``, ``this file type``.

    Written for the sentence "Skipped: {describe} is not supported yet", which is what a
    customer reads on the connector page.
    """
    if media_type == PDF:
        return "PDF"
    if media_type in set(ZIP_CONTAINERS.values()):
        return "Office document"
    family = media_type.split("/", 1)[0]
    return {
        "image": "image",
        "video": "video",
        "audio": "audio",
    }.get(family, "file type")


__all__ = [
    "CODE_LANGUAGES",
    "CODE_LANGUAGE_NAMES",
    "DEFAULT_BINARY_TYPE",
    "DEFAULT_TEXT_TYPE",
    "DOCX",
    "FORMAT_KINDS",
    "FORMAT_LABELS",
    "KNOWN_MEDIA_TYPES",
    "PDF",
    "PPTX",
    "SNIFF_BYTES",
    "TEXT_EXTENSIONS",
    "TEXT_MEDIA_TYPES",
    "XLSX",
    "bom_encoding",
    "describe",
    "extension_of",
    "format_label",
    "is_text",
    "language_of",
    "looks_like_text",
    "media_types_of",
    "sniff",
]
