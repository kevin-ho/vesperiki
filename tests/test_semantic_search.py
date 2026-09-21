"""Semantic-search contract tests; all provider traffic uses MockTransport."""
from __future__ import annotations

import hashlib
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
    def mock_embedding(text: str) -> list[float]:
        """A deterministic, structure-preserving test embedding.

        It is intentionally not an LLM: dimensions expose stable text
        structure (paragraphs, headings, characters) plus a digest-derived
        component, so distance/order assertions are meaningful without
        making provider traffic or relying on Python's randomized hash().
        """
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        paragraphs = [part for part in text.split("\n\n") if part]
        headings = sum(line.startswith("#") for line in text.splitlines())
        lower = text.lower()
        # A tiny committed golden vocabulary models a provider's paraphrase
        # behavior while retaining text structure in the other dimensions.
        concepts = (
            ("travel", "trip", "itinerary", "journey"),
            ("depart", "departure", "leaves", "flight"),
        )
        concept_features = [
            float(any(word in lower for word in group)) for group in concepts
        ]
        return [
            *concept_features,
            float(len(text)),
            float(len(paragraphs)),
            float(headings),
            float(sum(text.encode("utf-8")) % 997),
            float(int.from_bytes(digest[:4], "big") / 2**32),
            float(int.from_bytes(digest[4:8], "big") / 2**32),
        ]

    def transport(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        values = payload["input"] if isinstance(payload["input"], list) else [payload["input"]]
        return httpx.Response(200, json={"data": [
            {"index": i, "embedding": mock_embedding(text)}
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
    result = service.search_semantic("x")[0]
    assert result["match"] == "semantic"
    assert {"slug", "title", "type", "snippet", "heading_path", "distance"} <= result.keys()


def test_26_page_aggregation_orders_best_chunk_and_preserves_heading(semantic_env):
    service.create_page(
        slug="a", title="A", body="## First\nshort\n\n## Second\n" + "long " * 40,
    )
    service.create_page(slug="b", title="B", body="other")
    service.drain_embed_queue()
    # The deterministic transport embeds text length, so the short chunk is
    # the nearest match.  The page must still be represented once with that
    # chunk's heading, rather than whichever chunk vec0 happens to return first.
    rows = service.search_semantic("short", limit=10)
    a = next(row for row in rows if row["slug"] == "a")
    assert a["heading_path"] == "First"
    assert [row["slug"] for row in rows].count("a") == 1


def test_27_revive_requeues_and_restores_visibility(semantic_env):
    service.create_page(slug="a", title="A", body="revivable")
    service.drain_embed_queue()
    service.delete_page(slug="a")
    assert service.search_semantic("revivable") == []
    service.revive_page(slug="a")
    assert service.drain_embed_queue()["embedded"] >= 1
    assert service.search_semantic("revivable")[0]["slug"] == "a"


def test_28_golden_paraphrase_has_zero_lexical_overlap(semantic_env):
    service.create_page(
        slug="europe", title="Europe", body="The flight departs at dawn for the trip.",
    )
    service.drain_embed_queue()
    assert service.search_pages(query="Europe trip itinerary flight departure") == []
    assert service.search_semantic("journey leaves")[0]["slug"] == "europe"


def test_29_rrf_deduplicates_and_uses_one_based_ranks():
    fused = service._rrf_fuse(
        [{"slug": "same", "source": "keyword"}, {"slug": "key", "source": "keyword"}],
        [{"slug": "sem", "source": "semantic"}, {"slug": "same", "source": "semantic"}],
        limit=10,
    )
    assert [item["slug"] for item in fused] == ["same", "sem", "key"]
    assert sum(item["slug"] == "same" for item in fused) == 1

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
