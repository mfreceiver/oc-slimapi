"""Parse ``X-Slimapi-Capabilities`` opt-in header (contract §7 Opt-A / v0.3.1).

Grammar (frozen consensus I-R4-CAP-GRAMMAR + I-R5-CAP-DUPLICATES):

1. Comma-split tokens; trim ASCII whitespace on both sides of each token.
2. Each token must contain exactly one ``=``. Token without ``=`` (or malformed)
   → ignored as ``malformed_token`` (count it). Do NOT fail the request.
3. Name (left of ``=``) is case-insensitive; value (right of ``=``) is trimmed
   then literal-compared (case-sensitive).
4. Unknown name → ignored (NOT malformed, NOT counted as malformed; it's simply
   an unknown capability). Only structurally malformed tokens (no ``=``) count as
   malformed.
5. ``mid-partial-envelope=1`` → opt-in True; ``=0`` or absent → opt-in False.
6. **Duplicate conflict (fail-closed)**: if ``mid-partial-envelope`` appears with
   **conflicting** values (e.g. ``=1`` and ``=0`` in same header) → opt-in forced
   False AND ``duplicate_conflict=True``. Repeating the SAME value (e.g.
   ``=1, =1``) is idempotent, NOT a conflict.
"""

from __future__ import annotations

from typing import Final

CAPABILITY_OPT_A: Final[str] = "mid-partial-envelope"


class CapabilityParse:
    """Result of parsing a ``X-Slimapi-Capabilities`` header value."""

    __slots__ = ("opt_in", "duplicate_conflict", "malformed_tokens", "unknown_tokens")

    def __init__(
        self,
        opt_in: bool,
        duplicate_conflict: bool,
        malformed_tokens: int,
        unknown_tokens: int,
    ) -> None:
        self.opt_in = opt_in
        self.duplicate_conflict = duplicate_conflict
        self.malformed_tokens = malformed_tokens
        self.unknown_tokens = unknown_tokens


def parse_capabilities(header_value: str | None) -> CapabilityParse:
    """Parse the ``X-Slimapi-Capabilities`` header value.

    Examples:

        >>> parse_capabilities(None)
        CapabilityParse(opt_in=False, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=0)

        >>> parse_capabilities("")
        CapabilityParse(opt_in=False, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=0)

        >>> parse_capabilities("mid-partial-envelope=1")
        CapabilityParse(opt_in=True, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=0)

        >>> parse_capabilities("mid-partial-envelope=1, unknown_cap=foo")
        CapabilityParse(opt_in=True, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=1)

        >>> parse_capabilities("mid-partial-envelope=1, mid-partial-envelope=0")
        CapabilityParse(opt_in=False, duplicate_conflict=True, malformed_tokens=0, unknown_tokens=0)

        >>> parse_capabilities("mid-partial-envelope=1, malformed_no_eq")
        CapabilityParse(opt_in=True, duplicate_conflict=False, malformed_tokens=1, unknown_tokens=0)
    """
    if not header_value:
        return CapabilityParse(False, False, 0, 0)

    opt_in = False
    duplicate_conflict = False
    malformed_tokens = 0
    unknown_tokens = 0
    seen_mid_partial = None  # None | True | False

    for raw_token in header_value.split(","):
        token = raw_token.strip()
        if not token:
            # Empty tokens after trimming are skipped (e.g. trailing comma)
            continue

        eq_idx = token.find("=")
        if eq_idx == -1 or eq_idx == 0:
            # No '=' sign, or name is empty (e.g. "=value" is malformed)
            malformed_tokens += 1
            continue

        name = token[:eq_idx].strip().lower()
        value = token[eq_idx + 1 :].strip()

        # Name must not be empty (already caught by eq_idx == 0)
        if not name:
            malformed_tokens += 1
            continue

        if name == CAPABILITY_OPT_A.lower():
            if value not in ("0", "1"):
                # Malformed value but still a well-formed token with '='; treat as unknown?
                # Spec says value is literal-compared, and only =0/=1 are recognized.
                # If value is something else, is it "unknown" or "malformed"?
                # The spec says: "value (right of =) is trimmed then literal-compared (case-sensitive)"
                # and "Unknown name → ignored (NOT malformed...)". But value "abc" for
                # a known name is still a well-formed token; it just doesn't match =0/=1.
                # I'll treat it as unknown (not malformed) per the spirit of "literal-compared".
                # Actually, re-reading: "Unknown name → ignored" - but the *name* is known,
                # the *value* is unexpected. However the spec only mentions "Unknown name",
                # not "unknown value". For safety, treat unrecognized value as unknown.
                unknown_tokens += 1
                continue

            val_bool = value == "1"
            if seen_mid_partial is None:
                seen_mid_partial = val_bool
                if val_bool:
                    opt_in = True
            elif seen_mid_partial != val_bool:
                # Conflicting values
                opt_in = False
                duplicate_conflict = True
            # If same value repeated, idempotent - do nothing
        else:
            unknown_tokens += 1

    return CapabilityParse(opt_in, duplicate_conflict, malformed_tokens, unknown_tokens)
