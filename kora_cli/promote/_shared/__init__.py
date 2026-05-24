"""Shared helpers for the 3 propose-then-approve promotion loops added
in KR-PROMOTE-LOOPS-COMPLETION-MEGABUCKET (router-tuning, tool-trimming,
probe-fix-envelopes). Keeps each loop's per-module footprint small
while preserving the phrasebook (#186) on-disk pending/approved/
rejected/expired layout operators already know.
"""
