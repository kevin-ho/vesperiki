"""Embedding queue administration CLI."""
from __future__ import annotations

import argparse
import os
import sys

from . import db, embeddings, service


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vesperiki reembed")
    parser.add_argument("--db", default=os.environ.get("VESPERIKI_DB_PATH", "./vesperiki.db"))
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    db.init_db(args.db).close()
    service.init_service(args.db)
    config = embeddings.get_config()
    if args.status:
        if config is None:
            print("not configured: set VESPERIKI_EMBED_URL and VESPERIKI_EMBED_MODEL")
            return 0
        with db.connection(args.db) as conn:
            total = conn.execute("SELECT count(*) FROM page_chunks").fetchone()[0]
            embedded = conn.execute("SELECT count(*) FROM page_chunks WHERE embedded_at IS NOT NULL AND stale=0").fetchone()[0]
            queued = conn.execute("SELECT count(*) FROM embed_queue").fetchone()[0]
        print(f"configured host={config.host} model={config.model} {embedded}/{total} chunks embedded, {queued} queued")
        return 0
    if config is None:
        print("not configured: set VESPERIKI_EMBED_URL and VESPERIKI_EMBED_MODEL", file=sys.stderr)
        return 1
    if args.full:
        with db.connection(args.db) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP TABLE IF EXISTS chunk_vec")
            conn.execute("UPDATE page_chunks SET embedded_at=NULL, embedding_model=NULL, embedding_dim=NULL, stale=1")
            conn.execute("INSERT OR IGNORE INTO embed_queue(page_id) SELECT DISTINCT page_id FROM page_chunks")
            conn.execute("DELETE FROM embed_config")
            conn.commit()
    try:
        print(service.drain_embed_queue())
    except service.EmbedSpaceMismatch as exc:
        print(f"{exc}; use --full", file=sys.stderr)
        return 1
    except embeddings.EmbedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
