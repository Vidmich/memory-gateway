"""What a file is, decided from its bytes.

The tests worth reading here are the ones about *lying*: a file whose extension and
whose content disagree. That is not a hypothetical — it is what a renamed download, a
mislabelled export, and a browser guessing from a file association all produce — and
getting it wrong means embedding mojibake and reporting it as indexed.
"""

from __future__ import annotations

import pytest

from app.services.filetypes import (
    DEFAULT_BINARY_TYPE,
    bom_encoding,
    describe,
    extension_of,
    looks_like_text,
    sniff,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"
ZIP = b"PK\x03\x04" + b"\x00" * 32
MOV = b"\x00\x00\x00\x20ftypqt  " + b"\x00" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 32


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("notes.md", ".md"),
        ("NOTES.MD", ".md"),
        ("archive.tar.gz", ".gz"),
        ("docs/api/auth.py", ".py"),
        ("Makefile", ""),
        (".gitignore", ".gitignore"),
        ("", ""),
    ],
)
def test_the_extension_is_the_last_segment(name: str, expected: str) -> None:
    assert extension_of(name) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (PDF, "application/pdf"),
        (MOV, "video/quicktime"),
        (MP4, "video/mp4"),
        (ELF, "application/x-executable"),
    ],
)
def test_a_binary_signature_is_recognised(data: bytes, expected: str) -> None:
    assert sniff(data, name="whatever.txt") == expected


def test_the_bytes_beat_the_extension() -> None:
    """The whole point of sniffing. A JPEG called ``notes.txt`` decodes to mojibake and
    embeds as noise; recognising it means it is skipped with a reason instead."""
    assert sniff(JPEG, name="notes.txt") == "image/jpeg"


def test_a_mov_is_recognised_from_its_ftyp_box_not_its_name() -> None:
    """The demo drops in a `.mov`. It lands as skipped because of these twelve bytes."""
    assert sniff(MOV, name="clip.mov") == "video/quicktime"
    assert sniff(MOV, name="clip.txt") == "video/quicktime"


def test_a_zip_container_is_refined_by_its_extension() -> None:
    """The one place an extension refines a binary verdict — and only inside a container
    the bytes already proved it to be."""
    assert sniff(ZIP, name="report.docx").endswith("wordprocessingml.document")
    assert sniff(ZIP, name="backup.zip") == "application/zip"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("readme.md", "text/markdown"),
        ("data.csv", "text/csv"),
        ("data.tsv", "text/tab-separated-values"),
        ("config.yaml", "application/yaml"),
        ("app.py", "text/x-python"),
        ("index.html", "text/html"),
        ("rows.jsonl", "application/x-ndjson"),
        ("notes.unknown", "text/plain"),
    ],
)
def test_text_is_narrowed_by_extension(name: str, expected: str) -> None:
    """Bytes cannot tell Markdown from CSV from Python — they are all characters — so the
    extension gets the say it is actually qualified to have."""
    assert sniff(b"the quick brown fox\n" * 10, name=name) == expected


def test_binary_with_no_known_signature_is_octet_stream() -> None:
    assert sniff(bytes(range(256)) * 4, name="mystery.bin") == DEFAULT_BINARY_TYPE


def test_an_empty_file_is_text_not_binary() -> None:
    """It extracts to nothing and is reported as an empty document, which is truthful.
    "Binary" would not be."""
    assert looks_like_text(b"") is True
    assert sniff(b"", name="empty.md") == "text/markdown"


def test_a_nul_byte_means_binary() -> None:
    assert looks_like_text(b"hello\x00world") is False


def test_a_byte_order_mark_beats_the_nul_check() -> None:
    """UTF-16 is full of NUL bytes. A sniffer that stopped at the first one would reject
    half the world's exported spreadsheets."""
    utf16 = "hello, world".encode("utf-16")

    assert bom_encoding(utf16) in {"utf-16-le", "utf-16-be"}
    assert looks_like_text(utf16) is True


def test_an_ansi_coloured_log_is_still_text() -> None:
    """Escape is deliberately not a suspicious control character: coloured CI logs are
    text, and a source somebody would genuinely want indexed."""
    coloured = b"\x1b[32mPASS\x1b[0m tests/test_thing.py\n" * 20

    assert looks_like_text(coloured) is True


def test_one_stray_control_byte_does_not_make_a_file_binary() -> None:
    assert looks_like_text(b"a" * 5000 + b"\x07" + b"b" * 5000) is True


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        ("application/pdf", "PDF"),
        ("video/quicktime", "video"),
        ("image/png", "image"),
        ("audio/mpeg", "audio"),
        (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "Office document",
        ),
        ("application/x-executable", "file type"),
    ],
)
def test_the_description_reads_as_a_sentence(media_type: str, expected: str) -> None:
    """It goes into "This {x} is not a supported format", which a customer reads."""
    assert describe(media_type) == expected
