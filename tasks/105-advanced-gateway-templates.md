# Task 105 — Advanced configuration: request and response templates per gateway

**Slice:** every gateway gets an **Advanced** page where the text the gateway itself writes —
the reference block around retrieved documents, the memory block, each excerpt's heading line,
and the text it adds around the model's answer — is a template the organization can edit,
preview against a real question, and reset, without anyone touching the prompt assembler.
**Depends on:** 06, 10, 12, 100
**Spec:** §7 (prompt assembly), §7.1 (citations), §13.1 (gateway editor) — and amends §7 to say
the rendered shape it prints is a *default*, and §13.1 with the new page.
**Size:** M
**Status:** post-v1. Independent of 101–104; 103's evaluation runs should record the template
fingerprint this task introduces, so if 103 lands first it gains one field later.

---

## Why this slice

The gateway writes prose into every request and, since task 100, into some responses. All of it
is a constant in Python. `## Reference material`, the instruction that follows it, `[1] source:
handbook.pdf (p. 12)`, `## What you know about this user`, the bullet in front of each fact,
`Sources:` — each is a string in `app/services/prompt.py` or `app/services/citations.py`, and
each is a decision made once, in English, for every customer.

That is wrong in three ways that show up as support tickets rather than bugs:

- **Language.** A gateway serving German users injects an English heading and an English
  instruction into every prompt. Models cope; the transcript in the drawer still reads as two
  languages, and a customer who asked for a German assistant is right to ask why.
- **Domain.** "Reference material" is the right heading for a handbook and the wrong one for
  case law, source code or a product catalogue. The instruction "say so rather than inventing
  an answer" is correct for a support bot and too timid for a brainstorming assistant. The
  excerpt line prints a file name; a legal corpus wants the clause number first.
- **Measurement.** Prompt wording moves answer quality, and nobody can measure that with a
  wording they cannot change. Task 103 will compare recall before and after a *configuration*
  change; the block's phrasing is a configuration too, and today it is not one.

The fix is not a prompt editor with a text box for the whole system message — that already
exists as `system_context`, and it composes with the blocks rather than replacing them. It is
a small, fixed set of **templates**, each with a fixed set of placeholders, rendered by the
same assembler on the same code path, so the golden tests, the token budget and the citation
resolver all keep working exactly as they do for the defaults.

## Demo at the end of this task

Open a gateway, go to **Advanced**. Nine fields are listed with their defaults filled in and,
under each that takes placeholders, a chip row. Change the reference heading to
`## Referenzmaterial`, the instruction to German, and the excerpt line to
`[{handle}] {source_name}{section}`. Type a
question in the preview box: the assembled prompt below re-renders with the new block, token
count and all, from the same preview the Memory section uses. Save. Send a request through the
gateway: the transcript in Monitoring shows the German block.

Change the excerpt line to `{source_name}: {text}` — dropping the handle. The field goes red:
*"the excerpt template must contain `[{handle}]`; without it the model cannot cite and
citations cannot resolve."* It will not save.

Set the response suffix to `\n\n_Answers are generated from internal documents and may be
incomplete._`. Stream a request: the suffix arrives as the last content delta, after the
citation footer, before `[DONE]`. Open a second gateway: untouched, still the defaults — until
you set the organization's defaults under Settings, after which every *new* gateway starts
from them.

## In scope

- A `TemplateConfig` blob per gateway — four plain headings and five templates with
  placeholders — each defaulting to today's exact text, and a safe renderer with a closed
  placeholder vocabulary per template.
- Threading the templates through prompt assembly and citation delivery, on the same code
  path the data plane and the previews already share.
- Validation at save time, with messages that say which placeholder is missing or unknown.
- The **Advanced** page: editor, per-template reset, live preview, organization defaults.
- A template fingerprint on the request log, so a wording change is a visible boundary in the
  log and a comparable variable for task 103.

## Out of scope

- **A general templating language.** No Jinja, no conditionals, no loops, no attribute access.
  The templates render tenant text (chunk contents, fact text) into a prompt; a template
  engine that evaluates expressions is a template engine that evaluates the wrong expression
  one day. Named placeholders substituted once, and nothing else.
- **Templating the client's own messages.** Layer 5 of §7 is the client's verbatim; wrapping
  a user turn in a template is a different product (and a way to break every client that
  sends structured content).
- **Per-model or per-target templates.** A gateway routing to two models gets one set of
  templates. Model-specific wording belongs in the model's `system_context`, which already
  exists and already layers above these blocks.
- **Templating the *memory facts' selection* or the retrieval query.** Those are the
  strategies in `MemoryConfig`; this task is about rendered text only.
- **Localising the UI.** The templates let a gateway speak German to its model; the control
  plane still speaks English to its operator.

## Work items

### The blob

- [ ] `app/schemas/gateway_config.py`: `TemplateConfig(ConfigBlob)` with nine string fields,
      each with today's text as its default so a gateway that never opens the page renders
      byte-identically to before:
      `reference_heading` (`## Reference material`), `reference_instruction` (the §7
      sentence), `excerpt` (`[{handle}] source: {source_name}{section}\n{text}`),
      `memory_heading` (`## What you know about this user`), `fact` (`- {text}`), and the
      response side: `sources_heading` (`Sources:`), `source_line`
      (`[{handle}] {label}`), `answer_prefix` (empty), `answer_suffix` (empty). Four of
      these are plain text; the other five take placeholders.
- [ ] `gateways.template_config` JSONB column, `'{}'` default, in a migration; permissive on
      load and strict on write through the same `ConfigBlob`/`merge_config` machinery as the
      other three blobs, so a PATCH sends the fields that changed and cannot wipe the rest.
- [ ] Placeholder vocabulary, closed and per template: `excerpt` gets `{handle}`,
      `{source_name}`, `{section}` (renders ` (p. 12)` or the empty string — the parenthesised
      form the default prints, so a template author does not have to express "if there is a
      section"), `{section_raw}` (bare), `{text}`, `{score}`; `fact` gets `{text}`;
      `source_line` gets `{handle}`, `{label}` (name plus section, linked when a URL exists),
      `{source_name}`, `{section}`, `{url}`; `answer_prefix`/`answer_suffix` get
      `{cited_count}`, `{injected_count}`, `{gateway}`, `{model}`. An unknown placeholder is a
      422 that names it and lists the ones allowed.
- [ ] The renderer, `app/services/templates.py`: `render(template, values) -> str` by regex
      substitution of `{name}` **once**, never `str.format` (attribute access, index access,
      and a `{` inside a chunk's text being re-parsed are all things `str.format` would do
      and none of them may happen). A literal brace is `{{`/`}}`. Substituted values are never
      re-scanned, so a document that contains `{text}` renders the four characters.
- [ ] Validation rules with reasons, in the schema so the form and the API agree:
      `excerpt` **must contain `[{handle}]`** (task 100's resolver and the model's ability to
      cite both depend on it); `source_line` must contain `{handle}` (do not renumber — §7.1);
      `fact` must contain `{text}` and be one line (`render_facts` flattens each fact for the
      injection reason it documents, and a template with a newline would undo that);
      `reference_instruction` may be anything including empty, but an empty one gets a
      *warning* on the page, not a refusal ("the model is no longer told to say when the
      documents do not answer — that sentence is the difference between a grounded assistant
      and a confident one").
- [ ] Length ceilings: 500 characters per template, 2 000 for the instruction. Prompts are
      billed per token, and a template is multiplied by `doc_top_k` on every request.

### Where it is used

- [ ] `assemble()` and `render_documents` / `render_entry` / `render_facts` take a
      `Templates` value (a frozen dataclass built from the blob) with the current constants
      as the default argument. Every existing call site and every golden file is unchanged by
      construction; the golden suite is the proof and stays byte-for-byte.
- [ ] `ResolvedGateway.templates`, decoded from the blob when the payload is built, like
      `memory`; `PAYLOAD_VERSION` bumps. `ProxyService._assemble` passes it. The budget still
      measures the *rendered* block, template included — a long heading spends
      `doc_max_tokens`, as it should.
- [ ] Citation delivery (task 100) takes the same value: `footer()` renders
      `sources_heading` and `source_line`; the `[{handle}]` the resolver looks for in the
      answer is unaffected because it is the model's text, but the resolver's *excerpt*
      numbering is whatever the excerpt template printed, which is why the template must keep
      the bracketed handle.
- [ ] `answer_prefix` / `answer_suffix`: applied by the same response stage as the footer,
      streaming and not. The prefix is the first content delta (it is static, so it costs
      nothing to time-to-first-token beyond one frame); the suffix follows the footer and
      precedes `[DONE]`. Both are outside the provider's `usage`, like the footer. Both are
      empty by default and, when empty, add no frame at all — the `off` path stays
      byte-identical.
- [ ] `memory_preview.py` — Try retrieval and the prompt preview — accepts a
      `template_config` patch beside `memory_config`, merged by the same function the save
      uses, so the Advanced page previews unsaved templates the way the Memory section
      previews unsaved knobs. The distillation prompt (task 13) is **not** templated here; it
      is not a gateway's text.
- [ ] The **template fingerprint**: a short hash of the effective nine strings, on
      `ResolvedGateway`, written to `request_logs.template_fingerprint` (a `String(16)`
      column beside `dropped_params`). The drawer shows it; the log list can filter by it.
      It is how "did the German instruction change anything" becomes a query rather than a
      memory, and it is the field task 103 records on an evaluation run.

### Organization defaults

- [ ] `organizations.settings["template_defaults"]`, a partial `TemplateConfig`, read when a
      gateway is created — the same pattern as `logging_defaults`, in the same place. Applied
      at creation only: a change to the organization's defaults does not rewrite existing
      gateways, and the page says so ("new gateways start from these").
- [ ] Organization settings page gains the same editor, minus the preview (there is no
      gateway to preview against) and minus the response prefix/suffix (those are per
      endpoint by nature).

### UI

- [ ] **Gateways → Advanced**, its own route (`/gateways/{id}/advanced`) reached from a link
      at the bottom of the Prompt section — *"Advanced: the text the gateway writes around
      documents, memory and answers →"* — rather than a seventh section on an already long
      editor. Own save button, own unsaved-changes guard (`useUnsavedChanges`), the same
      audit event as the gateway PATCH it is.
- [ ] Each template: a label that says where the text goes, a textarea, the placeholder
      chips for *that* template (click to insert at the cursor), the default shown greyed
      when the value differs, and a **Reset** link per field. A field that fails validation
      shows the server's message under it, the same message the API gives.
- [ ] A **preview** box at the top of the page — one question, the assembled prompt below,
      the citations examples from task 100 rendered with the current templates — driven by
      the same prompt-preview call with the unsaved templates as a patch. This is what makes
      the page safe to use: nobody has to save to see.
- [ ] Two warnings, shown inline and not blocking: the instruction is empty (see above); the
      excerpt no longer prints `{source_name}` ("the model can cite, but cannot name the
      document; the footer and the metadata still can").
- [ ] The request drawer shows the fingerprint beside the model name; the Monitoring filter
      row gains **Template** when more than one fingerprint appears in the window, listing
      them with first-seen dates, so an A/B of wording is two filters and a comparison.

### Spec

- [ ] Amend §7: the rendered shape is the default of a per-gateway template set; list the
      templates and their placeholders; state the two invariants (the excerpt keeps
      `[{handle}]`, facts stay one line). Amend §7.1 for the response prefix/suffix and the
      footer templates. Add the Advanced page to §13.1 and `template_config` to §17.

## Acceptance criteria

- [ ] A gateway that has never opened the Advanced page produces prompts byte-identical to
      the golden files, footers byte-identical to task 100's tests, and no extra frames.
- [ ] Changing `reference_heading` changes the transcript of the next request, the prompt
      preview, and the token count in both, in the same place.
- [ ] An excerpt template without `[{handle}]` is a 422 naming the rule; with it, citations
      resolve exactly as before under every template that keeps it.
- [ ] A chunk whose text contains `{text}` or `{{` renders those characters literally;
      `{handle.__class__}` in a template is a 422 (unknown placeholder), not an evaluation.
- [ ] `answer_prefix` arrives as the first content frame and `answer_suffix` after the footer
      and before `[DONE]`; the provider's `usage` is unchanged; with both empty, the stream is
      frame-for-frame what task 100 produced.
- [ ] Two gateways with different templates produce two different `template_fingerprint`
      values on their rows; editing a template changes the fingerprint on the next request
      and not on rows already written.
- [ ] A new gateway in an organization with `template_defaults` starts from them; an existing
      gateway does not change when the defaults do.
- [ ] Try retrieval and the prompt preview with an unsaved template patch render the patched
      block and save nothing.

## Tests

- Renderer: every placeholder per template, unknown placeholder refused, escaped braces,
  substituted values never re-scanned, `str.format`-style attribute access refused.
- Schema: each validation rule and its message; the length ceilings; the merge is partial.
- Golden parity: defaults render identically (the existing suite, unchanged, is the test).
- Assembly with a custom excerpt template: the budget measures the rendered form; `[n]`
  handles still start at 1 and are what the resolver finds.
- Response stage: prefix/suffix streamed and not, both dialects, ordering against the footer
  and `[DONE]`, empty means no frame.
- Fingerprint: stable for equal templates, different for different ones, on the row.
- Organization defaults at creation and only at creation.
- UI: chips insert, reset restores, validation message renders, preview sends the patch,
  unsaved guard fires.

## Notes

- **"Per project" is per gateway.** The product's unit of configuration is the gateway —
  it is what has a system context, a memory configuration and an audience — so that is where
  a template set lives, with organization defaults for the common case of one language across
  every endpoint. A per-model template would be the wrong layer (see out of scope) and a
  per-organization-only one would make the German and the English support bots fight.
- **Nine strings, not a DSL.** The temptation is a single template for the whole block with
  a loop over excerpts. It buys expressiveness nobody has asked for at the price of the token
  budget (which re-renders the block per candidate length and needs the per-excerpt unit),
  the citation invariant (which needs to know where the handle is), and the injection story
  (which needs substitution to be the only operation). A dozen placeholders in five places
  is the whole feature.
- **The defaults are the spec.** §7's rendered shape stays in the SPEC as the default and in
  the golden files as the test. What changes is that it is now *one* configuration among the
  ones a customer can have, and the transcript always shows which.
- **The fingerprint is the cheap half of an experiment.** Task 103 will do the expensive half.
  Until then, "monitoring, filtered by fingerprint, before and after" is already a comparison
  a person can make by eye, and it is one they cannot make at all today.
