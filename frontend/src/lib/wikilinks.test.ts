import { describe, expect, it } from 'vitest';
import { wikilinksToMarkdown } from './wikilinks';

describe('wikilinksToMarkdown', () => {
  it('rewrites a bare [[slug]] wikilink to a markdown link', () => {
    expect(wikilinksToMarkdown('See [[onboarding]] for context.')).toBe(
      'See [onboarding](/p/onboarding) for context.',
    );
  });

  it('rewrites [[slug|label]] using the provided label', () => {
    expect(wikilinksToMarkdown('Read [[onboarding|the welcome doc]].')).toBe(
      'Read [the welcome doc](/p/onboarding).',
    );
  });

  it('leaves a label containing brackets alone (the regex rejects them)', () => {
    // The label capture group is `[^\[\]\n]+`, so any inner bracket blocks
    // the match and the input is passed through unchanged.
    const input = 'Try [[guide|a [draft] walkthrough]] today.';
    expect(wikilinksToMarkdown(input)).toBe(input);
  });

  it('drops a wikilink whose slug is empty or whitespace', () => {
    expect(wikilinksToMarkdown('Oops [[ ]] and [[   |ignored]] gone.')).toBe(
      'Oops  and  gone.',
    );
  });

  it('rewrites multiple wikilinks in the same string', () => {
    const input = 'Link [[alpha]] and [[beta|other]] and [[gamma]].';
    const output = wikilinksToMarkdown(input);
    expect(output).toBe('Link [alpha](/p/alpha) and [other](/p/beta) and [gamma](/p/gamma).');
  });

  it('URL-encodes slugs so spaces survive', () => {
    expect(wikilinksToMarkdown('Visit [[my notes|the journal]].')).toBe(
      'Visit [the journal](/p/my%20notes).',
    );
  });

  it('leaves plain markdown links untouched', () => {
    const input = 'See [the docs](https://example.com) for details.';
    expect(wikilinksToMarkdown(input)).toBe(input);
  });
});