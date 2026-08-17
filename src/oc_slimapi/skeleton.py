"""Pure v2-contract message/session projection functions."""

from __future__ import annotations

import hashlib
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
TOOL_METADATA_KEYS = {"sessionId", "sessionID", "description", "agent", "diffStats"}
FILE_URL_LIMIT = 8 * 1024
COMPACTION_PART_LIMIT = 64 * 1024

# Expand design v5 §4.1: inline caps for text/reasoning, measured in UTF-8
# encoded bytes. A part whose text exceeds the cap is projected as
# ``text: null`` + an ``expandRefs`` entry — never partially truncated.
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


def _expand_ref(category: str, message_id: str, part_id: str | None, sid: str) -> dict[str, Any]:
    """Build one §5 expandRef entry (frozen schema).

    Message-level href: ``/slimapi/messages/{sid}/expand/{category}/{mid}?v=3``
    Part-level href:    ``/slimapi/messages/{sid}/expand/{category}/{mid}/{partID}?v=3``
    ``directory`` is appended by the client (§5.2).
    """
    base = f"/slimapi/messages/{sid}/expand/{category}/{message_id}"
    href = f"{base}/{part_id}?v=3" if part_id is not None else f"{base}?v=3"
    ref = {"category": category, "messageID": message_id, "href": href}
    if part_id is not None:
        ref["partID"] = part_id
    return ref


def _emit_expand_refs(part: dict[str, Any], refs: list[tuple[str, str]], sid: str | None) -> dict[str, Any]:
    """Attach deduped, deterministic ``expandRefs`` to a part (§5.2).

    ``refs`` entries are ``(category, partID)`` pairs; each (category, partID)
    appears at most once (dedup) and output is sorted by (category, partID).
    Without ``sid`` no href can be built → refs are dropped (the reductions
    themselves still apply). Parts without a ``messageID`` get no refs.
    """
    if not sid or not refs:
        return part
    message_id = part.get("messageID")
    if not message_id:
        return part
    part["expandRefs"] = [
        _expand_ref(category, message_id, part_id, sid)
        for category, part_id in sorted(set(refs))
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


def _compute_diffstats(
    filediff: dict[str, Any] | list[dict[str, Any]] | Any,
) -> dict[str, int] | None:
    """Compute compact ``diffStats = {additions, deletions, files}`` from an
    upstream ``state.metadata.filediff`` value (a single ``Snapshot.FileDiff``
    object or possibly a list thereof for multi-file tools).

    Returns ``None`` when the input is not a recognised shape (no data to
    derive statistics from), so callers can safely skip injection.

    Edge cases:
      * Single filediff dict with missing/non-numeric ``additions`` /
        ``deletions`` → treated as zero.
      * List of filediff dicts → sums per-file additions/deletions across the
        list; ``files`` = list length.
      * ``None`` / non-dict / non-list → ``None`` (no stats).
      * Non-finite numbers (inf, nan) are not expected on the wire (Schema
        ``Schema.Finite`` rejects them upstream), but ``int(val) or 0``
        guards against degenerate values defensively.
    """
    # ── digest 对账标注 ────────────────────────────────────────────────
    # digest 对账（tool 完成→message.updated 映射）：后续 SSE 实测验证项，
    # 本轮不实现。参见 docs/specs/chat-toolcard-investigation.md §B.8
    # ────────────────────────────────────────────────────────────────────
    if isinstance(filediff, list):
        if not filediff:
            return None
        total_additions = 0
        total_deletions = 0
        for item in filediff:
            if isinstance(item, dict):
                total_additions += int(item.get("additions", 0) or 0)
                total_deletions += int(item.get("deletions", 0) or 0)
        return {
            "additions": total_additions,
            "deletions": total_deletions,
            "files": len(filediff),
        }
    if isinstance(filediff, dict):
        additions = int(filediff.get("additions", 0) or 0)
        deletions = int(filediff.get("deletions", 0) or 0)
        return {
            "additions": additions,
            "deletions": deletions,
            "files": 1,
        }
    return None


def _compute_diffstats_from_files(files: list[dict[str, Any]] | Any) -> dict[str, int] | None:
    """Compute ``diffStats = {additions, deletions, files}`` from a ``files[]``
    array (as used by patch parts and multi-file apply_patch). Each file item
    must be a dict with ``additions`` / ``deletions`` (schema ``NonNegativeInt``).

    Returns ``None`` when input is not a non-empty list of dicts.
    """
    if not isinstance(files, list) or not files:
        return None
    total_additions = 0
    total_deletions = 0
    for item in files:
        if isinstance(item, dict):
            total_additions += int(item.get("additions", 0) or 0)
            total_deletions += int(item.get("deletions", 0) or 0)
    return {
        "additions": total_additions,
        "deletions": total_deletions,
        "files": len(files),
    }


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


def _tool(part: dict[str, Any], *, budget: dict[str, int] | None = None, limits: SkeletonLimits = DEFAULT_SKELETON_LIMITS, sid: str | None = None) -> dict[str, Any]:
    result = _pick(part, TOOL_KEYS)
    omitted: list[str] = []
    refs: list[tuple[str, str]] = []
    part_id = part.get("id")
    state = part.get("state")
    if isinstance(state, dict):
        thin_state = _pick(state, {"status", "title", "time"})
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
            thin_metadata = _pick(source_metadata, TOOL_METADATA_KEYS)
            if thin_metadata:
                thin_state["metadata"] = thin_metadata
            omitted.extend(
                f"state.metadata.{key}"
                for key in source_metadata if key not in TOOL_METADATA_KEYS
            )
            if part_id and any(key not in TOOL_METADATA_KEYS for key in source_metadata):
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
        # Inject compact diffStats from upstream filediff (computed, injected
        # AFTER thresholding so it is never elligible for omission — the ~50 B
        # object is well below the per-field cap, and sits in TOOL_METADATA_KEYS
        # so it survives the whitelist). digest 对账（tool 完成→message.updated
        # 映射）为后续 SSE 实测验证项，本轮不实现.
        #
        # NOTE: ``thin_metadata`` from ``_pick`` above is a local var (empty
        # disconnected dict when no whitelist keys matched). We must write
        # to ``thin_state["metadata"]`` explicitly to ensure the key exists.
        if isinstance(source_metadata, dict):
            source_filediff = source_metadata.get("filediff")
            if source_filediff is not None:
                diffStats = _compute_diffstats(source_filediff)
                if diffStats is not None:
                    if "metadata" not in thin_state:
                        thin_state["metadata"] = {}
                    thin_state["metadata"]["diffStats"] = diffStats
        result["state"] = thin_state
    for key in part:
        if key not in TOOL_KEYS and key != "state":
            omitted.append(key)
    return _emit_expand_refs(_mark(result, omitted), refs, sid)


def _patch(part: dict[str, Any], *, budget: dict[str, int] | None = None, limits: SkeletonLimits = DEFAULT_SKELETON_LIMITS, sid: str | None = None) -> dict[str, Any]:
    # §4.2 (P0): v1.18.16 PatchPart = {type, hash, files: string[]}. ``hash``
    # was previously dropped into omitted — now preserved. ``files`` a plain
    # string[] carries no per-file stat projection value → kept verbatim.
    # Legacy dict-item files (pre-v1.18 shape) keep the old per-file pick.
    result = _pick(part, PART_IDS | {"hash"})
    omitted: list[str] = []
    files = part.get("files")
    if isinstance(files, list):
        if files and all(isinstance(item, str) for item in files):
            result["files"] = deepcopy(files)
        else:
            result["files"] = [
                _pick(item, {"path", "additions", "deletions", "status"})
                for item in files if isinstance(item, dict)
            ]
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
    # chain-break.
    if isinstance(files, list):
        diffStats = _compute_diffstats_from_files(files)
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


def _file(part: dict[str, Any], *, sid: str | None = None) -> dict[str, Any]:
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
    return _emit_expand_refs(_mark(result, omitted), refs, sid)


def skeleton_part(part: dict[str, Any], *, budget: dict[str, int] | None = None, limits: SkeletonLimits = DEFAULT_SKELETON_LIMITS, sid: str | None = None) -> dict[str, Any]:
    part_type = part.get("type")
    if part_type == "text":
        copied = deepcopy(part)
        if _utf8_bytes_exceeds(copied.get("text"), TEXT_INLINE_MAX_BYTES):
            # §4.1: whole-field omission — text becomes null, never truncated.
            copied["text"] = None
            return _emit_expand_refs(
                _mark(copied, ["text"]), [("part_text", copied.get("id"))], sid
            )
        return copied
    if part_type == "reasoning":
        result = _pick(part, PART_IDS | {"text"})
        omitted = [key for key in part if key not in PART_IDS | {"text"}]
        refs: list[tuple[str, str]] = []
        if _utf8_bytes_exceeds(part.get("text"), REASONING_INLINE_MAX_BYTES):
            result["text"] = None
            omitted.append("text")
            refs.append(("part_reasoning", result.get("id")))
        # reasoning metadata/time omissions are /full-only (§2.3) — no refs.
        return _emit_expand_refs(_mark(result, omitted), refs, sid)
    if part_type == "tool":
        return _tool(part, budget=budget, limits=limits, sid=sid)
    if part_type == "patch":
        return _patch(part, budget=budget, limits=limits, sid=sid)
    if part_type == "file":
        return _file(part, sid=sid)
    if part_type in {"step-start", "step-finish"}:
        result = _mark(_pick(part, PART_IDS), [key for key in part if key not in PART_IDS])
        refs: list[tuple[str, str]] = []
        # §5.3: snapshot omission maps to part_snapshot — only when the
        # snapshot existed non-null/non-empty at omission time. reason/cost/
        # tokens are /full-only (§2.3).
        if part.get("snapshot") not in (None, "") and result.get("id"):
            refs.append(("part_snapshot", result["id"]))
        return _emit_expand_refs(result, refs, sid)
    if part_type == "compaction":
        copied = deepcopy(part)
        # Compaction is retained unless the single part violates its explicit cap.
        if len(orjson.dumps(copied)) <= COMPACTION_PART_LIMIT:
            return copied
        # omitted ["*"] → /full-only (§2.3), no expandRefs.
        return _mark(_pick(part, PART_IDS), ["*"])
    return _mark(_pick(part, PART_IDS), [key for key in part if key not in PART_IDS] or ["*"])


def skeleton_message(
    message: dict[str, Any], *,
    limits: SkeletonLimits = DEFAULT_SKELETON_LIMITS,
    fingerprint: bool = False,
    sid: str | None = None,
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
    message_id = result["info"].get("id", "unknown")
    # §4.1: ``info.summary.diffs`` is ALWAYS projected as ``null`` (unconditional
    # reduction); summary siblings are preserved. §5.2/§5.3: a message-level
    # ``info_summary_diffs`` ref is generated iff diffs was non-null/non-empty
    # at omission time (and a sid is available to build the href).
    summary = result["info"].get("summary")
    if isinstance(summary, dict) and "diffs" in summary:
        orig_diffs = summary["diffs"]
        summary["diffs"] = None
        if sid and orig_diffs not in (None, [], {}, ""):
            result["info"]["expandRefs"] = [
                _expand_ref("info_summary_diffs", message_id, None, sid)
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
        skeleton_part(part, budget=budget, limits=limits, sid=sid)
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
) -> list[dict[str, Any]]:
    return [
        skeleton_message(message, limits=limits, fingerprint=fingerprint, sid=sid)
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
