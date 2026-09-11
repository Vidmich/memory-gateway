"""The template renderer and schema (task 105): substitution is the only operation, the
vocabulary is closed per template, the invariants hold, and the defaults are byte-for-byte
what SPEC §7 prints."""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.core.errors import Validation
from app.schemas.config import merge_config
from app.schemas.gateway_config import TemplateConfig
from app.services import prompt
from app.services.citations import FOOTER_HEADING, Citation, footer
from app.services.templates import (
    DEFAULT_TEMPLATES,
    MAX_INSTRUCTION_LENGTH,
    MAX_TEMPLATE_LENGTH,
    NAMES,
    VOCABULARY,
    Templates,
    placeholders,
    render,
)
from app.services.tokenizer import WordTokenizer
from tests.prompt_support import chunk, fact

# ---------------------------------------------------------------------------
# the renderer
# ---------------------------------------------------------------------------


def test_substitutes_each_placeholder_once() -> None:
    assert render(
        "[{handle}] {source_name}{section}",
        {"handle": 2, "source_name": "a.md", "section": " (p. 1)"},
    ) == ("[2] a.md (p. 1)")


def test_a_substituted_value_is_never_rescanned() -> None:
    """A chunk that contains ``{text}`` renders those six characters; one that contains
    ``{{`` renders both braces. The scan is over the template, not over what it inserts."""
    assert render("{text}", {"text": "see {text} and {{"}) == "see {text} and {{"
    assert render("{text}{text}", {"text": "{handle}"}) == "{handle}{handle}"


def test_escaped_braces_are_literal() -> None:
    assert render("{{not a placeholder}} {text}", {"text": "x"}) == "{not a placeholder} x"
    assert render("{{{text}}}", {"text": "x"}) == "{x}"


def test_no_attribute_or_index_access_ever_happens() -> None:
    """What ``str.format`` would evaluate is left as the characters it is."""

    class Leaky:
        secret = "hunter2"

    assert render("{handle.secret}", {"handle": Leaky()}) == "{handle.secret}"
    assert render("{handle[0]}", {"handle": ["x"]}) == "{handle[0]}"
    assert render("{handle:>10}", {"handle": 1}) == "{handle:>10}"


def test_an_unknown_name_renders_as_itself() -> None:
    assert render("{nope} {text}", {"text": "x"}) == "{nope} x"


def test_placeholders_reports_every_group_including_malformed_ones() -> None:
    assert placeholders("[{handle}] {{x}} {handle.__class__} {a b}") == [
        "handle",
        "handle.__class__",
        "a b",
    ]


# ---------------------------------------------------------------------------
# the schema
# ---------------------------------------------------------------------------


def save(patch: dict[str, str]) -> dict[str, object]:
    return merge_config(TemplateConfig, {}, patch, field="template_config")


def refused(patch: dict[str, str]) -> Validation:
    with pytest.raises(Validation) as caught:
        save(patch)
    return caught.value


def test_every_placeholder_of_every_template_is_accepted() -> None:
    for name, allowed in VOCABULARY.items():
        template = "".join("{" + item + "}" for item in allowed)
        if name == "excerpt":
            template = "[{handle}] " + template
        saved = save({name: template})
        assert saved[name] == template


def test_an_unknown_placeholder_is_refused_by_name_with_the_list() -> None:
    error = refused({"excerpt": "[{handle}] {page}"})
    assert error.param == "template_config.excerpt"
    assert "'{page}' is not a placeholder" in str(error)
    assert "{source_name}" in str(error)
    # `str.format`-style attribute access is an unknown placeholder, not an evaluation.
    error = refused({"excerpt": "[{handle}] {handle.__class__}"})
    assert "'{handle.__class__}' is not a placeholder" in str(error)


def test_the_vocabulary_is_per_template() -> None:
    error = refused({"fact": "{source_name}: {text}"})
    assert error.param == "template_config.fact"
    assert "Allowed: {text}." in str(error)


def test_a_lone_brace_is_refused_with_the_escape_in_the_message() -> None:
    error = refused({"excerpt": "[{handle}] {text} {"})
    assert "{{" in str(error)


def test_the_excerpt_must_keep_the_bracketed_handle() -> None:
    error = refused({"excerpt": "{source_name}: {text}"})
    assert error.param == "template_config.excerpt"
    assert "must contain [{handle}]" in str(error)
    assert "cannot cite" in str(error)
    # `{handle}` without the brackets is not enough: the resolver looks for `[n]`.
    assert "must contain [{handle}]" in str(refused({"excerpt": "{handle}. {text}"}))


def test_the_source_line_must_keep_the_handle() -> None:
    error = refused({"source_line": "{label}"})
    assert error.param == "template_config.source_line"
    assert "must contain {handle}" in str(error)


def test_the_fact_template_is_one_line_and_prints_the_text() -> None:
    assert "one line" in str(refused({"fact": "- {text}\n"}))
    assert "must contain {text}" in str(refused({"fact": "- a fact"}))


def test_plain_headings_take_no_placeholders_and_keep_their_braces() -> None:
    saved = save({"reference_heading": "## {Referenz}"})
    assert saved["reference_heading"] == "## {Referenz}"
    templates = Templates.of(TemplateConfig.load(saved))
    assert prompt.render_documents([chunk("x")], templates=templates).startswith("## {Referenz}\n")


def test_length_ceilings() -> None:
    assert save({"reference_instruction": "x" * MAX_INSTRUCTION_LENGTH})
    assert refused({"reference_instruction": "x" * (MAX_INSTRUCTION_LENGTH + 1)}).param == (
        "template_config.reference_instruction"
    )
    assert save({"reference_heading": "x" * MAX_TEMPLATE_LENGTH})
    assert refused({"reference_heading": "x" * (MAX_TEMPLATE_LENGTH + 1)})


def test_the_merge_is_partial() -> None:
    stored = save({"reference_heading": "## Referenzmaterial"})
    merged = merge_config(
        TemplateConfig, stored, {"sources_heading": "Quellen:"}, field="template_config"
    )
    assert merged["reference_heading"] == "## Referenzmaterial"
    assert merged["sources_heading"] == "Quellen:"
    assert merged["excerpt"] == DEFAULT_TEMPLATES.excerpt


def test_an_unknown_key_is_refused() -> None:
    error = refused({"excerpt_heading": "x"})
    assert "not a setting on this section" in str(error)


def test_warnings_are_inline_and_not_refusals() -> None:
    config = TemplateConfig.load(
        save({"reference_instruction": "", "excerpt": "[{handle}] {text}"})
    )
    assert len(config.warnings) == 2
    assert "grounded assistant" in config.warnings[0]
    assert "cannot name the document" in config.warnings[1]
    assert TemplateConfig().warnings == []


# ---------------------------------------------------------------------------
# defaults and rendering
# ---------------------------------------------------------------------------


def test_the_defaults_are_the_constants_the_assembler_and_the_footer_print() -> None:
    config = TemplateConfig()
    assert Templates.of(config) == DEFAULT_TEMPLATES
    assert Templates.load({}) == DEFAULT_TEMPLATES
    assert Templates.load(config.model_dump()) == DEFAULT_TEMPLATES
    assert DEFAULT_TEMPLATES.reference_heading == prompt.REFERENCE_HEADING
    assert DEFAULT_TEMPLATES.reference_instruction == prompt.REFERENCE_INSTRUCTION
    assert DEFAULT_TEMPLATES.memory_heading == prompt.MEMORY_HEADING
    assert DEFAULT_TEMPLATES.sources_heading == FOOTER_HEADING
    assert not DEFAULT_TEMPLATES.wraps


def test_a_custom_excerpt_renders_every_placeholder() -> None:
    templates = Templates(
        excerpt="[{handle}] {source_name}{section} · {section_raw} · {score}\n{text}"
    )
    entry = prompt.render_entry(3, chunk("Body.", section="p. 12", score=0.5), templates=templates)
    assert entry == "[3] handbook.md (p. 12) · p. 12 · 0.50\nBody."
    entry = prompt.render_entry(1, chunk("Body."), templates=templates)
    assert entry == "[1] handbook.md ·  · 0.80\nBody."


def test_a_chunk_containing_placeholders_renders_them_literally() -> None:
    entry = prompt.render_entry(1, chunk("use {text} or {{handle}}"), templates=DEFAULT_TEMPLATES)
    assert entry.endswith("\nuse {text} or {{handle}}")


def test_a_summary_keeps_its_fixed_shape_under_any_excerpt_template() -> None:
    templates = Templates(excerpt="[{handle}] Quelle: {source_name}\n{text}")
    summary = replace(chunk("It is about refunds.", index=9), kind="summary")
    assert prompt.render_entry(2, summary, templates=templates) == (
        "[2] summary of: handbook.md\nIt is about refunds."
    )


def test_empty_headings_are_dropped_rather_than_printed_blank() -> None:
    templates = Templates(reference_heading="", reference_instruction="", memory_heading="")
    assert (
        prompt.render_documents([chunk("x")], templates=templates) == "[1] source: handbook.md\nx"
    )
    assert prompt.render_facts([fact("Likes tea.")], templates=templates) == "- Likes tea."
    only_heading = Templates(reference_instruction="")
    assert prompt.render_documents([chunk("x")], templates=only_heading) == (
        "## Reference material\n\n[1] source: handbook.md\nx"
    )


def test_the_fact_template_applies_to_each_flattened_fact() -> None:
    templates = Templates(memory_heading="## Über diese Person", fact="• {text}")
    assert prompt.render_facts([fact("a\nb"), fact("c")], templates=templates) == (
        "## Über diese Person\n• a b\n• c"
    )


def test_the_footer_renders_its_two_templates() -> None:
    templates = Templates(
        sources_heading="Quellen:", source_line="{handle}. {source_name}{section}"
    )
    lines = footer([Citation(handle=2, chunk=chunk("x", section="p. 4"))], templates=templates)
    assert lines == "\n\nQuellen:\n2. handbook.md (p. 4)"
    with_url = footer(
        [Citation(handle=2, chunk=chunk("x"))],
        base_url="https://ui",
        templates=Templates(source_line="[{handle}] {label} <{url}>"),
    )
    assert with_url.startswith("\n\nSources:\n[2] [handbook.md](https://ui/connectors/")
    assert with_url.endswith(
        ") <https://ui/connectors/00000000-0000-0000-0000-000000000007?document=11111111-1111-5111-8111-111111111111&chunk=11111111-1111-5111-8111-111111111111%3A0>"
    )


def test_assembly_with_a_custom_template_measures_the_rendered_form() -> None:
    """The budget is over the block as rendered: a longer heading spends it."""
    tokenizer = WordTokenizer()
    chunks = [chunk("one two three"), chunk("four five six", index=1)]
    plain = prompt.fit_documents(chunks, budget=1000, tokenizer=tokenizer)
    longer = Templates(reference_heading="## " + " ".join(["word"] * 40))
    heavier = prompt.fit_documents(chunks, budget=1000, tokenizer=tokenizer, templates=longer)
    assert heavier.tokens > plain.tokens
    # And under a budget the plain block fits, the heavier one drops from the tail.
    squeezed = prompt.fit_documents(
        chunks, budget=plain.tokens, tokenizer=tokenizer, templates=longer
    )
    assert len(squeezed.kept) < len(plain.kept)
    # Handles still start at 1 and are what the text prints.
    assert heavier.text.count("[1] source:") == 1 and "[2] source:" in heavier.text


# ---------------------------------------------------------------------------
# the fingerprint
# ---------------------------------------------------------------------------


def test_the_fingerprint_is_stable_for_equal_templates_and_moves_for_different_ones() -> None:
    assert Templates().fingerprint == DEFAULT_TEMPLATES.fingerprint
    assert len(DEFAULT_TEMPLATES.fingerprint) == 16
    for name in NAMES:
        changed = Templates(**{name: "[{handle}] {text} changed"})
        assert changed.fingerprint != DEFAULT_TEMPLATES.fingerprint, name
    assert Templates(answer_suffix="a").fingerprint == Templates(answer_suffix="a").fingerprint
