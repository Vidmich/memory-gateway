/**
 * Turning a display name into a URL-safe slug.
 *
 * Only a suggestion: the field stays editable because the slug ends up in a public
 * gateway URL (`/g/{slug}/v1`), and the customer may well want something other than a
 * mangled version of their legal name.
 *
 * In its own module so `OrganizationsPage` exports components and nothing else, which is
 * what keeps Fast Refresh working during development.
 */
export function suggestSlug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 63)
}
