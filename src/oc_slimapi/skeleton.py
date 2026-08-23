"""Pure v2-contract message/session projection functions."""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import orjson

PLACEHOLDER_TEXT = "[内容已折叠，点开查看]"
PART_IDS = {"id", "type", "messageID", "sessionID"}

# ---------------------------------------------------------------------------
# Message content fingerprint (traffic plan Batch 4 / B3).
#
# Design authority: docs/specs/design-message-watermark.md (frozen). The
# fingerprint is a pure function of the message's FINAL external
# representation (projected info + final parts). ``FINGERPRINT_VERSION`` is
# an INDEPENDENT constant — it bumps ONLY when the normalisation rules below
# change (fields added/removed from the input, serialisation change), never
# with package releases or ``REP_VERSION``.
#
# Normalisation (frozen, design doc §4.3):
#   1. exclude ``contentFingerprint`` itself (no self-reference);
#   2. orjson ``OPT_SORT_KEYS``; parts stay in upstream order (upstream
#      order IS the semantic order — never re-sorted);
#   3. numbers/strings participate verbatim (no numeric normalisation);
#   4. full sha256 hex, ``"vN:"`` prefix, never truncated.
#
# Semantics (frozen, design doc §4.4): same normalised input → same
# fingerprint (determinism, survives restarts); different fingerprint ⟹
# different input; same fingerprint indicates same content ONLY under the
# engineering assumption that SHA-256 collisions are negligible (2^-256).
# NO monotonicity/timing semantics. Fingerprints are NOT comparable across
# representation modes (default skeleton vs mode=merged) — the ``vN`` prefix
# deliberately does NOT encode the mode; the contract text binds the
# comparison namespace.
# ---------------------------------------------------------------------------
FINGERPRINT_VERSION = 1
FINGERPRINT_FIELD = "contentFingerprint"


def compute_message_fingerprint(message: dict[str, Any]) -> str:
    """Fingerprint a message's final representation.

    ``message`` is a projected message (``{info, parts, ...}``) — possibly
    already carrying a stale ``contentFingerprint`` (excluded from the
    hash input, so recomputation is idempotent-safe).
    """
    canonical = {
        key: value for key, value in message.items()
        if key != FINGERPRINT_FIELD
    }
    digest = hashlib.sha256(
        orjson.dumps(canonical, option=orjson.OPT_SORT_KEYS)
    ).hexdigest()
    return f"v{FINGERPRINT_VERSION}:{digest}"


def recompute_fingerprint(message: dict[str, Any]) -> None:
    """Overwrite the message's fingerprint in place (merged splice site)."""
    message[FINGERPRINT_FIELD] = compute_message_fingerprint(message)
TOOL_KEYS = PART_IDS | {"tool", "callID"}
TOOL_INPUT_KEYS = {
    "path", "filePath", "file_path", "command", "agent", "description",
    "subagent_type", "todos",
}
TOOL_METADATA_KEYS = {"sessionId", "sessionID", "description", "agent", "diffStats", "files"}
# §4a/§4b: per-part ``files`` projection cap — a toolcard renders a compact
# list (≤10); the full list stays reachable via ``filesTotal`` (source count)
# + the ``part_state_metadata_full`` expand ref (P2-N2) or ``/full``.
FILES_PROJECTION_CAP = 10
# §2: character cap for the synthesized compress title (readable card size,
# NOT a byte cap — multibyte titles clip at the same character count).
COMPRESS_TITLE_CLIP_CHARS = 160
FILE_URL_LIMIT = 8 * 1024
COMPACTION_PART_LIMIT = 64 * 1024

# Expand design v5 §4.1: inline caps, measured in UTF-8 encoded bytes. A
# reasoning part whose text exceeds the cap is projected as ``text: null`` +
# an ``expandRefs`` entry — never partially truncated.
# [3.2.0] TextPart.text is no longer capped (always inlined verbatim);
# TEXT_INLINE_MAX_BYTES stays as the historical 3.1.x contract value — kept
# for expand-endpoint documentation and as the test baseline for building
# over-cap samples.
TEXT_INLINE_MAX_BYTES = 2048
REASONING_INLINE_MAX_BYTES = 2048


# ---------------------------------------------------------------------------
# Per-call skeleton limits (P1-3 config de-double-tracking).
#
# ``skeleton.py`` no longer reads the global ``settings`` singleton. The two
# inline caps are passed explicitly as an immutable ``SkeletonLimits`` value,
# threaded through every projection function. Defaults (4 KiB / 16 KiB) live
# here as module constants — they match the prior ``Settings`` defaults so
# direct pure-function tests are unchanged; production callers (the messages
# route) construct a fresh ``SkeletonLimits`` from
# ``request.app.state.config`` per request, so two apps with different Settings
# see different projections (T8-C1 / T8-C6).
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SkeletonLimits:
    """Per-call inline caps + projection switches for skeleton thresholding.

    ``field_bytes`` caps a single inlined ``state.output`` / ``state.error``
    field (per-field cap). ``message_bytes`` caps the cumulative inlined bytes
    across all parts of one message in part order (per-message budget). Both
    are JSON-wire bytes (measured by :func:`_field_byte_size`).

    ``fingerprint`` (Batch 4 / B3) carries the per-call
    ``message_fingerprint_enabled`` switch: the route builds it from config
    alongside the caps and the projection reads it here — the pack-worker
    signatures stay unchanged (existing monkeypatch stand-ins keep working).
    """

    field_bytes: int
    message_bytes: int
    fingerprint: bool = False


# Defaults for direct pure-function tests; production paths construct from
# request.app.state.config (see routes/messages.py).
DEFAULT_SKELETON_LIMITS = SkeletonLimits(field_bytes=4 * 1024, message_bytes=16 * 1024)


# ---------------------------------------------------------------------------
# Thresholded skeleton (additive; wire version UNCHANGED — stays 1).
#
# Small ``state.output`` (and ``state.error``) on tool/patch parts is inlined
# into the thin skeleton so ocdroid slim users actually see short tool results
# (diffs, file reads, command output, errors) without a ``/full`` round-trip.
# Large fields are still omitted + ``hasFull`` so the expand path fetches them
# WHOLE — a field is either fully inlined or fully expandable, never half-
# truncated. ``structured``/``result``/``raw``/``attachments`` stay always-omit
# (giant nested JSON has no inline value to the user).
#
# Two caps (env-overridable tuning knobs, centralised in ``Settings`` — they do
# NOT touch the wire contract, so ``X-Slimapi-Version`` is NOT bumped):
#   * per-field:   inline iff JSON-byte size
#                  <= Settings.skeleton_inline_output_max_bytes
#   * per-message: cumulative inlined bytes across all parts in one message
#                  <= Settings.skeleton_inline_output_max_message_bytes (once
#                  the cap is spent, later fields in part order fall back to omit).
# The outer response still honours ``Settings.max_response_bytes`` regardless —
# thresholding never bypasses the global body cap. Defaults: 4 KiB / 16 KiB.
# ---------------------------------------------------------------------------
# Fields eligible for inlining (small tool results / errors). Everything in
# SKELETON_ALWAYS_OMIT_FIELDS stays omitted unconditionally.
SKELETON_INLINE_FIELDS = ("output", "error")
SKELETON_ALWAYS_OMIT_FIELDS = ("structured", "result", "raw", "attachments")


def _pick(value: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    return {key: deepcopy(value[key]) for key in keys if key in value}


def _mark(part: dict[str, Any], omitted: list[str]) -> dict[str, Any]:
    if omitted:
        part["hasFull"] = True
        part["omitted"] = sorted(set(omitted))
    return part


def _utf8_bytes_exceeds(text: Any, limit: int) -> bool:
    """§4.1: threshold check measured in UTF-8 encoded bytes — multibyte
    strings count by wire bytes, not character count. Non-str values never
    exceed the limit."""
    return isinstance(text, str) and len(text.encode("utf-8")) > limit


def _expand_ref(
    category: str, message_id: str, part_id: str | None, sid: str,
    # wire_view default 3 = pure-function historical FREEZE (golden: tests/
    # test_expand_href_v4.py::test_projection_default_view_keeps_frozen_v3_bytes);
    # production always passes 4 — routes/messages/_list.py::_expand_wire_view
    # returns 4 unconditionally (D5). Do not "fix" the default to 4.
    wire_view: int = 3,
) -> dict[str, Any]:
    """Build one §5 expandRef entry (frozen schema).

    Message-level href: ``/slimapi/messages/{sid}/expand/{category}/{mid}?v={view}``
    Part-level href:    ``/slimapi/messages/{sid}/expand/{category}/{mid}/{partID}?v={view}``
    ``directory`` is appended by the client (§5.2).

    v4 §14: ``?v=`` carries the request's wire view — v3 requests keep the
    frozen v3 bytes (``?v=3``), v4 requests emit ``?v=4``. ``v`` stays the
    FIRST (and, from the sidecar, ONLY) query key — the client appends
    ``directory`` second (§14 frozen key order: ``v`` then ``directory``).
    """
    base = f"/slimapi/messages/{sid}/expand/{category}/{message_id}"
    href = f"{base}/{part_id}?v={wire_view}" if part_id is not None else f"{base}?v={wire_view}"
    ref = {"category": category, "messageID": message_id, "href": href}
    if part_id is not None:
        ref["partID"] = part_id
    return ref


def _emit_expand_refs(
    part: dict[str, Any], refs: list[tuple[str, str]], sid: str | None,
    # wire_view default 3 = pure-function historical FREEZE (golden: tests/
    # test_expand_href_v4.py::test_projection_default_view_keeps_frozen_v3_bytes);
    # production always passes 4 — routes/messages/_list.py::_expand_wire_view
    # returns 4 unconditionally (D5). Do not "fix" the default to 4.
    wire_view: int = 3,
) -> dict[str, Any]:
    """Attach deduped, deterministic ``expandRefs`` to a part (§5.2).

    ``refs`` entries are ``(category, partID)`` pairs; each (category, partID)
    appears at most once (dedup) and output is sorted by (category, partID).
    Without ``sid`` no href can be built → refs are dropped (the reductions
    themselves still apply). Parts without a ``messageID`` — or refs whose
    partID is falsy (missing/empty, M3) — get no part-level refs.

    v4 §14: ``wire_view`` selects the ``?v=`` value in every href (dedup /
    sort semantics are view-invariant — inherited unchanged from v3 §4a).
    """
    if not sid or not refs:
        return part
    message_id = part.get("messageID")
    if not message_id:
        return part
    part["expandRefs"] = [
        _expand_ref(category, message_id, part_id, sid, wire_view)
        for category, part_id in sorted({(c, p) for c, p in refs if p})
    ]
    return part


def _field_byte_size(value: Any) -> int:
    """Wire byte size of a state field value.

    Uses the SAME serialiser as the response body (:func:`orjson.dumps`) so the
    measured size is exactly what lands on the wire — including nested
    structure and JSON quoting/escaping for strings (an ASCII string of length
    ``N`` serialises to ``N+2`` bytes; a 4-byte emoji to 6). Multibyte UTF-8 is
    therefore counted consistently with wire cost. Non-JSON-serialisable values
    fall back to ``str(value)`` UTF-8 length. This is the SINGLE byte-accounting
    primitive for skeleton thresholding — do not re-implement at call sites.
    """
    try:
        return len(orjson.dumps(value))
    except TypeError:
        return len(str(value).encode("utf-8"))


def _clip(value: Any, limit: int) -> str | None:
    """§2: clip a candidate title string for skeleton projection.

    Frozen semantics: ``str`` only (any other type → ``None`` — never
    ``str()``-coerced); leading/trailing whitespace stripped first; a
    whitespace-only result counts as MISSING (→ ``None``); truncation is by
    CHARACTERS to ``limit`` with NO ellipsis appended. Coercion here would
    fabricate a title the upstream never sent — so no coercion, ever.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def _valid_count(value: Any) -> bool:
    """§4a/§4b: the SINGLE strict numeric validator for per-file count
    fields (``additions`` / ``deletions`` / ``files``).

    int-only (``bool`` is an ``int`` subclass — rejected explicitly), zero
    allowed, negatives rejected; floats — including the degenerate ``1.0``,
    ``inf``, ``nan`` — are rejected outright (P1-5: coercing 1.5 → 1 would
    silently publish a number upstream never sent).
    """
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _compute_diffstats(
    filediff: dict[str, Any] | list[dict[str, Any]] | Any,
) -> dict[str, int] | None:
    """Compute compact ``diffStats = {additions, deletions, files}`` from an
    upstream ``state.metadata.filediff`` value (a single ``Snapshot.FileDiff``
    object or possibly a list thereof for multi-file tools).

    Returns ``None`` when the input is not a recognised shape (no data to
    derive statistics from), so callers can safely skip injection.

    Exception-safe rewrite (P1-N3): every count value passes through the
    SINGLE strict validator :func:`_valid_count` — invalid values (strings,
    floats, bools, negatives, ``inf``/``nan``) contribute 0 and non-dict
    list entries are skipped, so this NEVER raises regardless of upstream
    shape. A non-empty input whose every entry is malformed (no entry
    carries ≥1 valid ``additions``/``deletions``) → ``None``: garbage must
    not masquerade as ``{0, 0, N}`` — the caller falls through to the
    lower-priority derivations (② ``files`` / ③ edit diff parse, §4b).
    """
    # ── digest 对账标注 ────────────────────────────────────────────────
    # digest 对账（tool 完成→message.updated 映射）：后续 SSE 实测验证项，
    # 本轮不实现。参见 **ocdroid 仓** docs/specs/chat-toolcard-investigation.md
    # §B.8（F-353：跨仓文件，本仓无此路径）。
    # ────────────────────────────────────────────────────────────────────
    entries: list[Any]
    if isinstance(filediff, list):
        if not filediff:
            return None
        entries = filediff
    elif isinstance(filediff, dict):
        entries = [filediff]
    else:
        return None
    total_additions = 0
    total_deletions = 0
    valid_entries = 0
    has_valid_count = False
    for item in entries:
        if not isinstance(item, dict):
            continue
        valid_entries += 1
        additions = item.get("additions")
        deletions = item.get("deletions")
        if _valid_count(additions):
            has_valid_count = True
            total_additions += additions
        if _valid_count(deletions):
            has_valid_count = True
            total_deletions += deletions
    if valid_entries == 0 or not has_valid_count:
        # No dict entries at all, or every entry malformed → nothing to
        # derive (fall through to ②/③ at the call site).
        return None
    return {
        "additions": total_additions,
        "deletions": total_deletions,
        "files": valid_entries,
    }


def _compute_diffstats_from_files(files: list[dict[str, Any]] | Any) -> dict[str, int] | None:
    """Compute ``diffStats = {additions, deletions, files}`` from a
    (projected/mapped) ``files[]`` array of dict entries — as used by patch
    parts (§4a normalized entries) and multi-file apply_patch metadata
    (§4b compact entries).

    Anti-fabrication guard (R1-M1 + P1-5/N4): injects only when at least ONE
    entry carries ≥1 ``_valid_count`` ``additions``/``deletions``. Count-less
    entries — a pure ``string[]`` patch (v1.18.16 shape), its §4a-normalized
    ``{path}`` derivations, or dict entries without stats — must NOT yield a
    fabricated ``{additions: 0, deletions: 0, files: N}``. Sums count valid
    values only (invalid → 0); ``files`` = number of mapped entries (not the
    source array length). Never raises.
    """
    if not isinstance(files, list) or not files:
        return None
    total_additions = 0
    total_deletions = 0
    has_valid_count = False
    for item in files:
        if not isinstance(item, dict):
            continue
        additions = item.get("additions")
        deletions = item.get("deletions")
        if _valid_count(additions):
            has_valid_count = True
            total_additions += additions
        if _valid_count(deletions):
            has_valid_count = True
            total_deletions += deletions
    if not has_valid_count:
        return None
    return {
        "additions": total_additions,
        "deletions": total_deletions,
        "files": len(files),
    }


# ---------------------------------------------------------------------------
# §4d B2: unified-diff text parser (opencode ``edit`` tool ``metadata.diff``).
#
# Production format (upstream ``tool/edit.ts`` → jsdiff
# ``createTwoFilesPatch(resource, resource, old, new)``):
#
#     Index: /abs/path/file.ts
#     ===================================================================
#     --- /abs/path/file.ts	2026-08-21T10:00:00.000Z
#     +++ /abs/path/file.ts	2026-08-21T10:00:00.000Z
#     @@ -1,4 +1,4 @@
#      context
#     -removed
#     +added
#
# (bare paths, optional ``\t`` timestamp suffix on the ---/+++ lines; git
# style ``a/``/``b/`` prefixes and ``/dev/null`` sides are supported
# defensively). Single-pass O(n), never raises, never fabricates: a text
# without any VALID file section (paired ---/+++ headers — an isolated
# ``Index:`` line does NOT validate a section, P1-N5) → ``None``.
# ---------------------------------------------------------------------------
_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _hunk_line_budget(line: str) -> int | None:
    """Remaining hunk-body budget from an ``@@ -l,s +l,s @@`` header.

    A hunk body line consumes: context 2 (one old + one new), ``+`` 1 (new),
    ``-`` 1 (old), ``\\`` marker 0. An unparseable header → ``None``
    (unbounded — only the ② boundary lines can then close the hunk).
    """
    match = _HUNK_HEADER_RE.match(line)
    if match is None:
        return None
    old_count = match.group(2)
    new_count = match.group(4)
    return (
        (int(old_count) if old_count is not None else 1)
        + (int(new_count) if new_count is not None else 1)
    )


def _diff_header_path(raw: str) -> str:
    """Strip the optional ``\t<timestamp>`` suffix and git ``a/``/``b/``
    prefix from a ``---``/``+++`` header operand."""
    value = raw.split("\t", 1)[0]
    if value.startswith("a/") or value.startswith("b/"):
        return value[2:]
    return value


def _pair_path(old_raw: str, new_raw: str) -> str | None:
    """Resolve a file path from a paired ``--- old`` / ``+++ new`` header.

    ``+++ /dev/null`` → deletion (path from the old side); ``--- /dev/null``
    → addition (path from the new side); otherwise the new side is
    authoritative (modify/rename). Both ``/dev/null`` → invalid pair → None.
    """
    old = old_raw.split("\t", 1)[0]
    new = new_raw.split("\t", 1)[0]
    if old == "/dev/null" and new == "/dev/null":
        return None
    if old == "/dev/null":
        return _diff_header_path(new) or None
    if new == "/dev/null":
        return _diff_header_path(old) or None
    return _diff_header_path(new) or None


def _files_from_diff_text(text: Any) -> list[dict[str, Any]] | None:
    """Parse unified-diff text into ``[{path, additions, deletions}, ...]``.

    State machine (idle → in_file → in_hunk):

    * File-segment recognition: a segment becomes VALID only on a paired
      ``---``/``+++`` header (isolated ``Index:`` lines never validate a
      segment — P1-N5); ``Index: <path>`` supplies the authoritative path
      for the next pair, otherwise the pair sides resolve it
      (``/dev/null`` distinguishing add/delete).
    * Hunk exit — dual mechanism (P1-N5): ① ``@@`` line counts
      (``@@ -l,s +l,s @@``); body exhaustion returns to ``in_file`` (before
      exhaustion, everything — including ``---``/``+++``-prefixed lines — is
      hunk BODY, git semantics); ② while NOT exhausted, ``Index: `` / an
      adjacent ``---``→``+++`` PAIR / ``diff --git`` are section boundaries:
      malformed/truncated — the current segment closes with the lines seen,
      the new segment starts normally (宁少计不误归属: undercount rather
      than misattribute).
    * Counting only happens in ``in_hunk``: leading ``+``/``-`` count; header
      lines never do; ``\\ No newline`` markers are ignored. Zero-hunk
      segments (paired headers, no ``@@`` — rename-only) count with ±0.
    """
    if not isinstance(text, str):
        return None
    files: list[dict[str, Any]] = []
    state = "idle"  # idle | in_file | in_hunk
    current: dict[str, Any] | None = None
    section_paired = False
    index_path: str | None = None  # pending Index: path for the NEXT pair
    pending_old: str | None = None  # "--- " operand awaiting its "+++ " pair
    pending_old_counted = False  # that "--- " was consumed as hunk body
    hunk_left: int | None = None  # remaining old+new body budget (None = ∞)

    def _close_section() -> None:
        nonlocal current, section_paired, pending_old, pending_old_counted
        if current is not None and section_paired:
            files.append(current)
        current = None
        section_paired = False
        pending_old = None
        pending_old_counted = False

    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r")
        # ── adjacent ---/+++ PAIR boundary (any state, ②) ──────────────
        if line.startswith("+++ ") and pending_old is not None:
            if pending_old_counted and current is not None:
                # the "--- " was consumed as a body deletion — undo it
                current["deletions"] -= 1
            path = index_path if index_path else _pair_path(pending_old, line[4:])
            _close_section()
            index_path = None
            if path:
                current = {"path": path, "additions": 0, "deletions": 0}
                section_paired = True
                state = "in_file"
            else:
                state = "idle"
            hunk_left = None
            continue
        if line.startswith("diff --git "):
            _close_section()
            index_path = None
            hunk_left = None
            state = "idle"
            continue
        if line.startswith("Index: "):
            _close_section()
            index_path = line[len("Index: "):].strip() or None
            hunk_left = None
            state = "in_file"
            continue
        # ── hunk body (① within the budget everything is body) ─────────
        if state == "in_hunk" and (hunk_left is None or hunk_left > 0):
            if line.startswith("\\"):
                # ``\ No newline at end of file`` — neither counted nor budgeted
                pending_old = None
                pending_old_counted = False
                continue
            if line.startswith("--- "):
                assert current is not None
                current["deletions"] += 1
                if hunk_left is not None:
                    hunk_left -= 1
                    if hunk_left <= 0:
                        # budget exhausted exactly on this line → it was body
                        state = "in_file"
                        pending_old = None
                        pending_old_counted = False
                        continue
                pending_old = line[4:]
                pending_old_counted = True
                continue
            pending_old = None
            pending_old_counted = False
            assert current is not None
            if line.startswith("+"):
                current["additions"] += 1
                consumed = 1
            elif line.startswith("-"):
                current["deletions"] += 1
                consumed = 1
            else:
                consumed = 2  # context line: one old + one new
            if hunk_left is not None:
                hunk_left -= consumed
                if hunk_left <= 0:
                    state = "in_file"
            continue
        # ── idle / in_file (outside any counted hunk body) ─────────────
        if line.startswith("@@") and state == "in_file" and current is not None:
            # new hunk for the open (paired) section — ``current`` is only
            # ever set together with ``section_paired``. A ``@@`` with no
            # valid section cannot be attributed → 宁少计: ignored (the
            # pending ``Index:`` attempt survives for a later pair).
            pending_old = None
            pending_old_counted = False
            hunk_left = _hunk_line_budget(line)
            if hunk_left:  # 0-line hunk (``@@ -0,0 +0,0 @@``) → stays in_file
                state = "in_hunk"
            continue
        if line.startswith("--- "):
            pending_old = line[4:]
            pending_old_counted = False
            continue
        if line.startswith("+++ "):
            # stray +++ without a pending --- : not a pair, ignore
            pending_old = None
            continue
        # everything else (=== separators, context, garbage) is ignored
    _close_section()
    return files if files else None


def _compact_tool_files(source_files: list[Any]) -> list[dict[str, Any]]:
    """§4b: compact projection of upstream ``state.metadata.files`` (the
    multi-file apply_patch shape ``{filePath, relativePath, type, patch,
    additions, deletions}``) → ``{path, additions?, deletions?, status?}``.

    ``path = relativePath ?? filePath``; ``status`` mirrors ``type``; heavy
    bodies (``patch``) are stripped. Non-dict entries are skipped (they still
    count toward the SOURCE-based ``filesTotal`` / ref eligibility at the
    call site — the ref-保活 judgment uses the source value, P2-N1). Count
    values are re-validated with :func:`_valid_count` — a malformed count is
    dropped rather than coerced. Pure projection: builds fresh dicts, never
    mutates the source.
    """
    compact: list[dict[str, Any]] = []
    for item in source_files:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {}
        relative = item.get("relativePath")
        file_path = item.get("filePath")
        path = relative if isinstance(relative, str) else (
            file_path if isinstance(file_path, str) else None
        )
        if path is not None:
            entry["path"] = path
        additions = item.get("additions")
        if _valid_count(additions):
            entry["additions"] = additions
        deletions = item.get("deletions")
        if _valid_count(deletions):
            entry["deletions"] = deletions
        status = item.get("type")
        if isinstance(status, str) and status:
            entry["status"] = status
        compact.append(entry)
    return compact


def _valid_source_diffstats(value: Any) -> bool:
    """⓪-leg guard: a source ``metadata.diffStats`` is authoritative when it
    is a dict whose ``additions``/``deletions``/``files`` all pass
    :func:`_valid_count` — upstream (or an existing consumer) already
    blessed the value, derived stats never override it."""
    if not isinstance(value, dict):
        return False
    return all(_valid_count(value.get(key)) for key in ("additions", "deletions", "files"))


def _aggregate_metadata_diffstats(
    source_metadata: dict[str, Any], tool: Any,
) -> dict[str, int] | None:
    """§4b FROZEN aggregate priority chain for ``state.metadata.diffStats``:

    ⓪ source ``diffStats`` present & legal shape → ``None`` (keep source —
    it was already picked verbatim by the whitelist; derivation skipped);
    ① ``filediff`` structurally valid (exception-safe
    :func:`_compute_diffstats` → non-None) → inject;
    ② ``files`` compact entries carry ≥1 valid count →
    :func:`_compute_diffstats_from_files`;
    ③ ``tool == "edit"`` and ``metadata.diff`` parses (not truncated) →
    :func:`_files_from_diff_text` aggregation;
    ④ no injection. Caller injects the non-None result after thresholding.
    """
    if _valid_source_diffstats(source_metadata.get("diffStats")):
        return None  # ⓪ keep source value
    diffstats = _compute_diffstats(source_metadata.get("filediff"))  # ①
    if diffstats is not None:
        return diffstats
    source_files = source_metadata.get("files")  # ②
    if isinstance(source_files, list) and source_files:
        diffstats = _compute_diffstats_from_files(
            _compact_tool_files(source_files))
        if diffstats is not None:
            return diffstats
    if tool == "edit" and source_metadata.get("truncated") is not True:  # ③
        parsed = _files_from_diff_text(source_metadata.get("diff"))
        if parsed:
            return _compute_diffstats_from_files(parsed)
    return None  # ④


def _maybe_inline_state_field(
    thin_state: dict[str, Any],
    state: dict[str, Any],
    key: str,
    omitted: list[str],
    budget: dict[str, int] | None,
    limits: SkeletonLimits,
) -> None:
    """Inline ``state[key]`` into ``thin_state`` iff it fits BOTH the per-field
    cap (``limits.field_bytes``) and the remaining per-message budget
    (``limits.message_bytes``); otherwise record
    ``state.<key>`` in ``omitted`` (no partial truncation — the field is either
    fully present or fully expandable via ``/full``). Mutates ``thin_state`` /
    ``omitted`` / ``budget`` in place. Only called for
    :data:`SKELETON_INLINE_FIELDS` (``output`` / ``error``).
    """
    if key not in state:
        return
    size = _field_byte_size(state[key])
    field_ok = size <= limits.field_bytes
    budget_ok = (
        budget is None
        or budget["used"] + size <= limits.message_bytes
    )
    if field_ok and budget_ok:
        thin_state[key] = deepcopy(state[key])
        if budget is not None:
            budget["used"] += size
    else:
        omitted.append(f"state.{key}")
        if key == "output" and state[key] not in (None, ""):
            # §3 P1-4: omitting a real output still hands the toolcard a
            # size hint — the SAME wire byte size that failed the caps
            # (len(orjson.dumps(value)), multibyte counted like the wire).
            # error gets no counterpart (no consumer); None/"" never reach
            # here meaningfully but stay guarded anyway.
            thin_state["outputBytes"] = size


# wire_view default 3 = pure-function historical FREEZE (golden: tests/
# test_expand_href_v4.py::test_projection_default_view_keeps_frozen_v3_bytes);
# production always passes 4 — routes/messages/_list.py::_expand_wire_view
# returns 4 unconditionally (D5). Do not "fix" the default to 4.
def _tool(part: dict[str, Any], *, budget: dict[str, int] | None = None, limits: SkeletonLimits = DEFAULT_SKELETON_LIMITS, sid: str | None = None, wire_view: int = 3) -> dict[str, Any]:
    result = _pick(part, TOOL_KEYS)
    omitted: list[str] = []
    refs: list[tuple[str, str]] = []
    part_id = part.get("id")
    state = part.get("state")
    if isinstance(state, dict):
        thin_state = _pick(state, {"status", "title", "time"})
        # §2 P1-3: compress-only title synthesis, FROZEN evaluation order —
        # (1) input is a dict (never .get() on a non-dict), (2) ``content``
        # is a non-empty list, (3) ``content[0]`` is a dict, (4) only then
        # topic → summary → segment-count fallback. Any miss → no title at
        # all (the fallback fires only after 1-3 passed and both keys
        # missed). An existing title (non-empty) is never overwritten.
        if part.get("tool") == "compress" and not thin_state.get("title"):
            synth_input = state.get("input")
            if isinstance(synth_input, dict):
                synth_content = synth_input.get("content")
                if isinstance(synth_content, list) and synth_content:
                    first = synth_content[0]
                    if isinstance(first, dict):
                        synth_title = _clip(
                            first.get("topic"), COMPRESS_TITLE_CLIP_CHARS)
                        if synth_title is None:
                            synth_title = _clip(
                                first.get("summary"), COMPRESS_TITLE_CLIP_CHARS)
                        if synth_title is None:
                            synth_title = f"压缩 {len(synth_content)} 段"
                        thin_state["title"] = synth_title
        source_input = state.get("input")
        if isinstance(source_input, dict):
            thin_input = _pick(source_input, TOOL_INPUT_KEYS)
            if thin_input:
                thin_state["input"] = thin_input
            omitted.extend(
                f"state.input.{key}" for key in source_input if key not in TOOL_INPUT_KEYS
            )
            if part_id and any(key not in TOOL_INPUT_KEYS for key in source_input):
                # §5.3: multiple non-whitelist input keys collapse to ONE
                # part_state_input_full ref (dedup by category+messageID+partID).
                refs.append(("part_state_input_full", part_id))
        elif source_input is not None:
            omitted.append("state.input")
        source_metadata = state.get("metadata")
        if isinstance(source_metadata, dict):
            # §4b: ``files`` is whitelisted VIA the compact projection
            # (never verbatim) — pick the other whitelist keys, then attach
            # the mapped, capped list (source-count filesTotal on overflow).
            metadata_whitelist = TOOL_METADATA_KEYS - {"files"}
            thin_metadata = _pick(source_metadata, metadata_whitelist)
            source_files = source_metadata.get("files")
            source_files_live = isinstance(source_files, list) and bool(source_files)
            if source_files_live:
                compact_files = _compact_tool_files(source_files)
                if compact_files:
                    thin_metadata["files"] = compact_files[:FILES_PROJECTION_CAP]
                # P2-25：触发口径 = **有效映射条目数**超 cap（§10.2 修订四），
                # 非 source 数组长度——源 15 条仅 8 条可映射时不截断、不附
                # filesTotal；触发时 filesTotal 的**值**仍 = 源计数（含无效
                # 条目，:len:`source_files`）。
                if len(compact_files) > FILES_PROJECTION_CAP:
                    thin_metadata["filesTotal"] = len(source_files)
            if thin_metadata:
                thin_state["metadata"] = thin_metadata
            omitted.extend(
                f"state.metadata.{key}"
                for key in source_metadata if key not in TOOL_METADATA_KEYS
            )
            # Ref保活 (P1-2 + P2-N1): the part_state_metadata_full ref fires
            # when any non-whitelist key is omitted OR the SOURCE
            # ``metadata.files`` is a non-empty list — the SOURCE value
            # decides (not the mapped result), so an all-malformed source
            # list still keeps the expand ref alive.
            if part_id and (
                any(key not in TOOL_METADATA_KEYS for key in source_metadata)
                or source_files_live
            ):
                refs.append(("part_state_metadata_full", part_id))
        # Thresholded: inline small output/error (per-field + per-message caps),
        # omit large or budget-spent ones. A field is fully inlined or fully
        # omitted — never half-truncated.
        for key in SKELETON_INLINE_FIELDS:
            value = state.get(key)
            before = len(omitted)
            _maybe_inline_state_field(thin_state, state, key, omitted, budget, limits=limits)
            if len(omitted) > before and value not in (None, "") and part_id:
                refs.append((f"part_state_{key}", part_id))
        # Always-omit heavy nested fields (giant JSON / binary-ish payloads).
        for key in SKELETON_ALWAYS_OMIT_FIELDS:
            if key in state:
                omitted.append(f"state.{key}")
                # §5.3: attachments has an expand category — ref only when the
                # value was non-null/non-empty at omission time. structured /
                # result / raw are /full-only (§2.3) — no refs.
                if key == "attachments" and state[key] not in (None, [], {}) and part_id:
                    refs.append(("part_state_attachments", part_id))
        # Inject compact aggregate diffStats AFTER thresholding so it is
        # never elligible for omission — the ~50 B object is well below the
        # per-field cap, and sits in TOOL_METADATA_KEYS so it survives the
        # whitelist. §4b FROZEN priority chain: ⓪ source diffStats kept →
        # ① filediff → ② files → ③ edit diff parse → ④ none. digest 对账
        # （tool 完成→message.updated 映射）为后续 SSE 实测验证项，本轮不实现.
        #
        # NOTE: ``thin_metadata`` from ``_pick`` above is a local var (empty
        # disconnected dict when no whitelist keys matched). We must write
        # to ``thin_state["metadata"]`` explicitly to ensure the key exists.
        if isinstance(source_metadata, dict):
            diffstats = _aggregate_metadata_diffstats(
                source_metadata, part.get("tool"))
            if diffstats is not None:
                if "metadata" not in thin_state:
                    thin_state["metadata"] = {}
                thin_state["metadata"]["diffStats"] = diffstats
            # §4d B2: edit synthetic ``metadata.files`` — ONLY when the
            # source metadata has no ``files`` of its own and the diff is
            # not truncated (same eligibility as the expand extractor);
            # capped like §4b. P2-25 口径核对：``_files_from_diff_text``
            # 每条返回均为有效映射条目（有效数 == parsed 数 == 本合成
            # 路径的源计数），故 ``len(parsed_files) > CAP`` 的触发口径
            # 与「有效映射条目超 10」天然一致，filesTotal 值 = parsed 数。
            if (part.get("tool") == "edit"
                    and "files" not in source_metadata
                    and source_metadata.get("truncated") is not True):
                parsed_files = _files_from_diff_text(source_metadata.get("diff"))
                if parsed_files:
                    if "metadata" not in thin_state:
                        thin_state["metadata"] = {}
                    thin_state["metadata"]["files"] = parsed_files[:FILES_PROJECTION_CAP]
                    if len(parsed_files) > FILES_PROJECTION_CAP:
                        thin_state["metadata"]["filesTotal"] = len(parsed_files)
        result["state"] = thin_state
    for key in part:
        if key not in TOOL_KEYS and key != "state":
            omitted.append(key)
    return _emit_expand_refs(_mark(result, omitted), refs, sid, wire_view)


def _patch(part: dict[str, Any], *, budget: dict[str, int] | None = None, limits: SkeletonLimits = DEFAULT_SKELETON_LIMITS, sid: str | None = None) -> dict[str, Any]:
    # §4.2 (P0): v1.18.16 PatchPart = {type, hash, files: string[]}. ``hash``
    # was previously dropped into omitted — now preserved.
    # §4a: ``files`` normalizes to ``{path}`` objects — a string entry maps
    # to ``{"path": s}`` (the card reads ONE shape); legacy dict entries
    # (pre-v1.18) keep the per-file pick ``{path, additions, deletions,
    # status}``; non-str/dict entries are skipped but still count toward the
    # SOURCE-count ``filesTotal``. Cap 10 entries; on overflow attach
    # ``filesTotal = len(source list)`` so the true breadth stays readable.
    result = _pick(part, PART_IDS | {"hash"})
    omitted: list[str] = []
    normalized: list[dict[str, Any]] = []
    files = part.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, str):
                normalized.append({"path": item})
            elif isinstance(item, dict):
                normalized.append(
                    _pick(item, {"path", "additions", "deletions", "status"}))
        result["files"] = normalized[:FILES_PROJECTION_CAP]
        # P2-25：触发口径 = **有效映射条目数**超 cap（§10.2 修订四），
        # 非 source 数组长度——被跳过的非 str/dict 条目不再单独触发
        # filesTotal；触发时 filesTotal 的**值**仍 = 源计数（含被跳过
        # 条目，:len:`files`）。
        if len(normalized) > FILES_PROJECTION_CAP:
            result["filesTotal"] = len(files)
    metadata = part.get("metadata")
    if isinstance(metadata, dict) and "path" in metadata:
        result["metadata"] = {"path": deepcopy(metadata["path"])}
    state = part.get("state")
    if isinstance(state, dict):
        thin_state = _pick(state, {"status", "title", "time"})
        source_input = state.get("input")
        if isinstance(source_input, dict):
            path_input = _pick(source_input, {"path", "filePath", "file_path"})
            if path_input:
                thin_state["input"] = path_input
            omitted.extend(
                f"state.input.{key}"
                for key in source_input if key not in {"path", "filePath", "file_path"}
            )
        # Thresholded like _tool: inline small output/error, omit large or
        # budget-spent. Patch parts share the per-message budget with tool
        # parts (part order) so neither can starve the other.
        for key in SKELETON_INLINE_FIELDS:
            _maybe_inline_state_field(thin_state, state, key, omitted, budget, limits=limits)
        result["state"] = thin_state
    # Inject compact diffStats from files[] into state.metadata.diffStats,
    # mirroring _tool() above. ocdroid reads state.metadata?.get("diffStats")
    # — a top-level result["diffStats"] is NEVER read (PartStateSerializer
    # only drops diagnostics, does not relocate). Injected AFTER thresholding;
    # the ~50 B object is never omit-eligible. A patch part may carry files[]
    # WITHOUT an upstream state object — create the minimal state.metadata
    # container so the client read path (state.metadata?.get) does not
    # chain-break. §4a guard: only when ≥1 normalized entry carries a
    # _valid_count addition/deletion — string-derived {path} entries never
    # fabricate stats (R1-M1), invalid values contribute 0, never raises.
    if isinstance(files, list):
        diffStats = _compute_diffstats_from_files(normalized)
        if diffStats is not None:
            if "state" not in result:
                result["state"] = {}
            thin_state = result["state"]
            if "metadata" not in thin_state:
                thin_state["metadata"] = {}
            thin_state["metadata"]["diffStats"] = diffStats
    for key in part:
        if key not in PART_IDS | {"files", "metadata", "state", "hash"}:
            omitted.append(key)
    # §5.3: patch has no expand categories — files kept verbatim; any state
    # omission on a patch part is /full-only (state categories are tool-only).
    return _mark(result, omitted)


# wire_view default 3 = pure-function historical FREEZE (golden: tests/
# test_expand_href_v4.py::test_projection_default_view_keeps_frozen_v3_bytes);
# production always passes 4 — routes/messages/_list.py::_expand_wire_view
# returns 4 unconditionally (D5). Do not "fix" the default to 4.
def _file(part: dict[str, Any], *, sid: str | None = None, wire_view: int = 3) -> dict[str, Any]:
    result = _pick(part, PART_IDS | {"filename", "mime"})
    omitted: list[str] = []
    refs: list[tuple[str, str]] = []
    part_id = part.get("id")
    url = part.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")) and len(url) <= FILE_URL_LIMIT:
        result["url"] = url
    elif "url" in part:
        result["url"] = None
        omitted.append("url")
        if url not in (None, "") and part_id:
            refs.append(("part_url", part_id))
    if "source" in part:
        omitted.append("source")
        if part.get("source") not in (None, {}, [], "") and part_id:
            refs.append(("part_source", part_id))
    for key in part:
        if key not in PART_IDS | {"filename", "mime", "url", "source"}:
            omitted.append(key)
    return _emit_expand_refs(_mark(result, omitted), refs, sid, wire_view)


# wire_view default 3 = pure-function historical FREEZE (golden: tests/
# test_expand_href_v4.py::test_projection_default_view_keeps_frozen_v3_bytes);
# production always passes 4 — routes/messages/_list.py::_expand_wire_view
# returns 4 unconditionally (D5). Do not "fix" the default to 4.
def skeleton_part(part: dict[str, Any], *, budget: dict[str, int] | None = None, limits: SkeletonLimits = DEFAULT_SKELETON_LIMITS, sid: str | None = None, wire_view: int = 3) -> dict[str, Any]:
    # §5: ``expandRefs`` is a sidecar-OWNED key — a foreign value from upstream
    # is dropped before any projection. It must never leak into the output, into
    # ``omitted``/``hasFull``, or survive a whole-part deepcopy (compaction);
    # the sidecar replaces it deterministically when it generates refs (m2).
    part = {key: value for key, value in part.items() if key != "expandRefs"}
    part_type = part.get("type")
    if part_type == "text":
        # [3.2.0] TextPart.text is ALWAYS inlined verbatim, regardless of
        # size — the conversation body is the primary browsing surface and is
        # never reduced (owner decision 2026-08-17). The part_text expand
        # category is no longer produced by this projection (the endpoint
        # stays for historical 3.1.x responses). §2.3 still holds:
        # synthetic/ignored/time are /full-only — omitted, never refs.
        copied = _pick(part, PART_IDS | {"text"})
        omitted = [key for key in part if key not in PART_IDS | {"text"}]
        return _emit_expand_refs(_mark(copied, omitted), [], sid, wire_view)
    if part_type == "reasoning":
        # n1: threshold evaluated on the ORIGINAL text BEFORE _pick — an
        # oversized text is never deep-copied.
        exceeds_reasoning = _utf8_bytes_exceeds(part.get("text"), REASONING_INLINE_MAX_BYTES)
        result = _pick(part, PART_IDS if exceeds_reasoning else PART_IDS | {"text"})
        omitted = [key for key in part if key not in PART_IDS | {"text"}]
        refs: list[tuple[str, str]] = []
        if exceeds_reasoning:
            result["text"] = None
            omitted.append("text")
            if result.get("id"):
                refs.append(("part_reasoning", result["id"]))
        # reasoning metadata/time omissions are /full-only (§2.3) — no refs.
        return _emit_expand_refs(_mark(result, omitted), refs, sid, wire_view)
    if part_type == "tool":
        return _tool(part, budget=budget, limits=limits, sid=sid, wire_view=wire_view)
    if part_type == "patch":
        return _patch(part, budget=budget, limits=limits, sid=sid)
    if part_type == "file":
        return _file(part, sid=sid, wire_view=wire_view)
    if part_type in {"step-start", "step-finish"}:
        result = _mark(_pick(part, PART_IDS), [key for key in part if key not in PART_IDS])
        refs: list[tuple[str, str]] = []
        # §5.3: snapshot omission maps to part_snapshot — only when the
        # snapshot existed non-null/non-empty at omission time. reason/cost/
        # tokens are /full-only (§2.3).
        if part.get("snapshot") not in (None, "") and result.get("id"):
            refs.append(("part_snapshot", result["id"]))
        return _emit_expand_refs(result, refs, sid, wire_view)
    if part_type == "compaction":
        copied = deepcopy(part)
        # Compaction is retained unless the single part violates its explicit cap.
        if len(orjson.dumps(copied)) <= COMPACTION_PART_LIMIT:
            return copied
        # §5.3: over-limit compaction → omitted ["*"] + a compaction_full ref —
        # NOT /full-only (§2.3 exempts "compaction 超限" from the ["*"] row).
        # Lane-A falsy-id guard: partID only when the part id is truthy;
        # _emit_expand_refs suppresses without a messageID.
        refs = [("compaction_full", part["id"])] if part.get("id") else []
        return _emit_expand_refs(_mark(_pick(part, PART_IDS), ["*"]), refs, sid, wire_view)
    return _mark(_pick(part, PART_IDS), [key for key in part if key not in PART_IDS] or ["*"])


def skeleton_message(
    message: dict[str, Any], *,
    limits: SkeletonLimits = DEFAULT_SKELETON_LIMITS,
    fingerprint: bool = False,
    sid: str | None = None,
    # wire_view default 3 = pure-function historical FREEZE (golden: tests/
    # test_expand_href_v4.py::test_projection_default_view_keeps_frozen_v3_bytes);
    # production always passes 4 — routes/messages/_list.py::_expand_wire_view
    # returns 4 unconditionally (D5). Do not "fix" the default to 4.
    wire_view: int = 3,
) -> dict[str, Any]:
    # P1-29: normalise nested fields defensively. A malformed upstream message
    # where ``info`` is null or ``parts`` is a non-list (int/bool/string) would
    # crash the projection: ``None.get("id")`` → AttributeError,
    # ``for part in 1`` → TypeError. Normalise to safe defaults so a single bad
    # message degrades to a placeholder rather than a 500.
    info = message.get("info") if isinstance(message, dict) else None
    if not isinstance(info, dict):
        info = {}
    result = {"info": deepcopy(info)}
    # §5: ``expandRefs`` is a sidecar-OWNED key — a foreign value in the
    # upstream info is dropped; the sidecar replaces it deterministically when
    # it generates refs (m2).
    result["info"].pop("expandRefs", None)
    info_id = result["info"].get("id")
    message_id = info_id if info_id else "unknown"
    # §4.1: ``info.summary.diffs`` is ALWAYS projected as ``null`` (unconditional
    # reduction); summary siblings are preserved. §5.2/§5.3: a message-level
    # ``info_summary_diffs`` ref is generated iff diffs was a NON-EMPTY LIST at
    # omission time (m1: type-aware — ``""`` / ``False`` / ``{}`` don't count)
    # AND a real message id exists (M3: the ``unknown`` fallback must not yield
    # an unusable ref).
    summary = result["info"].get("summary")
    if isinstance(summary, dict) and "diffs" in summary:
        orig_diffs = summary["diffs"]
        summary["diffs"] = None
        if sid and info_id and isinstance(orig_diffs, list) and orig_diffs:
            result["info"]["expandRefs"] = [
                _expand_ref(
                    "info_summary_diffs", message_id, None, sid, wire_view,
                )
            ]
    parts = message.get("parts") if isinstance(message, dict) else None
    if not isinstance(parts, list):
        parts = []
    # Per-message cumulative inline-byte budget shared across all parts in part
    # order. Bounds total inlined output/error so a single message cannot
    # balloon even when many small fields each individually pass the per-field
    # cap. Created here (per-message) and threaded through skeleton_part.
    budget = {"used": 0}
    thin_parts = [
        skeleton_part(
            part, budget=budget, limits=limits, sid=sid, wire_view=wire_view,
        )
        for part in parts if isinstance(part, dict)
    ]
    if not any(_is_renderable(part) for part in thin_parts):
        thin_parts.append({
            "id": f"thin_placeholder_{message_id}",
            "messageID": message_id,
            "type": "text",
            "text": PLACEHOLDER_TEXT,
            "hasFull": True,
            "omitted": ["parts"],
        })
    result["parts"] = thin_parts
    if fingerprint or limits.fingerprint:
        # B3: inject at projection-completion time (the message's final
        # assembly point for non-merged lists; merged splices overwrite
        # this later via recompute_fingerprint). Default stays OFF so the
        # pure functions keep their historical output shape (existing
        # tests unchanged); routes thread the config switch through
        # ``SkeletonLimits.fingerprint`` (worker signatures unchanged).
        recompute_fingerprint(result)
    return result


def skeleton_messages(
    messages: list[dict[str, Any]], *,
    limits: SkeletonLimits = DEFAULT_SKELETON_LIMITS,
    fingerprint: bool = False,
    sid: str | None = None,
    # wire_view default 3 = pure-function historical FREEZE (golden: tests/
    # test_expand_href_v4.py::test_projection_default_view_keeps_frozen_v3_bytes);
    # production always passes 4 — routes/messages/_list.py::_expand_wire_view
    # returns 4 unconditionally (D5). Do not "fix" the default to 4.
    wire_view: int = 3,
) -> list[dict[str, Any]]:
    """Project a full upstream message list to skeletons (design-expand §4).

    v4 §14: ``wire_view`` selects the ``?v=`` value in every expandRefs
    href (3 → frozen v3 bytes, 4 → ``?v=4``); default 3 keeps the pure
    functions' historical output byte-identical.
    """
    return [
        skeleton_message(
            message, limits=limits, fingerprint=fingerprint, sid=sid,
            wire_view=wire_view,
        )
        for message in messages
    ]


# ---------------------------------------------------------------------------
# /full diagnostics strip (additive; wire version UNCHANGED — stays 1).
#
# opencode's ``edit``/``write`` tools attach LSP diagnostics to every affected
# part's ``state.metadata.diagnostics``. ocdroid NEVER consumes them —
# ``Message.kt#parsePartState`` unconditionally deletes the ``diagnostics``
# key (comment: "is never read here"). For a slim client they are pure
# down-wire traffic + parse/heap cost with zero UI value, so the ``mode=full``
# routes strip them server-side.
#
# This is NOT a skeleton projection: every other field (output / text / files /
# metadata siblings / ...) is preserved verbatim so clients expanding a thin
# skeleton still fetch the WHOLE part — only the never-read diagnostics map is
# removed. ``diagnostics`` is the sole key touched; sibling keys and the
# container itself are left intact (an emptied ``metadata`` stays as ``{}``,
# never dropped), keeping ``/full`` semantics field-faithful.
# ---------------------------------------------------------------------------
def strip_diagnostics_message(message: dict[str, Any]) -> dict[str, Any]:
    """Strip the never-consumed LSP ``diagnostics`` map from every part **in place**.

    Two locations are considered per part, but ONLY
    ``state.metadata.diagnostics`` (where opencode's ``edit``/``write`` tools
    attach LSP diagnostics — the sole named target) is removed. A top-level
    ``metadata.diagnostics`` is intentionally NOT touched: it is outside the
    stated target and its consumption is unproven, so stripping it would be
    speculative overreach beyond "remove only ``diagnostics`` from the part".

    In-place: production callers feed a freshly ``orjson.loads``'d tree with no
    shared aliases, so a full ``deepcopy`` would only burn CPU/RSS. The input
    dict is mutated (``metadata.pop("diagnostics")``) and returned. Used by the
    ``mode=full`` routes via :func:`transform.strip_diagnostics_and_pack` and
    by the G6 batch full path.

    Shape-robust: a non-dict body (e.g. a list/scalar from a malformed upstream
    200) is returned as-is — there are no parts to scrub — so the route still
    serves the body, matching the prior verbatim passthrough for non-conforming
    shapes rather than turning a weird upstream 200 into a 500.
    """
    if not isinstance(message, dict):
        return message
    parts = message.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            state = part.get("state")
            if isinstance(state, dict):
                metadata = state.get("metadata")
                if isinstance(metadata, dict):
                    metadata.pop("diagnostics", None)
    return message


def _is_renderable(part: dict[str, Any]) -> bool:
    part_type = part.get("type")
    if part_type in {"text", "reasoning"}:
        # §4.3: a part whose text was reduced to null but carries expandRefs is
        # still renderable — the client sees the part skeleton + an expand
        # entry, not a whole-page placeholder.
        return bool(part.get("text")) or bool(part.get("expandRefs"))
    if part_type == "tool":
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        return bool(part.get("tool") or state.get("title") or state.get("input"))
    if part_type == "patch":
        return bool(part.get("files") or part.get("metadata") or part.get("state"))
    if part_type == "file":
        return bool(part.get("filename") or part.get("url"))
    return False


SESSION_KEYS = {
    "id", "directory", "parentID", "projectID", "title", "agent", "model",
}


def skeleton_session(session: dict[str, Any]) -> dict[str, Any]:
    result = _pick(session, SESSION_KEYS)
    for key, allowed in (
        ("time", {"created", "updated", "archived"}),
        ("summary", {"additions", "deletions", "files"}),
        ("revert", {"messageID", "partID"}),
    ):
        value = session.get(key)
        if isinstance(value, dict):
            result[key] = _pick(value, allowed)
    return result


# ---------------------------------------------------------------------------
# Catalog skeleton projections (additive; wire version UNCHANGED — stays 2).
#
# opencode's ``/command`` and ``/agent`` catalogs are large (live-measured
# ~292 KB / ~250 KB raw) but ocdroid reads only a handful of fields per entry
# for its command palette / agent picker UIs. The dominant cost is never-
# consumed content: command ``template`` (~97.7% of bytes) and agent
# ``prompt``+``permission`` (>96%). These projections keep only the UI-consumed
# whitelist and drop the rest — measured savings ~97.6% (command) / ~95.8%
# (agent) raw.
#
# Whitelists are the client-DEFINED fields (rev: a skeleton must never drop a
# key the client already reads). Optional keys are picked only when present
# (``_pick``), so a sparse upstream row yields a sparse skeleton rather than a
# key-shaped hole. ``hasFull``/``omitted`` are NOT emitted — these are catalog
# listings, not message parts; there is no per-entry expand endpoint, and the
# client always has the full upstream ``/command`` / ``/agent`` (catch-all
# passthrough) as the authoritative source when it needs an omitted field.
# ---------------------------------------------------------------------------
COMMAND_SKELETON_KEYS = {"name", "description", "agent", "hints"}


def skeleton_command(command: dict[str, Any]) -> dict[str, Any]:
    """Whitelist projection of an opencode command catalog entry.

    Keeps the ocdroid-consumed fields (``name`` / ``description`` / ``agent``
    / ``hints``) and drops the never-read ``template`` / ``source`` /
    ``model`` / ``subtask``. ``agent`` is optional in opencode's schema
    (present on a minority of commands; often ``null``) — it is preserved
    verbatim when present so the client's agent-scoping logic sees the same
    key shape as the upstream catalog.
    """
    return _pick(command, COMMAND_SKELETON_KEYS)


def skeleton_commands(commands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Non-dict items (e.g. a stray ``null`` / string / number in a malformed
    # upstream catalog) are silently skipped, mirroring ``skeleton_messages``'s
    # ``if isinstance(part, dict)`` filter. A non-dict element never reaches
    # ``_pick`` (which would otherwise ``TypeError`` on ``key in value``), so a
    # single bad row degrades to a shorter skeleton rather than a 500.
    return [skeleton_command(item) for item in commands if isinstance(item, dict)]


AGENT_SKELETON_KEYS = {"name", "description", "mode", "hidden", "native"}


def skeleton_agent(agent: dict[str, Any]) -> dict[str, Any]:
    """Whitelist projection of an opencode agent catalog entry.

    Keeps the ocdroid-consumed fields (``name`` / ``description`` / ``mode``
    / ``hidden`` / ``native``) and drops the never-read ``prompt`` (the
    largest field — the full system prompt), ``permission`` (the
    ``Permission.Ruleset`` list — no UI consumer; not the pending permission
    card), ``topP`` / ``temperature`` / ``color`` / ``variant`` / ``options``
    / ``steps`` / ``model``.
    """
    return _pick(agent, AGENT_SKELETON_KEYS)


def skeleton_agents(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Non-dict items are silently skipped — see skeleton_commands for rationale.
    return [skeleton_agent(item) for item in agents if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# v4 sessions DB 投影 skeleton（B3a-B2；additive——v3 既有函数零改动）。
#
# v4-contract §4.1：SessionSkeletonV4 = v3 SESSION_KEYS 投影 + ``project``
# 对象（join 缺行 → null，design-v4-dbaux §8 组装容忍）+ v4-only 字段
# （tokens 五列平铺，**键名 = 真库列名** tokens_input/tokens_output/...，
# R2 实证冻结——v2.2 模板 tokens_in/out 为撰写笔误）。输入 =
# ``dbaux.projection.rows_to_records`` 产出的记录 dict（键 = DB 列名 +
# p_id/p_name/p_worktree join 列）——DB 列名与 wire 键的映射集中在此处
# （tokens_input/tokens_output 真库列名，R2 实证；wire 侧 time/summary
# 子对象与 v3 ``skeleton_session`` 同构，但源是平铺 DB 列而非上游嵌套
# JSON，故独立投影、不经 ``_pick`` 复用——两函数是不同输入形状的投影，
# 不是同一投影的两份拷贝）。
# ---------------------------------------------------------------------------

def project_rows_to_v4_skeletons(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """DB 投影记录 → SessionSkeletonV4 列表（wire v4-only）。

    行级容忍（§8 后处理末段）：非 dict 行 / 缺 ``id`` 行直接跳过——
    ``rows_to_records`` 已在 SQL 行层执行同样容忍，此处防御的是组装器
    被绕过直喂原始行的场景。
    """
    skeletons: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = row.get("id")
        if sid is None:
            continue
        # Q7-P3-20（owner 裁决）：混合 NULL summary 行形状统一。本路径
        # 曾对 NULL 列无条件发含 null 子值的 summary 对象——与上游
        # ``fromRow``（session.ts:59-68 的 ``?? 0`` 填充）同族的「伪造完
        # 整对象」语义，和 canonical projector（:1559 起三态判定）分裂：
        # 同一条混合 NULL 行，canonical 发 ``null`` + ``partial: true``，
        # 本路径发 ``{additions: null, deletions: N, ...}``。契约 §13.1
        # 冻结 ``summary`` 为 ``{additions, deletions, files: number} |
        # null``——含 null 子值的对象不合法 → 统一到契约形态 ``null``
        # （本路径为 4.0.0 稀疏形态、无 partial 标记键可置；canonical
        # 路径同输入另附 ``partial: true``，summary 值形状两路径一致）。
        # 不模仿上游 0 填充：sidecar compact 语义不伪造计数——0 会与真
        # 实的 0 增删混淆，来源不完整就如实置 null。
        summary_additions = row.get("summary_additions")
        summary_deletions = row.get("summary_deletions")
        summary_files = row.get("summary_files")
        if (
            _canonical_number(summary_additions)
            and _canonical_number(summary_deletions)
            and _canonical_number(summary_files)
        ):
            # 三列全为规范数值 → 完整对象
            summary: dict[str, Any] | None = {
                "additions": summary_additions,
                "deletions": summary_deletions,
                "files": summary_files,
            }
        else:
            # 三列全 NULL = 业务合法 null（contract §13.2）；混合
            # NULL/非数值 = 来源不完整 → 同置 null（canonical 路径
            # 此时另置 partial:true）
            summary = None
        item: dict[str, Any] = {
            "id": sid,
            "directory": row.get("directory"),
            "parentID": row.get("parent_id"),
            "projectID": row.get("project_id"),
            "title": row.get("title"),
            "agent": row.get("agent"),
            "model": row.get("model"),
            "time": {
                "created": row.get("time_created"),
                "updated": row.get("time_updated"),
                "archived": row.get("time_archived"),
            },
            "summary": summary,
            "tokens_input": row.get("tokens_input"),
            "tokens_output": row.get("tokens_output"),
            "tokens_reasoning": row.get("tokens_reasoning"),
            "tokens_cache_read": row.get("tokens_cache_read"),
            "tokens_cache_write": row.get("tokens_cache_write"),
        }
        # revert / permission / metadata：上游 JSON 列（解析后 dict 或 NULL）
        if isinstance(row.get("revert"), dict):
            item["revert"] = _pick(row["revert"], {"messageID", "partID"})
        pid = row.get("p_id")
        if pid is None:
            # join 缺行（project_id 悬挂/NULL）→ null（session.ts:595 同语义）
            item["project"] = None
        else:
            item["project"] = {
                "id": pid,
                "name": row.get("p_name"),
                "worktree": row.get("p_worktree"),
            }
        skeletons.append(item)
    return skeletons


# ---------------------------------------------------------------------------
# v4 canonical item projector（§13 正式修订；rev 门禁 P0-1/P0-2 修复）。
#
# v4-contract §13.3 冻结不变量：列表 item 与单查**同一 canonical projector
# 代码路径**——分裂投影 = 实现违约。本节就是那唯一 projector：
#
# * ``native_session_to_record``：native 入口**归一化器**（非投影副本）——
#   把上游 SessionInfo（camelCase）映射为 DB 投影记录形状（snake_case 列 +
#   p_id/p_name/p_worktree join 列）。键 **presence** 是三态载体：上游键
#   缺席 → 记录键缺席（来源不可得）；显式 null → 在场 None（业务合法
#   null）；值 → 值。不做任何形状决策（§13.3「同一 keep/drop 规则」）。
# * ``canonical_session_skeleton_v4``：唯一装配器——DB 记录
#   （``rows_to_records`` 产出，列恒在场、值 None = 业务 null）与 native
#   归一化记录（键可缺席）喂**同一函数**，产出 §13.1 canonical 对象：
#
#   - required nullable 恒发（§13.1/§13.2 真值表）：业务 null → null；
#     来源不可得 → null + partial:true（+ degraded，§13.2b 三态②）。
#   - ``project`` 双形态（§13.5）：projectID null → project **缺席**；
#     非空 projectID 且 join 缺行（含 native 无 join）/ id mismatch /
#     worktree 空串 → project:null + partial:true（三不变量任一不满足）。
#   - required 且不可 null 字段（§13.2a：id/directory/title/
#     time.created/time.updated）缺席或 null → 返回 None，路由转整响应
#     503 ``auxiliary_unavailable``（禁占位值 / 禁丢键 / 禁跨源拼接）。
#   - ``fallback=True``：native 回退态——item degraded 恒 true（§13.4
#     公式在 fallback 分支的平凡推论），partial 按字段来源不可得置位。
# ---------------------------------------------------------------------------

def native_session_to_record(item: dict[str, Any]) -> dict[str, Any]:
    """上游 SessionInfo（native HTTP）→ DB 投影记录形状（§13.3 归一化）。

    键 presence 原样保留（三态载体）；仅 dict|None 形状通过对象字段
    （model/summary/tokens/revert），其余形状视为来源不可得（键缺席，
    装配器置 null+partial——不伪装业务 null，§13.2b）。
    """
    record: dict[str, Any] = {}
    for wire, column in (
        ("id", "id"), ("directory", "directory"), ("parentID", "parent_id"),
        ("projectID", "project_id"), ("title", "title"), ("agent", "agent"),
    ):
        if wire in item:
            record[column] = item[wire]

    model = item.get("model")
    if isinstance(model, dict) or ("model" in item and model is None):
        record["model"] = model

    time_obj = item.get("time")
    if isinstance(time_obj, dict):
        # 非 dict / 缺席 → 不落列：created 不可得 → 装配器判 §13.2a（503）
        for wire, column in (("created", "time_created"),
                             ("updated", "time_updated"),
                             ("archived", "time_archived")):
            if wire in time_obj:
                record[column] = time_obj[wire]

    summary = item.get("summary")
    if isinstance(summary, dict):
        # §13.2 summary 对象三子键均须 number：**在场 null 子值 = 畸形
        # 成员**（非业务缺列）→ 不落列（键缺席 = 来源不可得），projector
        # 置 null+partial；与 ``summary: null``（三列在场 None = 业务
        # null，不 partial）保持来源形态区分（§13.2b）。
        for wire, column in (("additions", "summary_additions"),
                             ("deletions", "summary_deletions"),
                             ("files", "summary_files")):
            if wire in summary and summary[wire] is not None:
                record[column] = summary[wire]
    elif "summary" in item and summary is None:
        record["summary_additions"] = None
        record["summary_deletions"] = None
        record["summary_files"] = None

    tokens = item.get("tokens")
    if isinstance(tokens, dict):
        for wire, column in (("input", "tokens_input"),
                             ("output", "tokens_output"),
                             ("reasoning", "tokens_reasoning")):
            if wire in tokens:
                record[column] = tokens[wire]
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            if "read" in cache:
                record["tokens_cache_read"] = cache["read"]
            if "write" in cache:
                record["tokens_cache_write"] = cache["write"]
    elif "tokens" in item and tokens is None:
        for column in ("tokens_input", "tokens_output", "tokens_reasoning",
                       "tokens_cache_read", "tokens_cache_write"):
            record[column] = None

    revert = item.get("revert")
    if isinstance(revert, dict) or ("revert" in item and revert is None):
        record["revert"] = revert
    return record


_CANONICAL_REQUIRED_NON_NULL = (
    "id", "directory", "title", "time_created", "time_updated",
)


def _canonical_number(value: Any) -> bool:
    """§13.2 数值类型冻结：JSON number（int/float，bool 除外）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _canonical_object_field(
    source: dict[str, Any],
    required: dict[str, Any],
    optional: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """§13.2 nullable 对象子字段校验（model/revert 共用）。

    ``required``/``optional``：wire 子键 → Python 类型。required 子键
    缺席或类型错、optional 子键**在场**但 null/类型错 → 返回 None
    （调用方整体 null+partial——禁发含类型违约成员的畸形对象，
    §13.2b ②）。optional 子键**缺席** → 不置键（``variant?``/
    ``partID?`` 允许 absent，不允许在场 null）；合法类型 → 照常。
    """
    projected: dict[str, Any] = {}
    for key, kind in required.items():
        value = source.get(key)
        if not isinstance(value, kind) or isinstance(value, bool):
            return None
        projected[key] = value
    for key, kind in (optional or {}).items():
        if key not in source:
            continue  # absent → 合法，不置键（「absent 不置 null」）
        value = source[key]
        # 在场 null / 类型违约 = 对象成员不符合声明类型 → 对象畸形
        if not isinstance(value, kind) or isinstance(value, bool):
            return None
        projected[key] = value
    return projected


def canonical_session_skeleton_v4(
    record: dict[str, Any], *, fallback: bool = False,
) -> dict[str, Any] | None:
    """DB 投影记录形状 → §13.1 canonical SessionSkeletonV4（唯一 projector）。

    输入契约：``rows_to_records`` 产出的 DB 记录（列恒在场，None = 业务
    null）或 ``native_session_to_record`` 产出的归一化记录（键可缺席 =
    来源不可得）。列表与单查、DB 与 native 四路径全部经本函数装配
    （§13.3 同一 projector 不变量）。None = required 字段不可表示
    （§13.2a：缺席/null/类型约束违约）→ 调用方转整响应 503，不得混入
    items（§13.2c）。

    §13.2 类型/约束冻结（:521-575）：``id``/``directory`` 非空字符串、
    ``title`` 字符串（可空串）、``time.created``/``updated`` 非负数值——
    违约 = 不可表示（None）；nullable 字段类型错（含 nullable 对象子
    字段）→ ``null + partial``（来源不可得同级，§13.2b ②），禁伪装
    业务 null、禁发畸形对象。
    """
    for key in _CANONICAL_REQUIRED_NON_NULL:
        # 缺席或 None 均不可表示（DB NOT NULL 列不可达；native 缺失可达）
        if record.get(key) is None:
            return None
    # §13.2a 类型/约束（required 非 nullable 无三态，只有整响应失败）
    if not isinstance(record["id"], str) or not record["id"]:
        return None
    if not isinstance(record["directory"], str) or not record["directory"]:
        return None  # 全局列表强制非空字符串（真库空目录行 = 不可表示）
    if not isinstance(record["title"], str):
        return None  # 可空串，但不可非字符串
    for key in ("time_created", "time_updated"):
        value = record[key]
        if not _canonical_number(value) or value < 0:
            return None  # 非负数值（字符串/bool/负数均不可表示）

    partial = False

    def nullable(column: str) -> Any:
        # §13.2b 三态：键缺席 → 来源不可得（null+partial）；
        # 在场 None → 业务合法 null（null 不 partial）；在场值 → 值。
        nonlocal partial
        if column not in record:
            partial = True
            return None
        return record[column]

    def nullable_str(column: str) -> str | None:
        # nullable string：类型错 → 来源不可得同级（null+partial）
        nonlocal partial
        value = nullable(column)
        if value is not None and not isinstance(value, str):
            partial = True
            return None
        return value

    def nullable_number(column: str) -> int | float | None:
        # nullable number（tokens 五列）：类型错（bool/字符串）→ null+partial
        nonlocal partial
        value = nullable(column)
        if value is not None and not _canonical_number(value):
            partial = True
            return None
        return value

    project_id = nullable_str("project_id")
    single: dict[str, Any] = {
        "id": record["id"],
        "directory": record["directory"],
        "parentID": nullable_str("parent_id"),
        "projectID": project_id,
    }
    if project_id is not None:
        # §13.5 三不变量：join 成功 + project.id == projectID + worktree
        # 非空串——任一不满足 → project:null + partial（native 归一化记录
        # 恒无 p_id 列 = join 不可用，同走此分支）。
        joined_id = record.get("p_id")
        worktree = record.get("p_worktree")
        if (joined_id is not None and joined_id == project_id
                and isinstance(worktree, str) and worktree):
            project_obj: dict[str, Any] = {
                "id": joined_id, "worktree": worktree,
            }
            name = record.get("p_name")
            if isinstance(name, str):
                project_obj = {"id": joined_id, "name": name,
                               "worktree": worktree}
            single["project"] = project_obj
        else:
            single["project"] = None
            partial = True
    # projectID null → project 缺席（不得补 null：§13.5 两形态不得混同）

    single["title"] = record["title"]
    single["agent"] = nullable_str("agent")

    model = nullable("model")
    if isinstance(model, dict):
        # §13.2 model 子字段：id/providerID 必为 string；variant?: string
        # （absent/None 不置键）——子字段畸形 → 整体 null+partial
        projected_model = _canonical_object_field(
            model, {"id": str, "providerID": str}, {"variant": str},
        )
        if projected_model is None:
            partial = True
            model = None
        else:
            model = projected_model
    elif model is not None:
        # 非法形状（JSON 列合法解析为非对象 / native 非法值）→ 来源不可得
        partial = True
        model = None
    single["model"] = model

    archived = nullable("time_archived")
    if archived is not None and not _canonical_number(archived):
        partial = True  # 畸形时间戳 → 来源不可得同级
        archived = None
    single["time"] = {
        "created": record["time_created"],
        "updated": record["time_updated"],
        "archived": archived,
    }
    additions = nullable_number("summary_additions")
    deletions = nullable_number("summary_deletions")
    files = nullable_number("summary_files")
    if additions is None and deletions is None and files is None:
        # 全 null（业务合法 / 全缺席——缺席列已按 §13.2b ②置 partial）
        single["summary"] = None
    elif additions is not None and deletions is not None and files is not None:
        # §13.2 对象时三子键均为数值——完整数值三元组才发对象
        single["summary"] = {
            "additions": additions, "deletions": deletions, "files": files,
        }
    else:
        # 混合（部分列 NULL / 部分类型错）→ 禁发含 null 子值的畸形对象
        partial = True
        single["summary"] = None
    single["tokens_input"] = nullable_number("tokens_input")
    single["tokens_output"] = nullable_number("tokens_output")
    single["tokens_reasoning"] = nullable_number("tokens_reasoning")
    single["tokens_cache_read"] = nullable_number("tokens_cache_read")
    single["tokens_cache_write"] = nullable_number("tokens_cache_write")

    revert = nullable("revert")
    if isinstance(revert, dict):
        # §13.2 revert 子字段：messageID 必为 string；partID?: string
        projected_revert = _canonical_object_field(
            revert, {"messageID": str}, {"partID": str},
        )
        if projected_revert is None:
            partial = True
            revert = None
        else:
            revert = projected_revert
    elif revert is not None:
        partial = True
        revert = None
    single["revert"] = revert

    single["partial"] = partial
    single["degraded"] = partial or fallback
    return single
