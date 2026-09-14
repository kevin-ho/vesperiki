/**
 * GitHub-style alert callout plugin for remark.
 *
 * Detects `> [!NOTE]`, `> [!WARNING]`, `> [!TIP]`, `> [!IMPORTANT]`, and
 * `> [!CAUTION]` markers at the start of a blockquote and marks the
 * blockquote with a `callout` + `callout-<type>` class so the renderer
 * (and CSS) can present it as a coloured note. The marker text is
 * stripped from the first paragraph so it does not render twice; the
 * page reader's `CalloutBlockquote` component shows a small-caps
 * "NOTE" / "WARNING" / etc. label.
 *
 * We don't add remark-directive (it's not installed); this plugin only
 * handles the GitHub-style marker syntax.
 */
import { visit } from 'unist-util-visit';
import type { Root, Blockquote, Paragraph, PhrasingContent } from 'mdast';

export type CalloutType = 'NOTE' | 'TIP' | 'WARNING' | 'IMPORTANT' | 'CAUTION';

export const CALLOUT_TYPES: ReadonlySet<CalloutType> = new Set<CalloutType>([
  'NOTE',
  'TIP',
  'WARNING',
  'IMPORTANT',
  'CAUTION',
]);

// Case-insensitive match. `\s*` after the bracket swallows the
// newline/space that the `>` prefix leaves behind, so the remaining
// paragraph text starts cleanly on the callout body.
const CALLOUT_RE = /^\s*\[!(NOTE|TIP|WARNING|IMPORTANT|CAUTION)\]\s*/i;

export function isCalloutType(value: string): value is CalloutType {
  return CALLOUT_TYPES.has(value.toUpperCase() as CalloutType);
}

function firstTextChild(node: Paragraph): PhrasingContent | undefined {
	return node.children[0];
}

function calloutTypeFromNode(node: Blockquote): CalloutType | null {
	if (!node.children.length) return null;
	const first = node.children[0];
	if (!first || first.type !== 'paragraph') return null;
	const head = firstTextChild(first);
	if (!head || head.type !== 'text') return null;
	const match = head.value.match(CALLOUT_RE);
	if (!match) return null;
	return match[1].toUpperCase() as CalloutType;
}

export function remarkCallouts() {
	return (tree: Root) => {
		visit(tree, 'blockquote', (node: Blockquote) => {
			const type = calloutTypeFromNode(node);
			if (!type) return;
			// Strip the `[!TYPE]` marker (and trailing whitespace) from the
			// first text node so the body content is just the prose.
			const first = node.children[0] as Paragraph;
			const head = firstTextChild(first);
			if (head && head.type === 'text') {
				const match = head.value.match(CALLOUT_RE);
				if (match) {
					head.value = head.value.slice(match[0].length);
				}
				if (head.value === '' && first.children[0] === head) {
					// Marker was the entire paragraph (e.g. `> [!NOTE]\n>\n> Body`).
					// Drop the now-empty paragraph so the body starts cleanly.
					node.children.shift();
				}
			}
			// Tag the blockquote with the callout class names. The
			// `CalloutBlockquote` component reads `className` off the hast
			// `properties` bag at render time and renders a label + body.
			const existing = (node.data?.hProperties ?? {}) as Record<string, unknown>;
			const existingClass = existing.className;
			const classNames: string[] = Array.isArray(existingClass)
				? existingClass.filter((c): c is string => typeof c === 'string')
				: existingClass
					? [String(existingClass)]
					: [];
			classNames.push('callout', `callout-${type.toLowerCase()}`);
			node.data = {
				...(node.data ?? {}),
				hProperties: {
					...existing,
					className: classNames,
				},
			};
		});
	};
}
