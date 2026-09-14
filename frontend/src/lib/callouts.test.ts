import { describe, expect, it } from 'vitest';
import { unified, type Processor } from 'unified';
import remarkParse from 'remark-parse';
import remarkGfm from 'remark-gfm';
import { toHast } from 'mdast-util-to-hast';
import type { Element as HastElement, ElementContent, Root as HastRoot, Text } from 'hast';
import type { Root as MdastRoot } from 'mdast';
import { remarkCallouts, isCalloutType, CALLOUT_TYPES } from './callouts';

/**
 * Run the callout plugin through the real mdast pipeline and convert
 * the result to a hast tree. The plugin tags blockquote nodes via
 * `node.data.hProperties.className`; `mdast-util-to-hast` (see
 * `node_modules/mdast-util-to-hast/lib/state.js:356`) lifts those
 * `hProperties` onto the resulting hast element's `properties` bag.
 * Asserting on the hast output exercises the plugin's end-to-end
 * effect, which is what `react-markdown` (via `remark-rehype`) will
 * ultimately render.
 */
function render(source: string): HastRoot {
	const processor: Processor<MdastRoot> = unified()
		.use(remarkParse)
		.use(remarkGfm)
		.use(remarkCallouts);
	const tree = processor.parse(source) as MdastRoot;
	processor.runSync(tree);
	return toHast(tree, { allowDangerousHtml: true }) as HastRoot;
}

function classNamesOf(node: HastElement | undefined): string[] {
	if (!node) return [];
	const className = node.properties?.className;
	if (Array.isArray(className)) {
		return className.filter((c): c is string => typeof c === 'string');
	}
	if (typeof className === 'string') return [className];
	return [];
}

function findBlockquote(node: ElementContent | HastRoot): HastElement | undefined {
	if ('type' in node && node.type === 'element' && node.tagName === 'blockquote') {
		return node as HastElement;
	}
	if ('children' in node && Array.isArray(node.children)) {
		for (const child of node.children) {
			const found = findBlockquote(child as ElementContent);
			if (found) return found;
		}
	}
	return undefined;
}

function blockquoteHtml(source: string): HastElement | undefined {
	return findBlockquote(render(source));
}

function hastText(node: HastElement): string {
	let out = '';
	for (const child of node.children) {
		if ('type' in child && child.type === 'text') {
			out += (child as Text).value;
		} else if ('type' in child && child.type === 'element') {
			out += hastText(child as HastElement);
		}
	}
	return out;
}

describe('remarkCallouts', () => {
	it('tags a `[!NOTE]` blockquote with the callout classes', () => {
		const blockquote = blockquoteHtml('> [!NOTE]\n> Heads up.');
		expect(blockquote).toBeDefined();
		const classes = classNamesOf(blockquote);
		expect(classes).toContain('callout');
		expect(classes).toContain('callout-note');
	});

	it('strips the `[!NOTE]` marker from the first paragraph', () => {
		const blockquote = blockquoteHtml('> [!NOTE]\n> Heads up.');
		expect(blockquote).toBeDefined();
		expect(hastText(blockquote!).trim()).toBe('Heads up.');
		// And the literal marker must not appear in the rendered text.
		const flat = JSON.stringify(blockquote);
		expect(flat).not.toContain('[!NOTE]');
	});

	it('recognises all five GitHub callout types case-insensitively', () => {
		for (const type of CALLOUT_TYPES) {
			const blockquote = blockquoteHtml(`> [!${type}]\n> Body.`);
			expect(blockquote, `expected a callout for ${type}`).toBeDefined();
			const classes = classNamesOf(blockquote);
			expect(classes).toContain(`callout-${type.toLowerCase()}`);
		}
		const blockquote = blockquoteHtml('> [!note]\n> Body.');
		expect(blockquote).toBeDefined();
		expect(classNamesOf(blockquote)).toContain('callout-note');
	});

	it('leaves plain blockquotes untouched', () => {
		const blockquote = blockquoteHtml('> Just a regular quote.\n> More.');
		expect(blockquote).toBeDefined();
		expect(classNamesOf(blockquote)).not.toContain('callout');
	});

	it('does not match a callout marker in the middle of the paragraph', () => {
		const blockquote = blockquoteHtml('> Lead text.\n> [!NOTE]\n> Tail.');
		expect(blockquote).toBeDefined();
		expect(classNamesOf(blockquote)).not.toContain('callout');
	});

	it('drops the now-empty paragraph when the marker is on its own line', () => {
		const blockquote = blockquoteHtml('> [!NOTE]\n>\n> Body text.');
		expect(blockquote).toBeDefined();
		expect(hastText(blockquote!).trim()).toBe('Body text.');
		expect(JSON.stringify(blockquote)).not.toContain('[!NOTE]');
	});
});

describe('isCalloutType', () => {
	it('accepts the five known callout types', () => {
		for (const type of CALLOUT_TYPES) {
			expect(isCalloutType(type)).toBe(true);
			expect(isCalloutType(type.toLowerCase())).toBe(true);
		}
	});

	it('rejects unknown types', () => {
		expect(isCalloutType('DANGER')).toBe(false);
		expect(isCalloutType('')).toBe(false);
	});
});
