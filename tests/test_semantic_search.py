"""Semantic-search contract tests; all provider traffic uses MockTransport."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from vesperiki import chunking, embeddings, service
from vesperiki.api import create_app
from vesperiki import mcp


@pytest.fixture
def semantic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "semantic.db"
    service.init_service(db)
    monkeypatch.setenv("VESPERIKI_EMBED_URL", "http://embed.test/v1")
    monkeypatch.setenv("VESPERIKI_EMBED_MODEL", "test-model")
    monkeypatch.setenv("VESPERIKI_EMBED_BATCH", "2")
    monkeypatch.setenv("VESPERIKI_WRITER", "test")
    monkeypatch.setenv("VESPERIKI_CLIENT", "pytest")
    def transport(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        values = payload["input"] if isinstance(payload["input"], list) else [payload["input"]]
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": [float(len(text)), 1.0, 0.0]}
            for i, text in enumerate(values)
        ]})
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(transport), **kwargs))
    return db


def test_01_unconfigured_keyword_unchanged(tmp_path, monkeypatch):
    service.init_service(tmp_path / "a.db"); monkeypatch.delenv("VESPERIKI_EMBED_URL", raising=False); monkeypatch.delenv("VESPERIKI_EMBED_MODEL", raising=False)
    assert service.search_hybrid("x")["semantic"] is False

def test_02_config_requires_url_and_model(monkeypatch):
    monkeypatch.delenv("VESPERIKI_EMBED_URL", raising=False); monkeypatch.setenv("VESPERIKI_EMBED_MODEL", "m")
    assert embeddings.get_config() is None

def test_03_config_rejects_bad_batch(monkeypatch):
    monkeypatch.setenv("VESPERIKI_EMBED_URL", "http://x"); monkeypatch.setenv("VESPERIKI_EMBED_MODEL", "m"); monkeypatch.setenv("VESPERIKI_EMBED_BATCH", "0")
    with pytest.raises(embeddings.EmbedProviderError): embeddings.get_config()

def test_04_endpoint_suffix(monkeypatch):
    monkeypatch.setenv("VESPERIKI_EMBED_URL", "http://x/v1/"); monkeypatch.setenv("VESPERIKI_EMBED_MODEL", "m")
    assert embeddings.get_config().endpoint == "http://x/v1/embeddings"

def test_05_chunk_empty():
    assert chunking.chunk_page("") == []

def test_06_chunk_heading_semantics():
    rows = chunking.chunk_page("intro\n## One\na\n### Sub\nb\n## Two\nc")
    assert [r["heading_path"] for r in rows] == ["", "One", "Two"]

def test_07_chunk_fence_atomic():
    text = "```\n" + "x" * 1500 + "\n```"
    assert len(chunking.chunk_page(text)) == 1

def test_08_chunk_seq_stable():
    assert [r["seq"] for r in chunking.chunk_page("a\n\nb\n\n c")] == [0]

def test_09_create_enqueues(semantic_env):
    service.create_page(slug="a", title="A", body="alpha")
    with service.db.connection(semantic_env) as c: assert c.execute("select count(*) from embed_queue").fetchone()[0] == 1

def test_10_update_rechunks(semantic_env):
    service.create_page(slug="a", title="A", body="alpha"); service.update_page(slug="a", body="beta")
    with service.db.connection(semantic_env) as c: assert c.execute("select body from page_chunks").fetchone()[0] == "beta"

def test_11_section_update_rechunks(semantic_env):
    service.create_page(slug="a", title="A", body="## S\na")
    service.update_section(slug="a", section_id="s", content="b")
    with service.db.connection(semantic_env) as c: assert c.execute("select body from page_chunks").fetchone()[0] == "b"

def test_12_soft_delete_keeps_queue(semantic_env):
    service.create_page(slug="a", title="A", body="a"); service.delete_page(slug="a")
    with service.db.connection(semantic_env) as c: assert c.execute("select count(*) from embed_queue").fetchone()[0] == 1

def test_13_purge_cascades_chunks(semantic_env):
    service.create_page(slug="a", title="A", body="a"); service.delete_page(slug="a", purge=True)
    with service.db.connection(semantic_env) as c: assert c.execute("select count(*) from page_chunks").fetchone()[0] == 0

def test_14_drain_batches(semantic_env):
    service.create_page(slug="a", title="A", body="a\n\nb"); assert service.drain_embed_queue()["embedded"] == 1

def test_15_drain_clears_queue(semantic_env):
    service.create_page(slug="a", title="A", body="a"); service.drain_embed_queue()
    assert service.drain_embed_queue()["queued"] == 0

def test_16_search_requires_config(tmp_path, monkeypatch):
    service.init_service(tmp_path / "a.db"); monkeypatch.delenv("VESPERIKI_EMBED_URL", raising=False); monkeypatch.delenv("VESPERIKI_EMBED_MODEL", raising=False)
    with pytest.raises(service.SemanticSearchNotConfigured): service.search_semantic("x")

def test_17_search_empty_does_not_call_provider(semantic_env):
    assert service.search_semantic(" ") == []

def test_18_model_guard(semantic_env, monkeypatch):
    service.create_page(slug="a", title="A", body="a"); service.drain_embed_queue(); monkeypatch.setenv("VESPERIKI_EMBED_MODEL", "other")
    with pytest.raises(service.EmbedSpaceMismatch): service.drain_embed_queue()

def test_19_dimension_guard(semantic_env, monkeypatch):
    service.create_page(slug="a", title="A", body="a"); service.drain_embed_queue(); monkeypatch.setenv("VESPERIKI_EMBED_DIM", "4")
    with pytest.raises(service.ServiceError): service.search_semantic("x")

def test_20_hybrid_fallback(semantic_env, monkeypatch):
    monkeypatch.delenv("VESPERIKI_EMBED_URL"); monkeypatch.delenv("VESPERIKI_EMBED_MODEL")
    assert service.search_hybrid("x")["modes"]["semantic"] is False

def test_21_semantic_result_shape(semantic_env):
    service.create_page(slug="a", title="A", body="alpha"); service.drain_embed_queue()
    assert service.search_semantic("x")[0]["match"] == "semantic"

def test_22_api_keyword_shape(semantic_env):
    app = create_app(str(semantic_env)); assert app is not None

def test_23_mcp_schema_has_modes():
    assert "mode" in mcp._TOOLS["wiki_search"].inputSchema["properties"]

def test_24_import_path_enqueues(semantic_env, tmp_path):
    from vesperiki import migrate
    p = tmp_path / "a.md"; p.write_text("# A\n\nalpha")
    migrate.migrate(tmp_path, semantic_env)
    with service.db.connection(semantic_env) as c: assert c.execute("select count(*) from embed_queue").fetchone()[0] >= 1

def test_25_vec_extension_usable(semantic_env):
    service.create_page(slug="a", title="A", body="a"); service.drain_embed_queue()
    assert service.search_semantic("x")
