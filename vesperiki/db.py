"""SQLite connection and schema initialization for Vesperiki."""

from __future__ import annotations

import fcntl
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from textwrap import dedent
from typing import Any


# Schema: a standard FTS5 table, never external-content — database
# triggers own its maintenance.
SCHEMA_SQL = dedent(
    """\
    CREATE TABLE change_seq (
        id      INTEGER PRIMARY KEY CHECK (id = 1),
        value   INTEGER NOT NULL DEFAULT 0
    );
    INSERT INTO change_seq (id, value) VALUES (1, 0);

    -- Every mutating transaction does, atomically:
    --   BEGIN IMMEDIATE;                          -- <-- mandatory, see below
    --   UPDATE change_seq SET value = value + 1;
    --   ... write rows, stamping seq = (SELECT value FROM change_seq) ...
    --   COMMIT;
    --
    -- BEGIN IMMEDIATE is not optional (rev 13). A DEFERRED transaction that reads
    -- before it writes takes a read lock, then must upgrade to a write lock — and
    -- SQLite does NOT invoke the busy handler for that upgrade. It returns
    -- SQLITE_BUSY (SQLITE_BUSY_SNAPSHOT under WAL) immediately, by design, to avoid
    -- deadlock. So `busy_timeout` does not help here at all, and a blind retry loop
    -- spins forever on a stale snapshot. IMMEDIATE takes the write lock up front,
    -- where busy_timeout DOES apply and the write simply queues.

    -- ════════════════════════════════════════════════════════
    -- EXTENSIBLE VOCABULARIES — lookup tables, not CHECK constraints (rev 14)
    -- ════════════════════════════════════════════════════════
    -- SQLite cannot ALTER a CHECK constraint. Adding a sixth page type to a CHECK
    -- means the 12-step table rebuild (create new, copy, drop, rename, recreate
    -- every index and trigger) — for every install, including other people's. §17
    -- requires no domain vocabulary welded into the public surface, and these three
    -- lists ARE domain vocabulary: they're the first thing any other user changes.
    --
    -- Lookup tables give DB-enforced integrity AND extension by INSERT. This is the
    -- established preference in this schema (guard triggers over app checks, no
    -- external-content FTS) — enforcement lives in the database, not in a rule the
    -- application has to remember.
    --
    -- NOTE: this reverses rev 10, which called a lookup table for five fixed values
    -- over-normalization. That was correct GIVEN the premise that the values were
    -- fixed. §17 changed the premise. The reversal follows the new requirement.
    CREATE TABLE page_types (
        name        TEXT PRIMARY KEY,
        description TEXT
    );
    INSERT INTO page_types (name, description) VALUES
        ('entity',    'A named thing that exists in our world'),
        ('guide',     'How to do something'),
        ('concept',   'An idea, principle, or decision'),
        ('reference', 'Lookup material'),
        ('log',       'Time-stamped record'),
        ('project',   'A planned undertaking with goals, scope, and milestones');

    CREATE TABLE link_rels (
        name        TEXT PRIMARY KEY,
        description TEXT
    );
    INSERT INTO link_rels (name, description) VALUES
        ('references',  'Mentions or points at'),
        ('depends_on',  'Requires to function'),
        ('part_of',     'Component of a larger thing'),
        ('supersedes',  'Replaces an earlier page'),
        ('contradicts', 'Conflicts with — flag for reconciliation');

    CREATE TABLE source_types (
        name        TEXT PRIMARY KEY,
        description TEXT
    );
    INSERT INTO source_types (name, description) VALUES
        ('conversation', 'Emerged from a session with a human'),
        ('research',     'Agent researched it'),
        ('cron',         'Scheduled job'),
        ('manual',       'Directly requested'),
        ('migration',    'Imported'),
        ('correction',   'Fixing a prior error');

    -- Kept as CHECK constraints (genuinely internal machinery, every value maps to
    -- a code path, no consumer will want to extend them): pages.status,
    -- revisions.change_type.

    -- ════════════════════════════════════════════════════════
    -- PAGES — the core content. Current state only.
    -- ════════════════════════════════════════════════════════
    CREATE TABLE pages (
        id          INTEGER PRIMARY KEY,
        slug        TEXT UNIQUE NOT NULL,      -- flat: 'edge-router', NOT 'entities/edge-router'
        title       TEXT NOT NULL,
        title_norm  TEXT NOT NULL,             -- lowercased, punctuation/space-stripped.
                                               -- Dedup gate signal (§7 "Dedup is a gate").
        body        TEXT NOT NULL,             -- markdown. Current state = copy of latest revision.
        type        TEXT NOT NULL DEFAULT 'entity'
            REFERENCES page_types(name),       -- extensible: INSERT a new type, no rebuild
        status      TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'stale', 'deprecated')),

        -- Page-level metadata
        sources     TEXT,                      -- JSON array of {url, title, note}: bibliography.
                                               -- Bounded in practice (5-20 entries/page). Shape
                                               -- validated on write; not queried relationally.
        metadata    TEXT,                      -- JSON: escape hatch. Promote consistent keys to real columns.

        -- Hot-read timestamps (justified denormalization — read 1000x more than written)
        updated_at  TEXT DEFAULT (datetime('now')),  -- = latest revision's changed_at. Display/sort only.
        verified_at TEXT,                      -- last confirmed-accurate (staleness, v2)
        confidence  REAL DEFAULT 1.0,          -- 0.0-1.0, agent-set (v2)

        seq         INTEGER NOT NULL DEFAULT 0 -- sync cursor. Bumped on every write to this row.
    );

    -- Removed from pages (redundant with first revision):
    --   created_by  → revisions.changed_by WHERE change_type='create'
    --   created_at  → revisions.changed_at WHERE change_type='create'

    -- ════════════════════════════════════════════════════════
    -- SLUG ALIASES — renames without breaking anything
    -- ════════════════════════════════════════════════════════
    -- Deep links, PWA caches, agent memory, and inline body links all reference
    -- pages by slug. Renaming must not 404 any of them. On rename: insert the old
    -- slug as an alias. Resolution order: pages.slug, then slug_aliases.
    CREATE TABLE slug_aliases (
        alias       TEXT PRIMARY KEY,
        page_id     INTEGER NOT NULL,
        created_at  TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (page_id) REFERENCES pages(id) ON DELETE CASCADE
    );

    -- Slug and alias share ONE namespace (rev 13). Without this, renaming
    -- 'alpha' → 'beta' and later creating a fresh `alpha` page is
    -- accepted silently: resolution checks pages first, so every old deep link now
    -- lands on an unrelated page. A wrong page is worse than a 404 — nothing
    -- surfaces the error, the reader just shows something plausible. Enforced at the
    -- DB so no write path can bypass it.
    CREATE TRIGGER pages_slug_guard BEFORE INSERT ON pages
    WHEN EXISTS (SELECT 1 FROM slug_aliases WHERE alias = new.slug)
    BEGIN SELECT RAISE(ABORT, 'slug collides with a retired alias'); END;

    CREATE TRIGGER pages_slug_guard_upd BEFORE UPDATE OF slug ON pages
    WHEN EXISTS (SELECT 1 FROM slug_aliases WHERE alias = new.slug AND page_id != new.id)
    BEGIN SELECT RAISE(ABORT, 'slug collides with a retired alias'); END;

    CREATE TRIGGER alias_slug_guard BEFORE INSERT ON slug_aliases
    WHEN EXISTS (SELECT 1 FROM pages WHERE slug = new.alias)
    BEGIN SELECT RAISE(ABORT, 'alias collides with a live slug'); END;

    -- Deliberate reuse is still possible, but must state intent:
    --   wiki_write(action="create", slug="alpha", release_alias=true)
    -- drops the alias inside the same transaction, then creates. Never implicit.
    --
    -- Purist alternative, rejected: a single slugs(slug PK, page_id, is_canonical)
    -- table makes collision impossible by construction, but adds a join to the
    -- hottest read path and a larger migration for the same guarantee.

    -- ════════════════════════════════════════════════════════
    -- TAGS — first-class entities
    -- ════════════════════════════════════════════════════════
    CREATE TABLE tags (
        id          INTEGER PRIMARY KEY,
        name        TEXT UNIQUE NOT NULL       -- 'infrastructure', 'proxy', 'self-hosted'
    );

    CREATE TABLE page_tags (
        page_id     INTEGER NOT NULL,
        tag_id      INTEGER NOT NULL,
        PRIMARY KEY (page_id, tag_id),
        FOREIGN KEY (page_id) REFERENCES pages(id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
    );

    -- ════════════════════════════════════════════════════════
    -- LINKS — typed relationships (not [[text]] parsed from body)
    -- ════════════════════════════════════════════════════════
    -- `rel` is what makes this a graph rather than an untyped adjacency list.
    -- An untyped edge + a free-text comment is barely better than a wikilink.
    -- `origin` matters operationally: derived links are rebuilt from the body on
    -- every write; explicit links are agent-declared and must survive that rebuild.
    CREATE TABLE links (
        source_id   INTEGER NOT NULL,
        target_id   INTEGER NOT NULL,
        rel         TEXT NOT NULL DEFAULT 'references'
            REFERENCES link_rels(name),        -- extensible (rev 14): your graph
                                               -- vocabulary will outgrow five values
        origin      TEXT NOT NULL DEFAULT 'derived'
            CHECK (origin IN ('derived', 'explicit')),
        context     TEXT,                      -- why this link exists (optional)
        PRIMARY KEY (source_id, target_id, rel),
        FOREIGN KEY (source_id) REFERENCES pages(id) ON DELETE CASCADE,
        FOREIGN KEY (target_id) REFERENCES pages(id) ON DELETE CASCADE
    );

    -- ════════════════════════════════════════════════════════
    -- REVISIONS — full history + structured provenance per change
    -- ════════════════════════════════════════════════════════
    -- `body` is the POST-change state (see "Body sync convention" below).
    -- Latest revision body == pages.body, always.
    CREATE TABLE revisions (
        id              INTEGER PRIMARY KEY,
        page_id         INTEGER NOT NULL,
        body            TEXT NOT NULL,             -- page body AFTER this change

        -- Writer identity (rev 12). Two columns, not one namespaced string.
        -- Both are SERVER-ASSIGNED from the MCP process environment, never agent-supplied.
        changed_by      TEXT NOT NULL,             -- who:  'alice', 'ci-bot', 'default'
        client          TEXT NOT NULL,             -- which client wrote this: 'cli', 'claude-code', 'cursor'
        changed_at      TEXT DEFAULT (datetime('now')),

        -- Structured provenance for THIS change
        change_type     TEXT NOT NULL
            CHECK (change_type IN ('create', 'update', 'verify', 'migrate', 'correct', 'delete')),
        change_summary  TEXT,                      -- human-readable: "Updated dependency version to v3.8.12"
        session_id      TEXT,                      -- which agent session triggered this change
        source_type     TEXT
            REFERENCES source_types(name),     -- extensible (rev 14): a consumer will
                                               -- want 'webhook' or 'import'
        source_refs     TEXT,                      -- JSON array: URLs, session links, citation IDs
        trigger         TEXT,                      -- what prompted this: "User asked about X"

        FOREIGN KEY (page_id) REFERENCES pages(id) ON DELETE CASCADE
    );

    -- ════════════════════════════════════════════════════════
    -- TOMBSTONES — purge only (rev 13)
    -- ════════════════════════════════════════════════════════
    -- Rev 11 added this table for "deletions," but every delete path in the design
    -- is soft — so nothing ever wrote a row here. Dead machinery.
    --
    -- Corrected model: SOFT DELETE NEEDS NO TOMBSTONE. `status='deprecated'` bumps
    -- `seq`, the page row rides down the normal page delta, and the client filters
    -- it out of browse. The existing sync path already handles it completely.
    --
    -- Tombstones exist for exactly one operation: PURGE (see §6 "Delete vs purge").
    CREATE TABLE tombstones (
        slug        TEXT PRIMARY KEY,
        seq         INTEGER NOT NULL,
        deleted_at  TEXT DEFAULT (datetime('now'))
    );
    -- Re-creating a purged slug MUST clear its tombstone in the same transaction,
    -- or a client replaying both sets deletes the freshly created page:
    --   DELETE FROM tombstones WHERE slug = ?;   -- inside every create
    -- Clients apply pages and tombstones interleaved in `seq` order, not in phases —
    -- correct for any purge/re-create/purge sequence rather than only the common one.

    -- `links` and `page_tags` deletions are handled differently: both tables are
    -- small, so /api/sync full-resends them every call. No tombstones,
    -- no per-edge change tracking. Revisit if the wiki grows large enough
    -- that these tables become a meaningful fraction of sync payload.

    -- ════════════════════════════════════════════════════════
    -- FULL-TEXT SEARCH INDEX (standard FTS5, not external-content)
    -- ════════════════════════════════════════════════════════
    -- Deliberately NOT content='pages'. External-content FTS5 does not
    -- self-update: it needs hand-written delete/insert trigger pairs that echo the
    -- OLD column values, and any write path that bypasses them rots the index
    -- silently — the exact failure class this project exists to escape. A standard
    -- FTS5 table duplicates the body (storage cost is proportional to corpus size)
    -- and is maintainable with plain UPDATEs. It also lets us index a `tags` column,
    -- which external-content cannot (pages has no tags column to read from).
    CREATE VIRTUAL TABLE pages_fts USING fts5(
        slug  UNINDEXED,
        title,
        tags,                                  -- space-joined tag names, trigger-maintained
        body,
        tokenize = "unicode61 remove_diacritics 2 tokenchars '_-'",
        prefix = '2 3'
    );
    -- prefix='2 3' (rev 13): §7b promises per-keystroke `title MATCH 'qua*'`. FTS5
    -- prefix indexes must be declared at creation — adding them later is a full
    -- index rebuild. Free to include now.
    --
    -- tokenchars '_-' (rev 13): without it, `edge-router` and `journal_mode` each
    -- split into two tokens. This corpus is full of identifiers and hyphenated slugs.
    --
    -- Porter stemming DROPPED (rev 13), deliberately. It's a poor fit for a corpus
    -- of proper nouns and code identifiers (edge-router,
    -- busy_timeout). Cost is real and acknowledged: "deploy" no longer matches
    -- "deployment", which is exactly the fuzzy-recall behavior §7b promised the
    -- human track. That recall is reassigned to the v2 semantic layer, which §7b
    -- already scopes to the human track. Precision now, recall later — rather than
    -- a stemmer that half-serves both.
    -- rowid is kept equal to pages.id by the triggers below.
    -- Human-tuned ranking (§7b): bm25(pages_fts, 0.0, 5.0, 3.0, 1.0)
    --   slug=0 (unindexed), title=5x, tags=3x, body=1x

    CREATE TRIGGER pages_fts_ai AFTER INSERT ON pages BEGIN
        INSERT INTO pages_fts (rowid, slug, title, tags, body)
        VALUES (new.id, new.slug, new.title, '', new.body);
    END;

    CREATE TRIGGER pages_fts_au AFTER UPDATE ON pages BEGIN
        UPDATE pages_fts
           SET slug = new.slug, title = new.title, body = new.body
         WHERE rowid = new.id;
    END;

    CREATE TRIGGER pages_fts_ad AFTER DELETE ON pages BEGIN
        DELETE FROM pages_fts WHERE rowid = old.id;
    END;

    -- Tag membership changes must refresh the denormalized tags column.
    CREATE TRIGGER page_tags_fts_ai AFTER INSERT ON page_tags BEGIN
        UPDATE pages_fts
           SET tags = COALESCE((SELECT group_concat(t.name, ' ')
                                  FROM page_tags pt JOIN tags t ON t.id = pt.tag_id
                                 WHERE pt.page_id = new.page_id), '')
         WHERE rowid = new.page_id;
    END;

    CREATE TRIGGER page_tags_fts_ad AFTER DELETE ON page_tags BEGIN
        UPDATE pages_fts
           SET tags = COALESCE((SELECT group_concat(t.name, ' ')
                                  FROM page_tags pt JOIN tags t ON t.id = pt.tag_id
                                 WHERE pt.page_id = old.page_id), '')
         WHERE rowid = old.page_id;
    END;

    -- Renaming a tag must refresh every page carrying it (§6 claims tag rename is
    -- a one-statement operation — this trigger is what makes that true for search).
    --
    -- The inner tables are aliased pt2/t2 deliberately (rev 13). `pages_fts.rowid`
    -- is an outward reference to the UPDATE target from inside a correlated
    -- subquery; it resolves correctly, but with `pt`/`t` reused inside it the line
    -- is one careless rewrite away from binding to the wrong table and silently
    -- writing the wrong tags. Distinct aliases remove the ambiguity entirely.
    -- This is the most breakable statement in the schema — it gets a dedicated test
    -- (page with 3 tags → rename the middle one → assert findable by the new name,
    -- not the old). Same test shape for page_tags_fts_ai / _ad.
    CREATE TRIGGER tags_fts_au AFTER UPDATE OF name ON tags BEGIN
        UPDATE pages_fts
           SET tags = (SELECT COALESCE(group_concat(t2.name, ' '), '')
                         FROM page_tags pt2 JOIN tags t2 ON t2.id = pt2.tag_id
                        WHERE pt2.page_id = pages_fts.rowid)
         WHERE rowid IN (SELECT page_id FROM page_tags WHERE tag_id = new.id);
    END;

    -- Rejected alternative: move FTS tag maintenance into application code
    -- (`refresh_fts_tags(page_id)`) and let triggers own only title/body. Less
    -- trigger cleverness, but it reintroduces the "app must remember or the index
    -- rots silently" failure mode that rev 11 removed by dropping external-content
    -- FTS. Enforcement stays in the database.
    --
    -- Migration note: these triggers fire per row, so the bulk import will be slow.
    -- Drop and recreate them around the bulk import, or accept the per-row overhead.

    -- ════════════════════════════════════════════════════════
    -- INDEXES (hot query paths)
    -- ════════════════════════════════════════════════════════
    CREATE INDEX idx_pages_type       ON pages(type);
    CREATE INDEX idx_pages_status     ON pages(status);
    CREATE INDEX idx_pages_updated    ON pages(updated_at);
    CREATE INDEX idx_pages_verified   ON pages(verified_at);
    CREATE INDEX idx_pages_seq        ON pages(seq);            -- sync delta scans
    CREATE INDEX idx_pages_title_norm ON pages(title_norm);     -- dedup gate exact-match probe
    CREATE INDEX idx_revisions_page   ON revisions(page_id, changed_at);
    CREATE INDEX idx_revisions_session ON revisions(session_id);
    CREATE INDEX idx_revisions_create ON revisions(page_id, change_type);  -- origin lookup
    CREATE INDEX idx_revisions_author ON revisions(changed_by, changed_at); -- per-author history lookup
    CREATE INDEX idx_revisions_client ON revisions(client, changed_at);     -- per-client history lookup
    CREATE INDEX idx_pagetags_tag     ON page_tags(tag_id);
    CREATE INDEX idx_links_target     ON links(target_id);      -- for backlink queries
    CREATE INDEX idx_slug_aliases_page ON slug_aliases(page_id);
    CREATE INDEX idx_tombstones_seq   ON tombstones(seq);

    CREATE TABLE media (
        id          INTEGER PRIMARY KEY,
        page_id     INTEGER NOT NULL,
        filename    TEXT,
        mime_type   TEXT,
        byte_size   INTEGER,
        sha256      TEXT,                  -- dedup identical uploads
        data        BLOB,
        created_at  TEXT DEFAULT (datetime('now')),
        FOREIGN KEY (page_id) REFERENCES pages(id) ON DELETE CASCADE
    );
    CREATE INDEX idx_media_page ON media(page_id);
    CREATE UNIQUE INDEX idx_media_sha ON media(sha256);

    -- Correction queue: one-tap typo/error flag from the reader. The actual
    -- page edit is performed by an agent later (authorship stays with agents,
    -- who record change_type='correct' and put the human in `trigger`).
    -- No ON DELETE for the resolved_by side: this is client bookkeeping, not
    -- page state, and the queue must outlive a writer rename. ON DELETE
    -- CASCADE on page_slug so a purged page's pending flags vanish with it.
    CREATE TABLE corrections (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        page_slug     TEXT NOT NULL,
        selected_text TEXT NOT NULL,
        note          TEXT,
        status        TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'resolved', 'dismissed')),
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        resolved_at   TEXT,
        resolved_by   TEXT,
        FOREIGN KEY (page_slug) REFERENCES pages(slug) ON DELETE CASCADE
    );
    CREATE INDEX idx_corrections_status ON corrections(status);
    """
)

# Idempotent migrations run after first-time schema creation. Fresh DBs get
# the corrections table from SCHEMA_SQL above; existing DBs only see it here.
# Keep these as a single block so init_db's verification sees a consistent
# schema on every open.
_MIGRATIONS_SQL = dedent(
    """\
    CREATE TABLE IF NOT EXISTS corrections (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        page_slug     TEXT NOT NULL,
        selected_text TEXT NOT NULL,
        note          TEXT,
        status        TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'resolved', 'dismissed')),
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        resolved_at   TEXT,
        resolved_by   TEXT,
        FOREIGN KEY (page_slug) REFERENCES pages(slug) ON DELETE CASCADE
    );
    CREATE INDEX IF NOT EXISTS idx_corrections_status ON corrections(status);
    """
)

_REQUIRED_OBJECTS = {
    "table": {
        "change_seq", "page_types", "source_types", "link_rels", "pages",
        "slug_aliases", "tags", "page_tags", "links", "revisions",
        "tombstones", "media", "pages_fts", "corrections",
    },
    "index": {
        "idx_pages_type", "idx_pages_status", "idx_pages_updated",
        "idx_pages_verified", "idx_pages_seq", "idx_pages_title_norm",
        "idx_revisions_page", "idx_revisions_session", "idx_revisions_create",
        "idx_revisions_author", "idx_revisions_client", "idx_pagetags_tag",
        "idx_links_target", "idx_slug_aliases_page", "idx_tombstones_seq",
        "idx_media_page", "idx_media_sha", "idx_corrections_status",
    },
    "trigger": {
        "pages_slug_guard", "pages_slug_guard_upd", "alias_slug_guard",
        "pages_fts_ai", "pages_fts_au", "pages_fts_ad",
        "page_tags_fts_ai", "page_tags_fts_ad", "tags_fts_au",
    },
}

_EXPECTED_COLUMNS = {
    "change_seq": ("id", "value"),
    "page_types": ("name", "description"),
    "source_types": ("name", "description"),
    "link_rels": ("name", "description"),
    "pages": (
        "id", "slug", "title", "title_norm", "body", "type", "status",
        "sources", "metadata", "updated_at", "verified_at", "confidence", "seq",
    ),
    "slug_aliases": ("alias", "page_id", "created_at"),
    "tags": ("id", "name"),
    "page_tags": ("page_id", "tag_id"),
    "links": ("source_id", "target_id", "rel", "origin", "context"),
    "revisions": (
        "id", "page_id", "body", "changed_by", "client", "changed_at",
        "change_type", "change_summary", "session_id", "source_type",
        "source_refs", "trigger",
    ),
    "tombstones": ("slug", "seq", "deleted_at"),
    "media": (
        "id", "page_id", "filename", "mime_type", "byte_size", "sha256",
        "data", "created_at",
    ),
    "pages_fts": ("slug", "title", "tags", "body"),
    "corrections": (
        "id", "page_slug", "selected_text", "note", "status",
        "created_at", "resolved_at", "resolved_by",
    ),
}

_SEEDS = {
    "page_types": {"entity", "guide", "concept", "reference", "log", "project"},
    "source_types": {
        "conversation", "research", "cron", "manual", "migration", "correction",
    },
    "link_rels": {
        "references", "depends_on", "part_of", "supersedes", "contradicts",
    },
}

# Invariants: both link endpoints, revisions, aliases, tags, and media
# rely on database cascades. These checks catch a subtly incompatible existing DB.
_REQUIRED_FOREIGN_KEYS = {
    "pages": {("type", "page_types", "name", "NO ACTION")},
    "slug_aliases": {("page_id", "pages", "id", "CASCADE")},
    "page_tags": {
        ("page_id", "pages", "id", "CASCADE"),
        ("tag_id", "tags", "id", "CASCADE"),
    },
    "links": {
        ("source_id", "pages", "id", "CASCADE"),
        ("target_id", "pages", "id", "CASCADE"),
        ("rel", "link_rels", "name", "NO ACTION"),
    },
    "revisions": {
        ("page_id", "pages", "id", "CASCADE"),
        ("source_type", "source_types", "name", "NO ACTION"),
    },
    "media": {("page_id", "pages", "id", "CASCADE")},
    "corrections": {("page_slug", "pages", "slug", "CASCADE")},
}


def _configure_connection(conn: sqlite3.Connection) -> None:
    """Apply the mandatory pragmas to a newly opened connection."""
    # Without this, SQLite silently ignores foreign keys and cascades.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL permits concurrent readers/writer; contention waits five seconds;
    # NORMAL is set explicitly rather than relying on the WAL default.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.row_factory = sqlite3.Row


def _has_schema_objects(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None


def _verify_schema(conn: sqlite3.Connection) -> None:
    problems: list[str] = []
    objects: dict[str, set[str]] = {}
    for object_type, required in _REQUIRED_OBJECTS.items():
        objects[object_type] = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = ?", (object_type,)
            )
        }
        missing = required - objects[object_type]
        if missing:
            problems.append(
                f"missing {object_type}s: {', '.join(sorted(missing))}"
            )

    if problems:
        raise RuntimeError("database schema verification failed: " + "; ".join(problems))

    for table, expected in _EXPECTED_COLUMNS.items():
        actual = tuple(
            row["name"] for row in conn.execute(f'PRAGMA table_info("{table}")')
        )
        if actual != expected:
            problems.append(f"unexpected columns for {table}: {actual!r}")

    # change_seq is the id=1 singleton. Its value may have advanced.
    seq_rows = conn.execute("SELECT id, value FROM change_seq").fetchall()
    if len(seq_rows) != 1 or seq_rows[0]["id"] != 1:
        problems.append("change_seq is not the id=1 singleton")

    # The three lookup vocabularies are extensible: seed by INSERT, never CHECK.
    for table, expected in _SEEDS.items():
        actual = {row["name"] for row in conn.execute(f'SELECT name FROM "{table}"')}
        missing = expected - actual
        if missing:
            problems.append(f"missing {table} seeds: {', '.join(sorted(missing))}")
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()["sql"]
        if "CHECK" in table_sql.upper():
            problems.append(f"{table} must be extensible, not CHECK-constrained")

    for table, required in _REQUIRED_FOREIGN_KEYS.items():
        actual = {
            (row["from"], row["table"], row["to"], row["on_delete"])
            for row in conn.execute(f'PRAGMA foreign_key_list("{table}")')
        }
        missing = required - actual
        if missing:
            problems.append(f"missing foreign keys for {table}: {sorted(missing)!r}")

    # SHA-256 is uniquely indexed so identical media deduplicates.
    media_indexes = {
        row["name"]: row["unique"]
        for row in conn.execute('PRAGMA index_list("media")')
    }
    if media_indexes.get("idx_media_sha") != 1:
        problems.append("idx_media_sha must be UNIQUE")

    fts_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pages_fts'"
    ).fetchone()["sql"]
    if "content=" in fts_sql.lower():
        problems.append("pages_fts must be standard FTS5, not external-content")

    # pt2/t2 are deliberate aliases in this correlated subquery.
    tag_trigger_sql = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'trigger' AND name = 'tags_fts_au'"
    ).fetchone()["sql"]
    if not all(alias in tag_trigger_sql for alias in ("pt2", "t2")):
        problems.append("tags_fts_au must retain the pt2/t2 aliases")

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        problems.append("existing rows violate foreign keys")

    if problems:
        raise RuntimeError("database schema verification failed: " + "; ".join(problems))


def init_db(path: str | Path) -> sqlite3.Connection:
    """Open *path*, initialize an empty database, and verify its schema."""
    conn = sqlite3.connect(str(path))
    try:
        if not _has_schema_objects(conn):
            # First-time init path: serialize across processes/threads.
            # Both _configure_connection (PRAGMA journal_mode = WAL on a fresh
            # file takes a write lock) and the schema executescript must be
            # inside the same flock — otherwise a thread that lost the pragma
            # race fails before the schema flock is reached. Threads that
            # enter this branch but find the schema already created under the
            # lock fall through to the configure call below the if-block.
            #
            # Use a sidecar lockfile so flock has something to lock regardless
            # of whether the DB file itself exists yet.
            lock_path = Path(str(path) + ".init.lock")
            with open(lock_path, "w") as lockf:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
                try:
                    # Re-check under the lock — another process may have
                    # created the schema between our check and our acquire.
                    if not _has_schema_objects(conn):
                        _configure_connection(conn)
                        try:
                            conn.executescript(
                                "BEGIN IMMEDIATE;\n" + SCHEMA_SQL + "\nCOMMIT;"
                            )
                        except sqlite3.OperationalError as e:
                            msg = str(e)
                            if "already exists" not in msg:
                                raise
                            conn.rollback()
                finally:
                    fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)
        # Idempotent migrations for tables/columns added after the first
        # release. Fresh DBs already have them via SCHEMA_SQL; existing DBs
        # need them added here so the schema check below stays authoritative.
        # CREATE TABLE/INDEX IF NOT EXISTS makes this a no-op when current.
        conn.executescript(_MIGRATIONS_SQL)
        # Always (re-)apply the connection configuration. Pragmas are
        # idempotent on an already-configured connection, and the row_factory
        # assignment is a plain attribute set. This also covers threads that
        # found the schema already created under the lock above.
        _configure_connection(conn)
        _verify_schema(conn)
        return conn
    except BaseException:
        conn.close()
        raise


@contextmanager
def connection(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Yield a verified, configured connection and close it afterward."""
    conn = init_db(path)
    try:
        yield conn
    finally:
        conn.close()


def _execute_write(
    conn: sqlite3.Connection,
    sql: str,
    params: Sequence[Any] | Mapping[str, Any] = (),
) -> sqlite3.Cursor:
    """Execute one sequenced mutation in the mandatory write transaction."""
    # Mandatory write pattern, for EVERY mutating transaction:
    # BEGIN IMMEDIATE → bump change_seq → write rows → COMMIT. A deferred
    # read-then-write can fail with SQLITE_BUSY_SNAPSHOT without honoring the
    # busy timeout; IMMEDIATE reserves the write lock before any reads.
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE change_seq SET value = value + 1 WHERE id = 1")
        cursor = conn.execute(sql, params)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return cursor
