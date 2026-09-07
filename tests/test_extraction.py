"""Extraction, per format, with a malformed example of each.

Two things are being tested and they are worth separating in your head.

The **happy path** assertions are about *rendering quality*: a CSV row becomes named
fields, a JSON tree becomes key paths, headings become section titles. Those are the
decisions that determine whether retrieval works at all, and they are invisible in any
end-to-end test, which would pass just as happily on raw commas.

The **malformed** assertions are about the error a customer reads. Every one of them
checks that the message names something actionable — a line number, a byte offset, a
format — because ``documents.error`` is rendered inline in the document table and "could
not parse" is not something anyone can act on.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.extraction import (
    ExtractionError,
    build_registry,
    decode,
    extract_csv,
    extract_html,
    extract_json,
    extract_jsonl,
    extract_markdown,
    extract_plain,
    extract_tsv,
    flatten,
)

# ---------------------------------------------------------------------------
# decoding
# ---------------------------------------------------------------------------


def test_utf8_is_tried_first() -> None:
    assert decode("héllo — wörld".encode()) == "héllo — wörld"


def test_a_byte_order_mark_is_honoured_and_stripped() -> None:
    assert decode("héllo".encode("utf-8-sig")) == "héllo"
    assert decode("héllo".encode("utf-16")) == "héllo"


def test_a_legacy_encoding_is_detected() -> None:
    """Windows-1252 is what a spreadsheet exported on a European desktop produces, and
    refusing it would fail a large fraction of real uploads."""
    text = "Café Ø resumé\n" * 40
    assert "Caf" in decode(text.encode("cp1252"))


def test_bytes_no_encoding_claims_are_reported_with_the_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detection almost always finds *something* — single-byte codecs map nearly every
    byte — so this drives the branch where it does not, which is the one whose message a
    customer reads. "Could not decode" alone sends somebody through a 200 MB export by
    hand; the offset says which line to look at."""
    monkeypatch.setattr(
        "app.services.extraction.from_bytes", lambda _: SimpleNamespace(best=lambda: None)
    )

    with pytest.raises(ExtractionError) as error:
        decode(b"ok so far \xff\xfe\xfd" * 100, name="broken.txt")

    assert "byte 10" in str(error.value)
    assert "utf-8" in str(error.value)


def test_a_declared_encoding_that_does_not_decode_is_reported() -> None:
    """A byte-order mark is a claim, and a truncated UTF-16 file is one that is wrong."""
    with pytest.raises(ExtractionError) as error:
        decode(b"\xff\xfeh\x00i\x00\x00", name="odd.txt")

    assert "utf-16-le" in str(error.value)


def test_an_empty_file_decodes_to_nothing() -> None:
    assert decode(b"") == ""


# ---------------------------------------------------------------------------
# plain text and code
# ---------------------------------------------------------------------------


def test_plain_text_is_one_section() -> None:
    extracted = extract_plain(b"line one\nline two\n", name="notes.txt")

    assert len(extracted.sections) == 1
    assert extracted.sections[0].title is None
    assert extracted.text == "line one\nline two"


def test_xml_keeps_its_tags() -> None:
    """Deliberate. An element name is the only label a value has; stripping the tags
    leaves a column of bare strings with nothing to attach them to."""
    extracted = extract_plain(b"<user><name>Ada</name></user>", name="u.xml")

    assert "<name>Ada</name>" in extracted.text


# ---------------------------------------------------------------------------
# markdown
# ---------------------------------------------------------------------------

GUIDE = b"""# Guide

Intro paragraph.

## Setup

Install the thing.

### Windows

Use the installer.

## Usage

Run it.
"""


def test_markdown_splits_on_headings() -> None:
    sections = extract_markdown(GUIDE, name="guide.md").sections

    assert [section.title for section in sections] == [
        "Guide",
        "Guide > Setup",
        "Guide > Setup > Windows",
        "Guide > Usage",
    ]


def test_a_section_title_is_the_whole_path() -> None:
    """A chunk labelled "Windows" tells a reader nothing. The path tells them where in
    the document they landed, which is what makes a citation usable."""
    sections = extract_markdown(GUIDE, name="guide.md").sections

    assert sections[2].title == "Guide > Setup > Windows"


def test_the_heading_stays_in_the_body_too() -> None:
    """It is the most information-dense line in the section; a chunk that dropped it
    would lose the only sentence naming what the section is about."""
    sections = extract_markdown(GUIDE, name="guide.md").sections

    assert sections[1].text.startswith("## Setup")


def test_a_hash_inside_a_code_fence_is_not_a_heading() -> None:
    """``# comment`` opens half the shell snippets ever written. Treating it as a heading
    splits the code block in two."""
    source = b"# Title\n\n```sh\n# install\nnpm ci\n```\n\nDone.\n"

    sections = extract_markdown(source, name="a.md").sections

    assert len(sections) == 1
    assert "npm ci" in sections[0].text


def test_markdown_with_no_headings_is_still_one_section() -> None:
    extracted = extract_markdown(b"just a paragraph\n", name="a.md")

    assert len(extracted.sections) == 1
    assert extracted.sections[0].title is None


# ---------------------------------------------------------------------------
# html
# ---------------------------------------------------------------------------

PAGE = b"""<html><head><title>t</title><style>.a{color:red}</style></head>
<body>
<h1>Handbook</h1>
<p>Welcome to <b>Acme</b>.</p>
<h2>Leave</h2>
<p>Twenty-five days.</p>
<script>track()</script>
</body></html>"""


def test_html_is_stripped_to_text() -> None:
    extracted = extract_html(PAGE, name="p.html")

    assert "Welcome to Acme." in extracted.text
    assert "<p>" not in extracted.text


def test_script_and_style_contents_are_dropped() -> None:
    """Otherwise every page contributes its analytics snippet to the index, and a search
    for anything common retrieves JavaScript."""
    text = extract_html(PAGE, name="p.html").text

    assert "track()" not in text
    assert "color:red" not in text


def test_html_headings_become_sections() -> None:
    sections = extract_html(PAGE, name="p.html").sections

    assert [section.title for section in sections] == ["Handbook", "Handbook > Leave"]


def test_inline_tags_do_not_introduce_whitespace() -> None:
    """``<b>data</b>base`` is one word. Splitting it makes two that mean nothing."""
    extracted = extract_html(b"<p><b>data</b>base</p>", name="a.html")

    assert extracted.text.strip() == "database"


def test_html_entities_are_decoded() -> None:
    extracted = extract_html(b"<p>caf&eacute; &amp; bar</p>", name="a.html")

    assert extracted.text.strip() == "café & bar"


def test_html_that_is_not_html_still_produces_text() -> None:
    """`html.parser` is lenient by design, which is right: a mislabelled file should
    produce its text rather than an error nobody can act on."""
    extracted = extract_html(b"just words, no tags at all", name="a.html")

    assert "just words" in extracted.text


# ---------------------------------------------------------------------------
# csv and tsv
# ---------------------------------------------------------------------------


def test_csv_becomes_named_records() -> None:
    """The single change that most improves retrieval on tabular sources.
    ``Ada,Engineer,London`` embeds to a vector about commas."""
    extracted = extract_csv(b"name,role,office\nAda,Engineer,London\n", name="p.csv")

    assert extracted.text == "name: Ada\nrole: Engineer\noffice: London"


def test_each_row_is_its_own_record() -> None:
    extracted = extract_csv(b"name\nAda\nGrace\n", name="p.csv")

    assert extracted.text == "name: Ada\n\nname: Grace"


def test_an_empty_cell_is_omitted_rather_than_rendered_blank() -> None:
    """ "office:" on every row costs a token per column per row and says nothing."""
    extracted = extract_csv(b"name,office\nAda,\n", name="p.csv")

    assert extracted.text == "name: Ada"


def test_a_semicolon_delimiter_is_sniffed() -> None:
    """Half the world's `.csv` files are semicolon-separated, from locales that use the
    comma for decimals."""
    extracted = extract_csv(b"name;role\nAda;Engineer\n", name="p.csv")

    assert extracted.text == "name: Ada\nrole: Engineer"


def test_tsv_does_not_let_the_sniffer_overrule_the_format() -> None:
    """A tab-separated file with commas inside its cells would otherwise become one
    column named after the whole header line."""
    extracted = extract_tsv(b"name\trole\nAda, Lovelace\tEngineer\n", name="p.tsv")

    assert extracted.text == "name: Ada, Lovelace\nrole: Engineer"


def test_a_column_with_no_header_gets_a_positional_name() -> None:
    extracted = extract_csv(b"name,\nAda,x\n", name="p.csv")

    assert "column 2: x" in extracted.text


def test_a_row_longer_than_its_header_still_renders() -> None:
    """Malformed, and common: a trailing delimiter or an unescaped separator. Dropping
    the row would silently lose data; naming the extra column keeps it."""
    extracted = extract_csv(b"name\nAda,Engineer\n", name="p.csv")

    assert "name: Ada" in extracted.text
    assert "column 2: Engineer" in extracted.text


def test_an_empty_csv_extracts_to_nothing() -> None:
    assert extract_csv(b"", name="p.csv").is_empty


# ---------------------------------------------------------------------------
# json and jsonl
# ---------------------------------------------------------------------------


def test_json_becomes_key_paths() -> None:
    extracted = extract_json(b'{"user": {"name": "Ada", "roles": ["admin", "dev"]}}', name="a.json")

    assert extracted.text.splitlines() == [
        "user.name: Ada",
        "user.roles.0: admin",
        "user.roles.1: dev",
    ]


def test_a_null_renders_as_nothing() -> None:
    """``office: None`` is a fact about the exporter, not about the subject."""
    assert list(flatten({"name": "Ada", "office": None})) == ["name: Ada"]


def test_an_empty_object_renders_as_nothing() -> None:
    assert list(flatten({"settings": {}, "tags": []})) == []


def test_a_bare_scalar_document_renders_without_a_path() -> None:
    assert list(flatten("hello")) == ["hello"]


def test_malformed_json_names_the_line_and_column() -> None:
    with pytest.raises(ExtractionError) as error:
        extract_json(b'{"a": 1,\n "b": }', name="a.json")

    message = str(error.value)
    assert "line 2" in message
    assert "column" in message


def test_jsonl_renders_one_record_per_line() -> None:
    extracted = extract_jsonl(b'{"a": 1}\n{"a": 2}\n', name="a.jsonl")

    assert extracted.text == "a: 1\n\na: 2"


def test_one_bad_jsonl_line_does_not_fail_the_file() -> None:
    """A truncated final line is the single most common thing wrong with an append-only
    export. Failing the whole file for it is the wrong trade."""
    extracted = extract_jsonl(b'{"a": 1}\nnot json\n{"a": 2}\n', name="a.jsonl")

    assert extracted.text == "a: 1\n\na: 2"


def test_jsonl_with_no_readable_line_fails_with_a_reason() -> None:
    """The tolerance above has a floor: a file where *nothing* parsed is not JSONL, and
    silently indexing an empty document would hide that."""
    with pytest.raises(ExtractionError) as error:
        extract_jsonl(b"nope\nalso nope\n", name="a.jsonl")

    assert "JSON Lines" in str(error.value)


def test_blank_lines_in_jsonl_are_ignored() -> None:
    assert extract_jsonl(b'\n{"a": 1}\n\n', name="a.jsonl").text == "a: 1"


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------


def test_the_registry_finds_an_extractor_by_media_type() -> None:
    registry = build_registry()

    assert registry.find(media_type="text/markdown", name="x") is extract_markdown


def test_the_registry_falls_back_to_the_extension_for_text() -> None:
    """For a *textual* media type this build has no opinion about. A `.rst` file works
    even if a future sniffer stops recognising it."""
    registry = build_registry()

    assert registry.find(media_type="text/x-newly-invented", name="notes.rst") is extract_plain


def test_an_extension_never_overrules_binary_bytes() -> None:
    """The load-bearing half. A JPEG called `notes.txt` must not reach a text extractor,
    or it decodes to mojibake and is indexed as though it worked."""
    registry = build_registry()

    assert registry.find(media_type="image/jpeg", name="notes.txt") is None


def test_an_unknown_format_has_no_extractor_and_no_note() -> None:
    registry = build_registry()

    assert registry.find(media_type="video/quicktime", name="clip.mov") is None
    assert registry.pending_note("video/quicktime") is None


@pytest.mark.parametrize(
    "media_type",
    [
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ],
)
def test_task_elevens_formats_are_recognised_as_coming_soon(media_type: str) -> None:
    """The distinction that keeps the gap honest: a roadmap item, not a broken product."""
    registry = build_registry()

    assert registry.find(media_type=media_type, name="x") is None
    note = registry.pending_note(media_type)
    assert note is not None and "later release" in note


def test_registering_an_extractor_clears_its_coming_soon_note() -> None:
    """What task 11 actually does. One call, no change to the pipeline."""
    registry = build_registry()
    registry.register(extract_plain, media_types=("application/pdf",), extensions=(".pdf",))

    assert registry.find(media_type="application/pdf", name="a.pdf") is extract_plain
    assert registry.pending_note("application/pdf") is None


def test_every_text_format_the_spec_names_has_an_extractor() -> None:
    """SPEC §9.2's text and code groups, checked as a set rather than one test each — the
    failure worth catching is a format silently missing from the registry."""
    registry = build_registry()
    from app.services.filetypes import TEXT_EXTENSIONS

    missing = [
        extension
        for extension, media_type in TEXT_EXTENSIONS.items()
        if registry.find(media_type=media_type, name=f"x{extension}") is None
    ]

    assert missing == []
