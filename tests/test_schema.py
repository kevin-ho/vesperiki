import sqlite3

import pytest

from vesperiki.db import init_db


def insert_page(conn: sqlite3.Connection, title: str = "Test Page") -> int:
    cursor = conn.execute(
        """
        INSERT INTO pages (slug, title, title_norm, body)
        VALUES (?, ?, ?, ?)
        """,
        (title.lower().replace(" ", "-"), title, title.lower().replace(" ", ""), "Body"),
    )
    return cursor.lastrowid


def fts_page_ids(conn: sqlite3.Connection, query: str) -> list[int]:
    return [
        row["rowid"]
        for row in conn.execute(
            "SELECT rowid FROM pages_fts WHERE pages_fts MATCH ?", (query,)
        )
    ]


def test_migration_on_live_old_schema_materializes_and_queues_pages(tmp_path):
    """An existing page corpus gets semantic bookkeeping on first reopen."""
    path = tmp_path / "old.db"
    # Start from the current schema, then remove only the Phase 7 objects;
    # this models a real pre-semantic database while retaining all prior
    # triggers, indexes, and lookup tables required by schema verification.
    with init_db(path) as conn:
        conn.execute("INSERT INTO pages(slug, title, title_norm, body) VALUES ('legacy', 'Legacy', 'legacy', 'old content')")
        conn.commit()
    with sqlite3.connect(path) as conn:
        conn.executescript("DROP TABLE embed_queue; DROP TABLE embed_config; DROP TABLE page_chunks;")
        conn.commit()

    with init_db(path) as conn:
        assert conn.execute("SELECT count(*) FROM page_chunks").fetchone()[0] == 1
        assert conn.execute("SELECT page_id FROM embed_queue").fetchone()[0] == 1
        assert conn.execute("SELECT body FROM page_chunks").fetchone()[0] == "old content"


def test_init_creates_file(tmp_path):
    path = tmp_path / "t.db"

    conn = init_db(path)
    try:
        assert path.is_file()
        assert isinstance(conn, sqlite3.Connection)
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_foreign_keys_are_on(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_wal_mode(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_change_seq_initialized(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        row = conn.execute("SELECT id, value FROM change_seq").fetchone()
        assert dict(row) == {"id": 1, "value": 0}


def test_lookup_tables_seeded(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        for table in ("page_types", "source_types", "link_rels"):
            count = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            assert count >= 3


def test_pages_cascade_on_delete(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        page_id = insert_page(conn)
        conn.execute(
            """
            INSERT INTO revisions
                (page_id, body, changed_by, client, change_type)
            VALUES (?, 'Body', 'testwriter', 'pytest', 'create')
            """,
            (page_id,),
        )

        conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))

        assert conn.execute(
            "SELECT 1 FROM revisions WHERE page_id = ?", (page_id,)
        ).fetchone() is None


def test_foreign_keys_reject_orphan_revision(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO revisions
                    (page_id, body, changed_by, client, change_type)
                VALUES (99999, 'Body', 'testwriter', 'pytest', 'create')
                """
            )


def test_fts5_insert_triggers_populate(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        page_id = insert_page(conn, "Hello World")

        assert fts_page_ids(conn, "hello") == [page_id]


def test_fts5_update_triggers_refresh(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        page_id = insert_page(conn, "Alpha")

        conn.execute(
            "UPDATE pages SET title = 'Beta', title_norm = 'beta' WHERE id = ?",
            (page_id,),
        )

        assert fts_page_ids(conn, "beta") == [page_id]
        assert fts_page_ids(conn, "alpha") == []


def test_fts5_delete_triggers_remove(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        page_id = insert_page(conn, "Delete Me")

        conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))

        assert fts_page_ids(conn, "delete") == []


def test_tag_rename_fts_refresh(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        page_id = insert_page(conn, "Tagged Page")
        tag_ids = []
        for name in ("first", "foo", "third"):
            tag_ids.append(conn.execute(
                "INSERT INTO tags (name) VALUES (?)", (name,)
            ).lastrowid)
        conn.executemany(
            "INSERT INTO page_tags (page_id, tag_id) VALUES (?, ?)",
            ((page_id, tag_id) for tag_id in tag_ids),
        )

        conn.execute("UPDATE tags SET name = 'bar' WHERE name = 'foo'")

        assert fts_page_ids(conn, "bar") == [page_id]
        assert fts_page_ids(conn, "foo") == []


def test_media_cascade(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        page_id = insert_page(conn)
        conn.execute(
            """
            INSERT INTO media (page_id, filename, sha256, data)
            VALUES (?, 'image.png', 'abc123', ?)
            """,
            (page_id, b"image bytes"),
        )

        conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))

        assert conn.execute(
            "SELECT 1 FROM media WHERE page_id = ?", (page_id,)
        ).fetchone() is None


# ---------------------------------------------------------------------------
# Correction queue
# ---------------------------------------------------------------------------

def test_corrections_table_exists(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = 'corrections'"
        ).fetchone()
        assert row is not None


def test_corrections_status_index_exists(tmp_path):
    with init_db(tmp_path / "t.db") as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'index' AND name = 'idx_corrections_status'"
        ).fetchone()
        assert row is not None


def test_corrections_round_trip(tmp_path):
    """Insert a row, read it back, verify defaults."""
    with init_db(tmp_path / "t.db") as conn:
        insert_page(conn, "Target")
        conn.execute(
            "INSERT INTO corrections (page_slug, selected_text, note) "
            "VALUES (?, ?, ?)",
            ("target", "teh", "should be 'the'"),
        )
        row = conn.execute(
            "SELECT page_slug, selected_text, note, status, "
            "created_at, resolved_at, resolved_by "
            "FROM corrections"
        ).fetchone()
        assert row["page_slug"] == "target"
        assert row["selected_text"] == "teh"
        assert row["note"] == "should be 'the'"
        assert row["status"] == "pending"
        assert row["created_at"] is not None
        assert row["resolved_at"] is None
        assert row["resolved_by"] is None


def test_corrections_status_check_rejects_invalid(tmp_path):
    """The CHECK constraint blocks bogus status values at the DB layer."""
    with init_db(tmp_path / "t.db") as conn:
        insert_page(conn, "X")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO corrections "
                "(page_slug, selected_text, status) VALUES (?, ?, ?)",
                ("x", "y", "in-progress"),
            )


def test_corrections_cascade_on_page_delete(tmp_path):
    """A purged page's pending flags vanish with it (no dangling rows)."""
    with init_db(tmp_path / "t.db") as conn:
        page_id = insert_page(conn, "Doomed")
        conn.execute(
            "INSERT INTO corrections (page_slug, selected_text) VALUES (?, ?)",
            ("doomed", "typo"),
        )
        assert conn.execute(
            "SELECT count(*) FROM corrections"
        ).fetchone()[0] == 1

        conn.execute("DELETE FROM pages WHERE id = ?", (page_id,))

        assert conn.execute(
            "SELECT count(*) FROM corrections"
        ).fetchone()[0] == 0


def test_corrections_fk_rejects_unknown_page(tmp_path):
    """FK to pages(slug) blocks inserting against a non-existent page."""
    with init_db(tmp_path / "t.db") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO corrections (page_slug, selected_text) "
                "VALUES (?, ?)",
                ("ghost", "missing"),
            )
