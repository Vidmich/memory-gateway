import { describe, expect, it } from 'vitest'

import {
  ORGANIZATION_TEMPLATE_NAMES,
  TEMPLATE_NAMES,
  insertPlaceholder,
  organizationTemplateForm,
  organizationTemplateSettings,
  shortFingerprint,
  templateForm,
  templatePatch,
  templateUseLabel,
  templateWarnings,
  templatesDiffer,
} from '@/pages/templates'
import { makeTemplateConfig, makeTemplateUse } from '@/test/factories'

describe('the form and the patch', () => {
  it('lists the nine templates, request side first', () => {
    expect(TEMPLATE_NAMES).toEqual([
      'reference_heading',
      'reference_instruction',
      'excerpt',
      'memory_heading',
      'fact',
      'sources_heading',
      'source_line',
      'answer_prefix',
      'answer_suffix',
    ])
    expect(ORGANIZATION_TEMPLATE_NAMES).not.toContain('answer_prefix')
    expect(ORGANIZATION_TEMPLATE_NAMES).not.toContain('answer_suffix')
  })

  it('sends only what changed, so the server merge cannot wipe the rest', () => {
    const saved = templateForm(makeTemplateConfig())
    const form = { ...saved, reference_heading: '## Referenzmaterial', answer_suffix: ' ✓' }

    expect(templatePatch(form, saved)).toEqual({
      reference_heading: '## Referenzmaterial',
      answer_suffix: ' ✓',
    })
    expect(templatePatch(saved, saved)).toEqual({})
    expect(templatesDiffer(form, saved)).toBe(true)
    expect(templatesDiffer(saved, saved)).toBe(false)
  })
})

describe('inserting a placeholder', () => {
  it('puts the token at the caret and leaves the caret after it', () => {
    expect(insertPlaceholder('[] source', 1, 1, 'handle')).toEqual({
      value: '[{handle}] source',
      caret: 9,
    })
  })

  it('replaces a selection', () => {
    expect(insertPlaceholder('[X] source', 1, 2, 'handle')).toEqual({
      value: '[{handle}] source',
      caret: 9,
    })
  })

  it('clamps a caret outside the value', () => {
    expect(insertPlaceholder('ab', 99, 99, 'text')).toEqual({ value: 'ab{text}', caret: 8 })
    expect(insertPlaceholder('ab', -3, -1, 'text')).toEqual({ value: '{text}ab', caret: 6 })
  })
})

describe('the warnings', () => {
  it('warns about an empty instruction and a nameless excerpt, and nothing otherwise', () => {
    const fine = templateWarnings(templateForm(makeTemplateConfig()))
    expect(fine).toEqual({})

    const warned = templateWarnings({ reference_instruction: '   ', excerpt: '[{handle}] {text}' })
    expect(warned.reference_instruction).toMatch(/grounded assistant/)
    expect(warned.excerpt).toMatch(/cannot name the document/)
  })
})

describe("the organization's defaults", () => {
  it('reads stored overrides over the platform defaults and ignores junk', () => {
    const platform = makeTemplateConfig()
    const form = organizationTemplateForm(
      { template_defaults: { reference_heading: '## Referenz', fact: 7, answer_prefix: 'x' } },
      platform,
    )
    expect(form.reference_heading).toBe('## Referenz')
    expect(form.fact).toBe(platform.fact)
    // Not one of the organization's templates, so not read even when stored.
    expect(form.answer_prefix).toBe('')
    expect(organizationTemplateForm(undefined, platform)).toEqual(templateForm(platform))
    expect(organizationTemplateForm({ template_defaults: 'nope' }, platform)).toEqual(
      templateForm(platform),
    )
  })

  it('stores only the difference from the platform defaults and keeps the other keys', () => {
    const platform = makeTemplateConfig()
    const form = { ...templateForm(platform), memory_heading: '## Über diese Person' }

    expect(
      organizationTemplateSettings(
        { logging_defaults: { retention_days: 7 }, template_defaults: { excerpt: 'old' } },
        form,
        platform,
      ),
    ).toEqual({
      logging_defaults: { retention_days: 7 },
      template_defaults: { memory_heading: '## Über diese Person' },
    })
    // Back to the defaults: the key goes, rather than storing an empty object.
    expect(
      organizationTemplateSettings(
        { template_defaults: { excerpt: 'old' } },
        templateForm(platform),
        platform,
      ),
    ).toEqual({})
  })
})

describe('the fingerprint on screen', () => {
  it('is shortened and absent when not recorded', () => {
    expect(shortFingerprint('0123456789abcdef')).toBe('01234567')
    expect(shortFingerprint(null)).toBe('—')
  })

  it('labels a template use with when it was first seen and how many requests', () => {
    const now = new Date('2026-09-11T12:00:00Z')
    const today = templateUseLabel(
      makeTemplateUse({
        fingerprint: 'aaaaaaaabbbbbbbb',
        first_seen: '2026-09-11T09:30:00Z',
        requests: 1,
      }),
      now,
    )
    expect(today).toMatch(/^aaaaaaaa · first seen today \d{1,2}:\d{2}( [AP]M)? · 1 request$/)
    const earlier = templateUseLabel(
      makeTemplateUse({
        fingerprint: 'ccccccccdddddddd',
        first_seen: '2026-09-02T09:30:00Z',
        requests: 1184,
      }),
      now,
    )
    expect(earlier).toMatch(/^cccccccc · first seen .+ · 1,184 requests$/)
    expect(earlier).not.toContain('today')
  })
})
