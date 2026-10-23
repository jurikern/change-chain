"""H3 signal matchers (prereg-h3.md sections 2, 5, 6).

Primary matchers use the paper's printed strings (arXiv 2606.24429v1, 4.1 Finding 4),
case-insensitive (3.2). Lenient matchers (S2L, S3L) are preregistered sensitivity rows.
"""
from __future__ import annotations

import math
import os
import re

S1_TEXT = "noreply@anthropic.com"
S2_TEXT = "co-authored-by: claude <noreply@anthropic.com>"
S3_TEXT = "generated with claude code"
S2L_RE = re.compile(r"co-authored-by:\s*claude\b[^<\n]*<noreply@anthropic\.com>", re.I)
S3L_RE = re.compile(r"generated with \[?claude code\]?", re.I)
Z95 = 1.959964
LABEL_RANK = {"ESTABLISHED": 0, "INFERRED": 1, "MISSING": 2}


def s1(author_email):
    return bool(author_email) and S1_TEXT in author_email.lower()


def s2(message):
    return S2_TEXT in (message or "").lower()


def s3(message):
    return S3_TEXT in (message or "").lower()


def s2l(message):
    return bool(S2L_RE.search(message or ""))


def s3l(message):
    return bool(S3L_RE.search(message or ""))


def match_all(author_email, message):
    r = {"S1": s1(author_email), "S2": s2(message), "S3": s3(message),
         "S2L": s2l(message), "S3L": s3l(message)}
    r["U"] = r["S1"] or r["S2"] or r["S3"]
    r["UL"] = r["S1"] or r["S2L"] or r["S3L"]
    return r


def miss_reason(message, parents):
    """First reason that applies, in prereg order M1..M5."""
    if s2l(message) and not s2(message):
        return "M1"
    if s3l(message) and not s3(message):
        return "M2"
    low = (message or "").lower()
    has_text = S1_TEXT in low or "claude code" in low
    if not has_text:
        return "M3" if len(parents) >= 2 else "M4"
    return "M5"


def wilson(k, n, z=Z95):
    if n == 0:
        return (None, None)
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))


def best_label(labels):
    return min(labels, key=lambda l: LABEL_RANK[l])


def public_repo_names(paths):
    """Basename for datapce*/change-chain* repos; other-N (sorted by basename) for the rest."""
    out, others = {}, []
    for p in paths:
        b = os.path.basename(p.rstrip("/"))
        if b.startswith("datapce") or b.startswith("change-chain"):
            out[p] = b
        else:
            others.append((b, p))
    for i, (_, p) in enumerate(sorted(others), 1):
        out[p] = f"other-{i}"
    return out
