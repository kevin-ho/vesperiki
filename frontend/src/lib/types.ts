export interface Page {
  slug: string;
  id?: number;
  title: string;
  type: string;
  tags: string[];
  sources: string[];
  body: string;
  status: string;
  updated_at: string;
  created_at?: string;
  seq?: number;
  redirected_from?: string;
  /**
   * Confidence score from the ingest pipeline (0..1). When absent or
   * 1.0 the UI shows no badge; values below 1.0 render a subtle
   * "Confidence: NN%" marker so reviewers know to double-check.
   */
  confidence?: number;
  verified_at?: string;
}

export interface Correction {
  id: number;
  page_slug: string;
  selected_text: string;
  note?: string | null;
  status: 'pending' | 'resolved' | 'dismissed';
  created_at: string;
  resolved_at?: string | null;
  resolved_by?: string | null;
}

export interface SearchHit {
  slug: string;
  title: string;
  type: string;
  snippet: string;
  score: number;
  body?: string;
}

export interface Tag {
  name: string;
  count: number;
}

export interface GraphNode {
  id: number;
  slug: string;
  title: string;
  type: string;
}

export interface GraphEdge {
  source: number;
  target: number;
  rel: string;
  origin: string;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface SyncDelta {
  cursor: number;
  pages: Page[];
  tombstones: { slug: string; seq: number; deleted_at: string }[];
  links: unknown[];
  page_tags: unknown[];
  tags: Tag[];
  aliases: unknown[];
}

export interface ApiErrorShape {
  code: string;
  message: string;
  details?: unknown;
}

export class ApiError extends Error {
  constructor(
    public readonly payload: ApiErrorShape,
    public readonly status: number,
  ) {
    super(payload.message);
    this.name = 'ApiError';
  }
}

export interface PagesResponse {
  pages: Page[];
  next_cursor: number | null;
}

/**
 * A single row of the offline sync log so the footer can show how
 * long ago the local copy was last refreshed, and whether it's
 * still in sync with the server.
 */
export interface SyncStatus {
  /** True when the local WASM DB is open and ready for queries. */
  ready: boolean;
  /** Last successful sync time (ISO 8601), or null if never synced. */
  lastSyncAt: string | null;
  /** Server change-seq cursor that the local copy has reached. */
  cursor: number;
  /**
   * Whether the page is currently considered "online" by the
   * browser. The data-source selection logic uses this together with
   * the WASM availability to decide which backend serves requests.
   */
  isOnline: boolean;
  /**
   * Status of a sync attempt that just finished or is in flight.
   * `'idle'` means no sync has been triggered yet; `'syncing'` means
   * one is currently running; `'ok'` and `'error'` are the result of
   * the most recent sync.
   */
  syncState: 'idle' | 'syncing' | 'ok' | 'error';
  /**
   * When the last sync errored, the human-readable message from the
   * most recent failed attempt. Cleared on the next successful sync.
   */
  lastError: string | null;
  /**
   * Whether the browser granted `navigator.storage.persist()`. We
   * ask on every cold start; the answer determines whether the
   * offline copy is durable or might be evicted.
   */
  persisted: boolean | null;
  /**
   * Search implementation chosen at init: FTS5 when the WASM build
   * supports it, otherwise a LIKE-based fallback. Surfaces in the UI
   * to make the trade-off honest.
   */
  searchStrategy: 'fts5' | 'like';
}

/**
 * A page the user has marked for offline availability. Persisted in
 * localStorage and rehydrated on every cold start. Used by the pin
 * toggle in the reader header to drive background media prefetch.
 */
export interface PinState {
  /** Slugs currently pinned for offline, in pin order (newest last). */
  slugs: string[];
}