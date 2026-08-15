"""Static feature announcements advertised by ``/slimapi/health``.

The four capabilities below ship on the same release train — they are NOT
gradual-rollout feature flags. ``FEATURES`` is a static, all-true dictionary
that ``routes/health.py`` merges into the health ``features`` response, so the
L2-A/B/CD lanes never touch a shared feature source.

Keys follow the wire contract naming (camelCase) used by ocdroid's
capability-discovery: ``tokenCoalesce`` (A), ``permissionEvents`` (B),
``serverMerge`` (C), ``transformAbsorb`` (D). ``tokenStream`` and
``thresholdedSkeleton`` remain sourced from ``routes/health.py`` itself
(unchanged).
"""

FEATURES: dict[str, bool] = {
    "tokenCoalesce": True,
    "permissionEvents": True,
    "serverMerge": True,
    "transformAbsorb": True,
}
