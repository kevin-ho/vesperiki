/**
 * Render `[[slug]]` and `[[slug|label]]` wikilinks as Markdown links. Slug is
 * validated (no whitespace, no brackets) so the produced Markdown is always
 * well-formed and safe to feed to react-markdown.
 */
export function wikilinksToMarkdown(body: string): string {
  return body.replace(
    /\[\[([^\[\]\n|]+)(?:\|([^\[\]\n]+))?\]\]/g,
    (_, rawSlug: string, rawLabel: string | undefined) => {
      const slug = rawSlug.trim();
      if (!slug) return '';
      const label = (rawLabel ?? slug).trim() || slug;
      const safeLabel = label.replace(/[\[\]]/g, '');
      return `[${safeLabel}](/p/${encodeURIComponent(slug)})`;
    },
  );
}