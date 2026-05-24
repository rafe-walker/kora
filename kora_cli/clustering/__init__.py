"""Shared text-clustering utilities for promotion loops.

First consumer: KR-PROMOTE-PHRASEBOOK-FOUNDATION. Future consumers:
snapshot-expand, router-trigger, tool-trimming, probe-fix-envelope
promotion loops (all the loops Joshua locked in
``feedback-promotion-loops-self-improving-subsystems``).

Public API: :func:`text_similarity.embed_texts`,
:func:`text_similarity.cosine_similarity`,
:func:`text_similarity.cluster_by_similarity`.
"""
