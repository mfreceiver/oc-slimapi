"""Environment-only configuration; no database or mutable config files."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit

from .versioning import ACCEPTED_CLIENT_VERSIONS, SERVER_API_VERSION


# ---------------------------------------------------------------------------
# Token-stream budget / timing constants (design-token-stream.md §6).
#
# These are code-level defaults (not production ops knobs) because they are wire-invariant
# tuning knobs: changing them does NOT change the wire contract, only server-
# side batching/memory behaviour. The per-subscriber envelope knobs above
# (token_stream_*) ARE env-overridable for ops break-glass.
#
# Debug/联调-only env overrides (``OC_SLIMAPI_TOKEN_STREAM_DEBUG_*``):
# these three caps also support env override via
# :func:`~oc_slimapi.sse.tokenstream.hub.apply_debug_budget_overrides`,
# which is called once during app lifespan startup. The override is OFF by
# default (env unset = code-level cap unchanged). Intended only for
# development / integration testing where a small data volume must trigger
# memory-limit eviction (MB-P-S1 current-key nodrop path). The env knobs
# remain wire-invariant (they do not change the wire contract, only server-
# side memory pressure thresholds). Production deployments should NOT set
# these env vars.
#
# Stage E (§16-C residual): the memory budget is split 4+4 (Option B).
# ``TOKEN_LIVEPARTS_MAX_BYTES`` bounds the authoritative LivePart text
# (seed + committed deltas); ``TOKEN_PENDING_MAX_BYTES`` bounds the
# transient DeltaAccumulator flush window (unflushed chunks). The same
# delta byte physically occupies BOTH buffers simultaneously (LivePart.chunks
# is the persistent authoritative copy; DeltaAccumulator.chunks is the
# transient pre-flush shadow), so each budget independently protects its
# OWN buffer — no double-count of a single buffer. Total worst-case
# accumulator memory = 4 + 4 = 8 MiB (unchanged from the pre-split merged
# cap, but now each growth mode is bounded independently).
# ---------------------------------------------------------------------------
TOKEN_PART_MAX_BYTES = 1024 * 1024             # 1 MiB — single part accumulation cap
TOKEN_LIVE_PARTS_MAX = 32                      # global active LivePart count cap (C5)
TOKEN_LIVEPARTS_MAX_BYTES = 4 * 1024 * 1024    # 4 MiB — global LivePart byte cap (C5, Stage E split)
TOKEN_PENDING_MAX_BYTES = 4 * 1024 * 1024      # 4 MiB — global pending (unflushed) byte cap (Stage E split)
TOKEN_FLUSH_SECONDS = 0.1                      # 100 ms flush window
TOKEN_FLUSH_BYTES = 4096                       # 4 KiB early-flush threshold per accumulator
TOKEN_ACC_IDLE_MS = 60_000                     # 60 s idle TTL for orphan LivePart retirement
TOKEN_HEARTBEAT_SECONDS = 15                   # keepalive vs stunnel / proxy idle timeout
# Stage B bounded tombstones (§16-B): _disabled_parts / _nontext_parts are
# insertion-ordered bounded maps so a long-running sidecar cannot leak memory
# across many truncated / non-text parts. 4096 entries / 300 s TTL mirrors the
# kind of budget a single opencode process run is ever likely to produce; prune
# is on-insert (oldest first) — see TokenStreamHub._prune_bounded.
TOKEN_DISABLED_MAX = 4096                      # cap on _disabled_parts / _nontext_parts
TOKEN_DISABLED_TTL_S = 300                     # 5 min TTL on tombstone entries
TOKEN_DISABLED_TTL_MS = TOKEN_DISABLED_TTL_S * 1000
# Stage C (NB-B2): bound on _pending_session_resinks queue. A single sid that
# cycles busy→idle (or a flap of session.deleted events) must not let the
# queue grow unbounded across a long-running sidecar; oldest entries are
# dropped when the cap is exceeded (the newest resync reason is the most
# relevant — clients cold-start on any resync regardless).
TOKEN_RESYNC_QUEUE_CAP = 64
# Stage B v0.6 §P.2 (MAJOR 4 方案 C): bounded replay queue for upstream
# ``message.removed`` tombstones — lets a token-stream subscriber that
# attaches AFTER a removal (or reconnects post-upstream-loss) learn about
# it during the handshake. Global FIFO cap + 24h TTL bound memory across
# a long-running sidecar; on-insert prune + ttl_sweep hook keep both
# invariants enforced (see TokenStreamHub._prune_removed_messages /
# ttl_sweep).
TOKEN_REMOVED_MESSAGES_MAX = 1000                # global FIFO cap
TOKEN_REMOVED_MESSAGES_TTL_MS = 24 * 60 * 60 * 1000   # 24 h TTL
# Default per-frame byte ceiling for token-stream frames (design §6). Mirrors
# Settings.token_stream_max_frame_bytes; Stage D wires the env-overridable
# value through TokenStreamHub's constructor. Code-level here so TokenStreamHub
# has a sensible default for tests / unwired state.
DEFAULT_TOKEN_MAX_FRAME_BYTES = 1024 * 1024    # 1 MiB

# CRITICAL 2 (rev-ogpt round-3): handshake buffer caps for the per-subscriber
# _SubscriberQueue handshake deque.
#   * Item cap (2048): MUST accommodate the full §5.5 handshake quantity
#     upper bound — server.connected + tombstone replay batch
#     (TOKEN_REMOVED_MESSAGES_MAX, all matching the sid worst case)
#     + 1 snapshot per active LivePart (TOKEN_LIVE_PARTS_MAX).
#     A static assertion below enforces this.
#   * Byte cap (8 MiB): a fail-safe resource limit, NOT guaranteed to
#     always cover the full §5.5 pre-fill. In extreme scenarios (32 near-
#     1 MiB snapshot frames with JSON escaping amplification) it may be
#     insufficient — overflow triggers a safe 503
#     ``sse_token_handshake_overflow`` (no silent frame loss).
# The previous default (256 items) was DETERMINISTICALLY too small: a sid
# with 256+ removed messages caused drop-oldest to evict server.connected
# (the FIRST frame), leaving the subscriber in an unrecoverable state (no
# connection-establishment frame, no resync marker). Overflow now FAILS
# LOUD (sub.closed → attach bails → 503 retry) rather than silently dropping.
TOKEN_HANDSHAKE_ITEMS = 2048
TOKEN_HANDSHAKE_BUFFER_BYTES = 8 * 1024 * 1024    # 8 MiB

# Guard: the revision cap (_part_revisions, bounded by TOKEN_DISABLED_MAX)
# must never be smaller than the LivePart count cap, otherwise a still-alive
# LivePart's revision can be evicted under FIFO pressure, causing the next
# update to reset it to 0 (revision regression bug). Defaults satisfy this
# easily (32 <= 4096); the assertion catches misconfigured code-level edits.
assert TOKEN_LIVE_PARTS_MAX <= TOKEN_DISABLED_MAX, (
    f"TOKEN_LIVE_PARTS_MAX ({TOKEN_LIVE_PARTS_MAX}) must be <= "
    f"TOKEN_DISABLED_MAX ({TOKEN_DISABLED_MAX}) to prevent revision-cap "
    "eviction of still-living PartKeys"
)
# CRITICAL 2 guard: the handshake item cap must accommodate the FULL §5.5
# pre-fill — server.connected + the entire tombstone replay batch
# (TOKEN_REMOVED_MESSAGES_MAX, all matching the sid worst case) + one
# snapshot per active LivePart (TOKEN_LIVE_PARTS_MAX). A cap smaller than
# this sum deterministically evicts server.connected on a tombstone-heavy
# sid → unrecoverable subscriber state.
assert TOKEN_HANDSHAKE_ITEMS >= (
    TOKEN_REMOVED_MESSAGES_MAX + 1 + TOKEN_LIVE_PARTS_MAX
), (
    f"TOKEN_HANDSHAKE_ITEMS ({TOKEN_HANDSHAKE_ITEMS}) must be >= "
    f"TOKEN_REMOVED_MESSAGES_MAX ({TOKEN_REMOVED_MESSAGES_MAX}) + 1 "
    f"(server.connected) + TOKEN_LIVE_PARTS_MAX ({TOKEN_LIVE_PARTS_MAX}) "
    "= full §5.5 handshake pre-fill ceiling (CRITICAL 2)"
)
assert TOKEN_HANDSHAKE_BUFFER_BYTES > 0, (
    "TOKEN_HANDSHAKE_BUFFER_BYTES must be > 0"
)


def _version_range(value: str) -> tuple[int, int]:
    try:
        minimum, maximum = (int(item.strip()) for item in value.split(",", 1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS must be min,max") from exc
    return minimum, maximum


def _opt_int_env(name: str) -> int | None:
    """Parse an optional int env var for the debug/联调-only token-stream
    budget overrides. Unset / empty / whitespace → ``None`` (treated as
    unset); a present-but-non-integer value raises ``ValueError`` at startup
    (fail-fast, mirroring the other ``int(os.getenv(...))`` knobs)."""
    value = os.getenv(name)
    return int(value) if value and value.strip() else None


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = os.getenv("OC_SLIMAPI_HOST", "127.0.0.1")
    port: int = int(os.getenv("OC_SLIMAPI_PORT", "4097"))
    upstream: str = os.getenv("OC_SLIMAPI_UPSTREAM", "http://127.0.0.1:4096").rstrip("/")
    max_message_bytes: int = int(os.getenv("OC_SLIMAPI_MAX_MESSAGE_BYTES", str(32 * 1024 * 1024)))
    # Transform-pool sizing. Admission is acquired BEFORE the upstream GET so
    # the sidecar cannot OOM by buffering many concurrent large bodies; the
    # worker pool bounds the CPU work that runs off the uvicorn event loop.
    max_transforms: int = int(os.getenv("OC_SLIMAPI_MAX_TRANSFORMS", "1"))
    transform_wait_seconds: float = float(os.getenv("OC_SLIMAPI_TRANSFORM_WAIT_SECONDS", "2"))
    max_response_bytes: int = int(os.getenv("OC_SLIMAPI_MAX_RESPONSE_BYTES", str(64 * 1024 * 1024)))
    smoke_session_id: str | None = os.getenv("OC_SLIMAPI_SMOKE_SESSION_ID")
    server_api_version: int = int(os.getenv("OC_SLIMAPI_SERVER_API_VERSION", str(SERVER_API_VERSION)))
    accepted_client_versions: tuple[int, int] = _version_range(
        os.getenv(
            "OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS",
            f"{ACCEPTED_CLIENT_VERSIONS[0]},{ACCEPTED_CLIENT_VERSIONS[1]}",
        )
    )
    # T3 subscriber / SSE-buffer guards (v1 contract §6). All must be strictly
    # positive; total >= per-directory so the broader cap can never be the
    # binding constraint in a single-hub world without making admission
    # mathematically inconsistent.
    max_subscribers_per_directory: int = int(
        os.getenv("OC_SLIMAPI_MAX_SUBSCRIBERS_PER_DIRECTORY", "8")
    )
    max_total_subscribers: int = int(os.getenv("OC_SLIMAPI_MAX_TOTAL_SUBSCRIBERS", "16"))
    sse_queue_items: int = int(os.getenv("OC_SLIMAPI_SSE_QUEUE_ITEMS", "256"))
    sse_buffer_bytes: int = int(os.getenv("OC_SLIMAPI_SSE_BUFFER_BYTES", str(2 * 1024 * 1024)))
    sse_max_frame_bytes: int = int(os.getenv("OC_SLIMAPI_SSE_MAX_FRAME_BYTES", str(256 * 1024)))
    # Token-stream SSE (design-token-stream.md §6): independent per-session
    # opt-in stream for live text-part deltas. Own ledger — does NOT consume
    # MAX_TOTAL_SUBSCRIBERS. Worst case:
    #   8 × (512KiB queue + 8MiB handshake buffer) + 4MiB live + 4MiB pending
    #   = 76 MiB
    # (MAJOR 2) Handshake buffer (8 MiB) may be insufficient in extreme
    #   scenarios: 32 near-1 MiB snapshot frames (TOKEN_LIVE_PARTS_MAX) with
    #   JSON escaping amplification. In practice 32 near-1 MiB text parts in
    #   a single session is extremely rare — when it happens the handshake
    #   overflow fails safe (503, not silent drop).
    # Module-level budget constants (flush / heartbeat / memory caps) live
    # below this class.
    token_stream_max_subscribers: int = int(
        os.getenv("OC_SLIMAPI_TOKEN_STREAM_MAX_SUBSCRIBERS", "8")
    )
    token_stream_queue_items: int = int(os.getenv("OC_SLIMAPI_TOKEN_STREAM_QUEUE_ITEMS", "64"))
    token_stream_buffer_bytes: int = int(
        os.getenv("OC_SLIMAPI_TOKEN_STREAM_BUFFER_BYTES", str(512 * 1024))
    )
    token_stream_max_frame_bytes: int = int(
        os.getenv("OC_SLIMAPI_TOKEN_STREAM_MAX_FRAME_BYTES", str(1024 * 1024))
    )
    # Debug/联调-only env overrides for token-stream memory budget caps.
    # These are OFF by default (None).  When set, they are applied during app
    # lifespan startup via apply_debug_budget_overrides(), overriding the
    # code-level module globals in hub.py.  Intended only for development /
    # integration testing where low data volume must trigger memory-limit
    # eviction.  Not for production use.
    token_stream_debug_live_budget_bytes: int | None = _opt_int_env(
        "OC_SLIMAPI_TOKEN_STREAM_DEBUG_LIVE_BUDGET_BYTES"
    )
    token_stream_debug_part_max_bytes: int | None = _opt_int_env(
        "OC_SLIMAPI_TOKEN_STREAM_DEBUG_PART_MAX_BYTES"
    )
    token_stream_debug_live_parts_max: int | None = _opt_int_env(
        "OC_SLIMAPI_TOKEN_STREAM_DEBUG_LIVE_PARTS_MAX"
    )
    # Shell/PTY HTTP deny-list (spec §6). Default ON; the path table is
    # code-level in proxy.py (hardcoded from B0 §1.3 route scan of opencode
    # v1.18.3). This toggle exists for ops break-glass only — turning it OFF is
    # NOT a security guarantee (real isolation = stunnel mTLS + network edge).
    shell_deny_list_enabled: bool = os.getenv("OC_SLIMAPI_SHELL_DENY_LIST_ENABLED", "1").lower() in (
        "1", "true", "yes", "on",
    )

    # S-C/S-E deployment revision (env-or-file, best-effort, no validate needed).
    deployment_revision: str | None = os.getenv("OC_SLIMAPI_DEPLOYMENT_REVISION")
    deployment_revision_file: str | None = os.getenv("OC_SLIMAPI_DEPLOYMENT_REVISION_FILE")


    # Full bidirectional byte ledger + structured access log (traffic
    # accounting). Additive observability — does NOT touch the wire contract.
    # The in-memory ledger is the source for the new ``traffic`` block on
    # ``/slimapi/metrics``; the JSON-lines access log captures per-request
    # downstream + upstream bytes for ops forensics. Both default ON.
    traffic_metrics_enabled: bool = os.getenv(
        "OC_SLIMAPI_TRAFFIC_METRICS_ENABLED", "true"
    ).lower() in ("1", "true", "yes", "on")
    access_log_enabled: bool = os.getenv(
        "OC_SLIMAPI_ACCESS_LOG_ENABLED", "true"
    ).lower() in ("1", "true", "yes", "on")
    access_log_path: str = os.getenv("OC_SLIMAPI_ACCESS_LOG_PATH", "logs/access.jsonl")
    access_log_max_bytes: int = int(
        os.getenv("OC_SLIMAPI_ACCESS_LOG_MAX_BYTES", str(10 * 1024 * 1024))
    )
    access_log_backups: int = int(os.getenv("OC_SLIMAPI_ACCESS_LOG_BACKUPS", "5"))

    # Skeleton projection inline caps (thresholded; config.env overridable).
    # Per-field cap: inline iff JSON-byte size <= this. Per-message cap: cumulative
    # inlined bytes across all parts in one message <= this. Defaults: 4 KiB / 16 KiB.
    skeleton_inline_output_max_bytes: int = int(
        os.getenv("OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_BYTES", str(4 * 1024))
    )
    skeleton_inline_output_max_message_bytes: int = int(
        os.getenv("OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_MESSAGE_BYTES", str(16 * 1024))
    )

    def read_deployment_revision(self) -> str | None:
        """Best-effort deployment revision (env or file). Returns None if unset.
        Swallows read errors (non-fatal — health simply omits the field)."""
        # env wins; else file (support CREDENTIALS_DIRECTORY fallback);
        # strip whitespace; no length requirement (it's a deploy label, not a secret).
        value: str | None = self.deployment_revision
        if value:
            return value.strip()
        path = self.deployment_revision_file
        if path is None and os.getenv("CREDENTIALS_DIRECTORY"):
            path = str(Path(os.environ["CREDENTIALS_DIRECTORY"]) / "deployment-revision")
        if not path:
            return None
        try:
            return Path(path).read_text().strip()
        except Exception:
            return None

    def validate(self) -> None:
        # Bind host: loopback is the safe default; ``0.0.0.0`` is allowed as a
        # plaintext direct-entry surface (port 4097) for ops scenarios such as
        # reaching the sidecar over a Tailscale address without terminating
        # stunnel mTLS in front. The :4097 listener is **plaintext** — remote
        # exposure must rely on Tailscale ACL / host firewall for isolation.
        # Arbitrary routable hosts (e.g. 192.168.x.x) remain rejected.
        if self.host not in {"127.0.0.1", "::1", "localhost", "0.0.0.0"}:
            raise RuntimeError(
                "OC_SLIMAPI_HOST must be loopback or 0.0.0.0 "
                "(plaintext direct-entry; protect via Tailscale ACL / firewall)"
            )
        parsed = urlsplit(self.upstream)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("OC_SLIMAPI_UPSTREAM must be fixed loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RuntimeError("OC_SLIMAPI_UPSTREAM must not contain credentials/query/fragment")
        minimum, maximum = self.accepted_client_versions
        if self.server_api_version < 1 or minimum < 1 or minimum > maximum:
            raise RuntimeError("slimapi version configuration is invalid")
        # Transform-pool guards: all three must be strictly positive or the
        # sidecar would either reject every request (max_transforms=0) or
        # never admit a slow one (wait<=0), and a zero/negative byte cap
        # would 413 every response.
        if self.max_transforms < 1:
            raise RuntimeError("OC_SLIMAPI_MAX_TRANSFORMS must be >= 1")
        if self.transform_wait_seconds <= 0:
            raise RuntimeError("OC_SLIMAPI_TRANSFORM_WAIT_SECONDS must be > 0")
        if self.max_response_bytes <= 0:
            raise RuntimeError("OC_SLIMAPI_MAX_RESPONSE_BYTES must be > 0")
        # Skeleton projection inline caps: per-field and per-message.
        if self.skeleton_inline_output_max_bytes <= 0:
            raise RuntimeError(
                "OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_BYTES must be > 0"
            )
        if self.skeleton_inline_output_max_message_bytes <= 0:
            raise RuntimeError(
                "OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_MESSAGE_BYTES must be > 0"
            )
        # Capping at 16 MiB to prevent unintentional OOM (mirrors max_response_bytes
        # but the skeleton thresholds are per-field / per-message, so a looser cap is
        # fine — the outer response still honours Settings.max_response_bytes regardless).
        if self.skeleton_inline_output_max_bytes > 16 * 1024 * 1024:
            raise RuntimeError(
                "OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_BYTES must be <= 16 MiB"
            )
        if self.skeleton_inline_output_max_message_bytes > 16 * 1024 * 1024:
            raise RuntimeError(
                "OC_SLIMAPI_SKELETON_INLINE_OUTPUT_MAX_MESSAGE_BYTES must be <= 16 MiB"
            )
        # T3 subscriber / SSE-buffer guards (contract §6). All positive and
        # the global cap must be at least as large as the per-directory cap,
        # otherwise admission could never admit ``max_per_directory`` clients
        # under a single global hub.
        if self.max_subscribers_per_directory < 1:
            raise RuntimeError("OC_SLIMAPI_MAX_SUBSCRIBERS_PER_DIRECTORY must be >= 1")
        if self.max_total_subscribers < self.max_subscribers_per_directory:
            raise RuntimeError(
                "OC_SLIMAPI_MAX_TOTAL_SUBSCRIBERS must be >= OC_SLIMAPI_MAX_SUBSCRIBERS_PER_DIRECTORY"
            )
        if self.sse_queue_items < 2:
            # Overflow terminal path enqueues resync + STOP after clearing;
            # a queue of size 1 cannot hold both, which would drop STOP and
            # violate the SSE backpressure contract (resync without guaranteed
            # connection close).
            raise RuntimeError(
                "OC_SLIMAPI_SSE_QUEUE_ITEMS must be >= 2 "
                "(overflow terminal path enqueues resync + STOP after clearing; "
                "a queue of size 1 cannot hold both, which would drop STOP and "
                "violate the SSE backpressure contract)"
            )
        if self.sse_buffer_bytes <= 0:
            raise RuntimeError("OC_SLIMAPI_SSE_BUFFER_BYTES must be > 0")
        if self.sse_max_frame_bytes <= 0:
            raise RuntimeError("OC_SLIMAPI_SSE_MAX_FRAME_BYTES must be > 0")

        # Token-stream guards (design-token-stream.md §6). Same shape as the
        # control-plane SSE guards above: queue_items must be >= 2 so the
        # Stage-D overflow terminal path can land resync + STOP after a clear,
        # and the byte / subscriber caps must be strictly positive.
        if self.token_stream_max_subscribers < 1:
            raise RuntimeError("OC_SLIMAPI_TOKEN_STREAM_MAX_SUBSCRIBERS must be >= 1")
        if self.token_stream_queue_items < 2:
            raise RuntimeError(
                "OC_SLIMAPI_TOKEN_STREAM_QUEUE_ITEMS must be >= 2 "
                "(overflow terminal path enqueues resync + STOP after clearing; "
                "a queue of size 1 cannot hold both, which would drop STOP and "
                "violate the token-stream backpressure contract)"
            )
        if self.token_stream_buffer_bytes <= 0:
            raise RuntimeError("OC_SLIMAPI_TOKEN_STREAM_BUFFER_BYTES must be > 0")
        if self.token_stream_max_frame_bytes <= 0:
            raise RuntimeError("OC_SLIMAPI_TOKEN_STREAM_MAX_FRAME_BYTES must be > 0")

        # Debug/联调-only budget overrides: if set, must be strictly positive.
        if self.token_stream_debug_live_budget_bytes is not None and self.token_stream_debug_live_budget_bytes <= 0:
            raise RuntimeError(
                "OC_SLIMAPI_TOKEN_STREAM_DEBUG_LIVE_BUDGET_BYTES must be > 0 when set"
            )
        if self.token_stream_debug_part_max_bytes is not None and self.token_stream_debug_part_max_bytes <= 0:
            raise RuntimeError(
                "OC_SLIMAPI_TOKEN_STREAM_DEBUG_PART_MAX_BYTES must be > 0 when set"
            )
        if self.token_stream_debug_live_parts_max is not None and self.token_stream_debug_live_parts_max <= 0:
            raise RuntimeError(
                "OC_SLIMAPI_TOKEN_STREAM_DEBUG_LIVE_PARTS_MAX must be > 0 when set"
            )
        if (
            self.token_stream_debug_live_parts_max is not None
            and self.token_stream_debug_live_parts_max > TOKEN_DISABLED_MAX
        ):
            raise RuntimeError(
                f"OC_SLIMAPI_TOKEN_STREAM_DEBUG_LIVE_PARTS_MAX "
                f"({self.token_stream_debug_live_parts_max}) must be <= "
                f"TOKEN_DISABLED_MAX ({TOKEN_DISABLED_MAX}) — otherwise "
                "the revision cap can evict a still-living PartKey, "
                "causing revision regression (MINOR 6)"
            )

        # Traffic-accounting guards. The in-memory ledger flag has no scalar
        # bound (it is a bool), but the access-log rotation knobs must be
        # strictly positive so the RotatingFileHandler does not divide-by-zero
        # or refuse to roll over.
        if self.access_log_max_bytes <= 0:
            raise RuntimeError("OC_SLIMAPI_ACCESS_LOG_MAX_BYTES must be > 0")
        if self.access_log_backups < 0:
            raise RuntimeError("OC_SLIMAPI_ACCESS_LOG_BACKUPS must be >= 0")

settings = Settings()
