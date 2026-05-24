"""Tests for kora_cli.clustering.text_similarity."""

from __future__ import annotations

import pytest

from kora_cli.clustering.text_similarity import (
    TextEmbedding,
    cluster_by_similarity,
    cosine_similarity,
    embed_texts,
)


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    """Per-test cache dir under tmp_path so cache assertions don't
    bleed across tests."""
    monkeypatch.setenv("KORA_HOME", str(tmp_path))
    monkeypatch.setattr(
        "kora_constants.get_kora_home", lambda: tmp_path, raising=False
    )
    return tmp_path


@pytest.mark.asyncio
async def test_embed_texts_returns_one_embedding_per_input():
    texts = ["what's the burn?", "are you paused?", "any alerts?"]
    out = await embed_texts(texts)
    assert len(out) == 3
    for emb, text in zip(out, texts):
        assert emb.text == text
        assert isinstance(emb.embedding, dict)
        assert emb.embedding  # non-empty
        assert emb.cached is False  # first call → cold cache


@pytest.mark.asyncio
async def test_embed_texts_cache_hit_on_second_call():
    out1 = await embed_texts(["burn?"])
    out2 = await embed_texts(["burn?"])
    assert out1[0].cached is False
    assert out2[0].cached is True
    # Embeddings byte-identical (the cached dict is what's stored).
    assert out1[0].embedding == out2[0].embedding


@pytest.mark.asyncio
async def test_embed_texts_yields_features_for_short_text():
    out = await embed_texts(["burn?"])
    feats = out[0].embedding
    # Token feature for "burn" present.
    assert "TOK:burn" in feats
    # Char-ngram features present (3-grams over " burn? ").
    assert any(k.startswith("CHR:") for k in feats)


@pytest.mark.asyncio
async def test_embed_texts_normalizes_apostrophes():
    out1 = await embed_texts(["what's"])
    out2 = await embed_texts(["whats"])
    assert "TOK:whats" in out1[0].embedding
    assert "TOK:whats" in out2[0].embedding


@pytest.mark.asyncio
async def test_cosine_identical_texts_is_one():
    out = await embed_texts(["burn rate today?"])
    same_emb = out[0]
    assert cosine_similarity(same_emb, same_emb) == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_cosine_unrelated_texts_below_threshold():
    out = await embed_texts(
        ["what's the burn?", "elephants migrate in winter"]
    )
    assert cosine_similarity(out[0], out[1]) < 0.3


@pytest.mark.asyncio
async def test_cosine_paraphrase_above_threshold():
    out = await embed_texts(
        [
            "what's the burn rate today?",
            "what's the burn today?",
        ]
    )
    assert cosine_similarity(out[0], out[1]) >= 0.5


@pytest.mark.asyncio
async def test_cosine_with_empty_embedding_is_zero():
    real = (await embed_texts(["x"]))[0]
    empty = TextEmbedding(text="", embedding={}, cached=False)
    assert cosine_similarity(real, empty) == 0.0


@pytest.mark.asyncio
async def test_cluster_groups_similar_texts():
    texts = [
        "what's the burn rate today?",
        "burn rate?",
        "burn rate now?",
        "completely unrelated about elephants",
    ]
    out = await embed_texts(texts)
    clusters = cluster_by_similarity(out, threshold=0.4)
    # 3 burn variants should cluster; the elephants line stands
    # alone.
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 3]


@pytest.mark.asyncio
async def test_cluster_threshold_too_strict_yields_singletons():
    texts = ["burn?", "alerts?", "state?"]
    out = await embed_texts(texts)
    clusters = cluster_by_similarity(out, threshold=0.99)
    assert all(len(c) == 1 for c in clusters)


@pytest.mark.asyncio
async def test_cluster_preserves_input_order():
    """Determinism: same input list → same partition. Verified
    indirectly: clusters' first members appear in input order."""
    texts = [f"burn variant {i}" for i in range(8)]
    out = await embed_texts(texts)
    clusters = cluster_by_similarity(out, threshold=0.6)
    # Whichever cluster has the first text, its first element is
    # text 0.
    first_cluster = clusters[0]
    assert first_cluster[0].text == texts[0]
