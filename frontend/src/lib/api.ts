import type { Graph, GraphNode, GraphEdge, Page, PagesResponse, SearchHit, SyncDelta, Tag, Correction, ApiErrorShape } from './types';
import { ApiError } from './types';

const base = (import.meta.env.VITE_API_BASE as string | undefined) ?? '';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: { accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!response.ok) {
    let payload: ApiErrorShape = { code: 'http_error', message: response.statusText };
    try {
      payload = (await response.json()) as ApiErrorShape;
    } catch {
      /* preserve HTTP error */
    }
    throw new ApiError(payload, response.status);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function queryString(params: Record<string, unknown>): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') query.set(key, String(value));
  }
  return query.size ? `?${query}` : '';
}

export const listPages = (
  params: { limit?: number; cursor?: number; tag?: string; type?: string; status?: string } = {},
) => request<PagesResponse>(`/api/pages${queryString(params)}`);

export const getPageBySlug = (slug: string) =>
  request<Page>(`/api/pages/${encodeURIComponent(slug)}`);

// include_body=true: the backend excludes `body` from list payloads by
// default, but the homepage recent-pages cards render description
// snippets from it.
export const getRecent = (limit = 20) =>
  request<PagesResponse>(
    // order=updated: newest-edited first, matching the offline (OPFS)
    // path's recency sort. The API's default `id` order is insertion
    // order, which froze the home page at the oldest-inserted pages.
    `/api/pages?limit=${limit}&status=active&include_body=true&order=updated`,
  );

export const search = (params: {
  q: string;
  tag?: string;
  type?: string;
  include_body?: boolean;
  limit?: number;
}) => request<SearchHit[]>(`/api/search${queryString(params)}`);

export const listTags = () => request<Tag[]>('/api/tags');

export const listOrphans = () =>
  request<Array<Pick<Page, 'slug' | 'title' | 'type'>>>('/api/orphans');

export const sync = (since = 0, limit?: number) =>
  request<SyncDelta>(
    `/api/sync?since=${since}${limit !== undefined ? `&limit=${limit}` : ''}`,
  );

export const getGraph = () => request<Graph>('/api/graph');

export const listCorrections = (status: 'pending' | 'resolved' | 'dismissed' = 'pending') =>
  request<Correction[]>(`/api/corrections${queryString({ status })}`);

export const createCorrection = (payload: {
  page_slug: string;
  selected_text: string;
  note?: string;
}) =>
  request<Correction>('/api/corrections', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });

export const resolveCorrection = (id: number, status: 'resolved' | 'dismissed') =>
  request<Correction>(`/api/corrections/${id}`, {
    method: 'PATCH',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ status }),
  });

/**
 * Filter search hits whose slug is in the deprecated set so the
 * search UI never shows superseded pages. Returns a new array; the
 * input is not mutated. `deprecatedSlugs` may be `undefined` (e.g.
 * before the deprecated-page lookup has resolved) in which case the
 * input is returned unchanged.
 */
export function filterDeprecatedHits<T extends { slug: string }>(
  hits: readonly T[],
  deprecatedSlugs: ReadonlySet<string> | undefined,
): T[] {
  if (!deprecatedSlugs) return [...hits];
  return hits.filter((hit) => !deprecatedSlugs.has(hit.slug));
}

/**
 * Resolve which pages link to `slug` by walking the full link graph.
 * The backend returns edges with integer `source`/`target` page IDs, so
 * we build a node-id → node map and match by ID, then return the
 * matching nodes. Returns an empty list on network failure.
 */
export async function getBacklinks(
  slug: string,
): Promise<Array<Pick<Page, 'slug' | 'title' | 'type'>>> {
  let graph: Graph;
  try {
    graph = await request<Graph>('/api/graph');
  } catch {
    return [];
  }
  const idToSlug = new Map<number, GraphNode['slug']>();
  for (const node of graph.nodes as GraphNode[]) {
    idToSlug.set(node.id, node.slug);
  }
  const targetId = [...idToSlug.entries()].find(([, s]) => s === slug)?.[0];
  if (targetId === undefined) return [];
  const sourceIds = new Set<number>();
  for (const edge of graph.edges as GraphEdge[]) {
    if (edge.target === targetId) sourceIds.add(edge.source);
  }
  return (graph.nodes as GraphNode[])
    .filter((node) => sourceIds.has(node.id))
    .map((node) => ({ slug: node.slug, title: node.title, type: node.type }));
}
