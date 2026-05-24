"""Text embedding + similarity + clustering — KR-PROMOTE-PHRASEBOOK-FOUNDATION.

# Embedding strategy: lexical, deterministic, $0

The spec's default was "Haiku-based pseudo-embedding" with a per-text
cost of ~$0.001 (cold cache). For the promote-phrasebook use case
(clustering ≤200 short operator DMs/day) we ship a **lexical**
embedder instead — token-set + character-n-gram features projected
into a fixed-dimensional sparse vector and L2-normalized. This is:

  * deterministic (same input → same vector; same vector across
    re-runs and across machines — no LLM nondeterminism to hide
    behind a cache);
  * **$0/day** (no LLM call); cost discipline target
    ($0.01-0.05/day across all promotion loops) is met with budget
    to spare for the proposer's per-proposal Haiku synthesis;
  * tunable via the n-gram window;
  * stable under typo / paraphrase noise common to short operator
    DMs ("burn?" / "what's the burn?" / "burn rate?").

The Haiku-based approach is documented in the bucket's STOP-ASK §4
as the alternative if lexical clustering produced pathological
similarity distributions. We pre-empted that ASK by going lexical
from the start — the proposer module still uses Haiku where natural-
language synthesis genuinely helps (reply-template generation, one
call per proposal).

# Cache

A disk cache (keyed by ``sha256(text)``) sits in front of
:func:`embed_texts` because the cycle re-reads the past
``KORA_PROMOTE_PHRASEBOOK_OBSERVATION_WINDOW_DAYS`` of observations
on each run. With the lexical embedder cache HIT vs MISS is just an
I/O optimization (computation is microseconds either way), but the
cache is shipped so a future Haiku-backed embedder can drop in
without API surface churn.

# Public API

  * :class:`TextEmbedding` — named-tuple-ish dataclass, (text,
    embedding, cached).
  * :func:`embed_texts(texts, *, cache_dir=None)` — async; one
    pass over the input texts; returns a TextEmbedding per input
    in original order.
  * :func:`cosine_similarity(a, b)` — float in [-1, 1].
  * :func:`cluster_by_similarity(embeddings, *, threshold)` —
    greedy agglomerative clustering. Returns list of clusters
    (each cluster a list of TextEmbedding); single-element
    clusters represent unmatched observations.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


# Character n-gram window. Smaller catches typos; larger captures
# phrase shape. 3-gram is the sweet spot for short DMs.
_CHAR_NGRAM = 3

# Token-set features are tagged with a stable prefix so they don't
# collide with character-n-grams that happen to be the same string.
_TOKEN_PREFIX = "TOK:"
_CHAR_PREFIX = "CHR:"

# Lowercased + apostrophe-stripped; matches "what's" → "whats" so
# common contractions don't fragment.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TextEmbedding:
    """One text + its sparse embedding.

    ``embedding`` is a sparse-mapping representation: feature-name →
    weight. Stored as a dict for simplicity (lexical embeddings
    have at most a few hundred features per short DM; this is
    cheaper than dense lookups for our scale). Consumers see a
    consistent interface via :func:`cosine_similarity`.
    """

    text: str
    embedding: Dict[str, float]
    cached: bool


# ---------------------------------------------------------------------------
# Cache (disk-backed, sha256-keyed)
# ---------------------------------------------------------------------------


def _resolve_default_cache_dir() -> Path:
    """``${KORA_HOME}/cache/text_similarity_embeddings``. Created
    on first write; an env-unset / unreachable KORA_HOME degrades
    gracefully to a process-local tempdir so tests don't need to
    set anything."""
    try:
        from kora_constants import get_kora_home

        return get_kora_home() / "cache" / "text_similarity_embeddings"
    except Exception:
        return Path(tempfile.gettempdir()) / "kora_text_similarity_cache"


def _cache_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_get(cache_dir: Path, text: str) -> Optional[Dict[str, float]]:
    path = cache_dir / f"{_cache_key(text)}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.debug(
            "[kora.clustering] cache read failed for %s: %r",
            path,
            exc,
        )
        return None


def _cache_put(
    cache_dir: Path, text: str, embedding: Dict[str, float]
) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{_cache_key(text)}.json"
        path.write_text(json.dumps(embedding), encoding="utf-8")
    except OSError as exc:
        logger.debug(
            "[kora.clustering] cache write failed for %s: %r",
            cache_dir,
            exc,
        )


# ---------------------------------------------------------------------------
# Embedding (lexical)
# ---------------------------------------------------------------------------


def _normalize_text(text: str) -> str:
    """Lowercase + strip apostrophes so "what's" and "whats"
    collapse to the same feature set."""
    return text.lower().replace("'", "").replace("’", "")


def _token_features(text: str) -> Dict[str, float]:
    """Token-set features (binary presence, 1.0 weight each).

    Binary rather than count-weighted: short DMs rarely repeat
    tokens, and binary keeps the cosine well-behaved for very
    short inputs.
    """
    norm = _normalize_text(text)
    tokens = _TOKEN_RE.findall(norm)
    return {f"{_TOKEN_PREFIX}{t}": 1.0 for t in set(tokens)}


def _char_ngram_features(text: str) -> Dict[str, float]:
    """Character n-gram features over the normalized text. Catches
    typos + morphology that token features miss
    (``burn`` vs ``burning``)."""
    norm = _normalize_text(text)
    norm_padded = f" {norm} "
    out: Dict[str, float] = {}
    if len(norm_padded) < _CHAR_NGRAM:
        return out
    for i in range(len(norm_padded) - _CHAR_NGRAM + 1):
        gram = norm_padded[i : i + _CHAR_NGRAM]
        key = f"{_CHAR_PREFIX}{gram}"
        out[key] = out.get(key, 0.0) + 1.0
    return out


def _l2_normalize(features: Dict[str, float]) -> Dict[str, float]:
    norm_sq = sum(v * v for v in features.values())
    if norm_sq <= 0:
        return dict(features)
    norm = math.sqrt(norm_sq)
    return {k: v / norm for k, v in features.items()}


def _compute_embedding(text: str) -> Dict[str, float]:
    """Lexical embedding: union of token features + char-ngram
    features, then L2-normalized."""
    feats: Dict[str, float] = {}
    feats.update(_token_features(text))
    feats.update(_char_ngram_features(text))
    return _l2_normalize(feats)


async def embed_texts(
    texts: List[str], *, cache_dir: Optional[Path] = None
) -> List[TextEmbedding]:
    """Embed each text. Returns embeddings in the same order as the
    input list (one-to-one).

    Cache hits are recorded on the ``cached`` field — caller can
    sum cached vs uncached for cost telemetry, even though the
    lexical embedder is free either way (the field stays useful
    if the embedder is swapped for a paid one later).

    The function is ``async`` for forward-compat with the Haiku-
    backed embedder (no actual awaiting needed today; the loop
    intentionally yields to keep CPU-bound batches cooperative
    under concurrent cycles).
    """
    target_dir = cache_dir or _resolve_default_cache_dir()
    out: List[TextEmbedding] = []
    for idx, text in enumerate(texts):
        cached_emb = _cache_get(target_dir, text)
        if cached_emb is not None:
            out.append(
                TextEmbedding(text=text, embedding=cached_emb, cached=True)
            )
        else:
            emb = _compute_embedding(text)
            _cache_put(target_dir, text, emb)
            out.append(
                TextEmbedding(text=text, embedding=emb, cached=False)
            )
        # Yield every 32 texts so a 200-text batch doesn't hog the
        # event loop. Trivial overhead; matters under concurrent
        # cycles.
        if idx and idx % 32 == 0:
            await asyncio.sleep(0)
    return out


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def cosine_similarity(a: TextEmbedding, b: TextEmbedding) -> float:
    """Cosine similarity over the sparse embedding dicts.

    Both vectors are L2-normalized by ``embed_texts`` so this
    reduces to the dot product (intersection sum). Empty vectors
    short-circuit to 0.
    """
    if not a.embedding or not b.embedding:
        return 0.0
    # Iterate the smaller side for the intersection.
    if len(a.embedding) <= len(b.embedding):
        small, large = a.embedding, b.embedding
    else:
        small, large = b.embedding, a.embedding
    total = 0.0
    for key, weight in small.items():
        other = large.get(key)
        if other is not None:
            total += weight * other
    # Clamp — float noise on near-1.0 may push very slightly above.
    if total > 1.0:
        return 1.0
    if total < -1.0:
        return -1.0
    return total


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def cluster_by_similarity(
    embeddings: List[TextEmbedding],
    *,
    threshold: float = 0.85,
) -> List[List[TextEmbedding]]:
    """Greedy agglomerative clustering.

    Each new embedding joins the first existing cluster whose
    centroid-similarity ≥ ``threshold``. If none qualify, it
    starts a new cluster.

    Single-element clusters represent unmatched observations
    (caller — typically the proposer — filters by
    ``len(cluster) >= min_cluster_size``).

    "Centroid-similarity" is approximated as max(sim to any member);
    this is conservative (favors tight clusters) and cheap. For
    short DMs the centroid distance to any one member is a good
    proxy for the cluster's average — we don't need k-means
    sophistication.

    Determinism: input order is preserved; same input list ⇒ same
    output partition.
    """
    clusters: List[List[TextEmbedding]] = []
    for emb in embeddings:
        placed = False
        for cluster in clusters:
            # Max-link rather than centroid for sparse vectors —
            # cheaper, and matches the conservative-tightness goal.
            best_sim = max(
                cosine_similarity(emb, member) for member in cluster
            )
            if best_sim >= threshold:
                cluster.append(emb)
                placed = True
                break
        if not placed:
            clusters.append([emb])
    return clusters
