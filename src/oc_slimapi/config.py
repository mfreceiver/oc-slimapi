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
# Default per-frame byte ceiling for token-stream frames (design §6). Mirrors
# Settings.token_stream_max_frame_bytes; Stage D wires the env-overridable
# value through TokenStreamHub's constructor. Code-level here so TokenStreamHub
# has a sensible default for tests / unwired state.
DEFAULT_TOKEN_MAX_FRAME_BYTES = 1024 * 1024    # 1 MiB


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
    route_secret: str | None = os.getenv("OC_SLIMAPI_ROUTE_SECRET")
    route_secret_file: str | None = os.getenv("OC_SLIMAPI_ROUTE_SECRET_FILE")
    smoke_session_id: str | None = os.getenv("OC_SLIMAPI_SMOKE_SESSION_ID")
    server_api_version: int = int(os.getenv("OC_SLIMAPI_SERVER_API_VERSION", str(SERVER_API_VERSION)))
    accepted_client_versions: tuple[int, int] = _version_range(
        os.getenv(
            "OC_SLIMAPI_ACCEPTED_CLIENT_VERSIONS",
            f"{ACCEPTED_CLIENT_VERSIONS[0]},{ACCEPTED_CLIENT_VERSIONS[1]}",
        )
    )
    max_since_pages: int = 5
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
    # MAX_TOTAL_SUBSCRIBERS. Worst case 8 × 512KiB queues + 8MiB accumulators
    # (4 live + 4 pending, Stage E split) = 12MiB (contract §6 addendum).
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

    # Opt-A partial-envelope (v0.3.1, wire-additive, no X-Slimapi-Version bump).
    # Feature-flagged so ops can force legacy semantics even when clients send the
    # capability header. Rollback thresholds drive an in-memory 1h rolling window
    # in BatchLedger (see observability.py); trips ONLY when sample >= min_sample.
    opt_a_partial_envelope_enabled: bool = os.getenv("OC_SLIMAPI_OPT_A_PARTIAL_ENVELOPE_ENABLED", "1").lower() in (
        "1", "true", "yes", "on",
    )
    opt_a_auto_rollback_enabled: bool = os.getenv("OC_SLIMAPI_OPT_A_AUTO_ROLLBACK_ENABLED", "1").lower() in (
        "1", "true", "yes", "on",
    )
    opt_a_rollback_window_seconds: int = int(os.getenv("OC_SLIMAPI_OPT_A_ROLLBACK_WINDOW_SECONDS", "3600"))
    opt_a_rollback_min_sample: int = int(os.getenv("OC_SLIMAPI_OPT_A_ROLLBACK_MIN_SAMPLE", "100"))
    opt_a_rollback_envelope_5xx_zero_baseline_rate: float = float(
        os.getenv("OC_SLIMAPI_OPT_A_ROLLBACK_ENVELOPE_5XX_ZERO_BASELINE_RATE", "0.01")
    )
    opt_a_rollback_unknown_code_rate: float = float(
        os.getenv("OC_SLIMAPI_OPT_A_ROLLBACK_UNKNOWN_CODE_RATE", "0.05")
    )
    # S-C/S-E deployment revision (env-or-file, best-effort, no validate needed).
    deployment_revision: str | None = os.getenv("OC_SLIMAPI_DEPLOYMENT_REVISION")
    deployment_revision_file: str | None = os.getenv("OC_SLIMAPI_DEPLOYMENT_REVISION_FILE")

    # S-B Retry-After. Conservative minimum for network errors with no upstream
    # guidance; hard cap for any passthrough/derived per-mid retryAfterMs value.
    opt_a_retry_after_ms_conservative: int = int(
        os.getenv("OC_SLIMAPI_OPT_A_RETRY_AFTER_MS_CONSERVATIVE", "200")
    )
    opt_a_retry_after_ms_cap: int = int(
        os.getenv("OC_SLIMAPI_OPT_A_RETRY_AFTER_MS_CAP", "10000")
    )

    def read_deployment_revision(self) -> str | None:
        """Best-effort deployment revision (env or file). Returns None if unset.
        Swallows read errors (non-fatal — health simply omits the field)."""
        # env wins; else file (support CREDENTIALS_DIRECTORY fallback like route_secret);
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

        # Opt-A rollback / retry-after guards (v0.3.1, additive).
        if self.opt_a_rollback_window_seconds < 1:
            raise RuntimeError("OC_SLIMAPI_OPT_A_ROLLBACK_WINDOW_SECONDS must be >= 1")
        if self.opt_a_rollback_min_sample < 1:
            raise RuntimeError("OC_SLIMAPI_OPT_A_ROLLBACK_MIN_SAMPLE must be >= 1")
        if not 0.0 <= self.opt_a_rollback_envelope_5xx_zero_baseline_rate <= 1.0:
            raise RuntimeError(
                "OC_SLIMAPI_OPT_A_ROLLBACK_ENVELOPE_5XX_ZERO_BASELINE_RATE "
                "must be between 0.0 and 1.0"
            )
        if not 0.0 <= self.opt_a_rollback_unknown_code_rate <= 1.0:
            raise RuntimeError(
                "OC_SLIMAPI_OPT_A_ROLLBACK_UNKNOWN_CODE_RATE "
                "must be between 0.0 and 1.0"
            )
        if not 1 <= self.opt_a_retry_after_ms_cap <= 10000:
            raise RuntimeError(
                "OC_SLIMAPI_OPT_A_RETRY_AFTER_MS_CAP must be between 1 and 10000"
            )
        if not 0 <= self.opt_a_retry_after_ms_conservative <= self.opt_a_retry_after_ms_cap:
            raise RuntimeError(
                "OC_SLIMAPI_OPT_A_RETRY_AFTER_MS_CONSERVATIVE must be between 0 "
                "and OC_SLIMAPI_OPT_A_RETRY_AFTER_MS_CAP"
            )

    def read_route_secret(self) -> bytes:
        if self.route_secret:
            value = self.route_secret.encode()
        else:
            path = self.route_secret_file
            if path is None and os.getenv("CREDENTIALS_DIRECTORY"):
                path = str(Path(os.environ["CREDENTIALS_DIRECTORY"]) / "route-secret")
            if not path:
                raise RuntimeError("route secret is required (env or systemd LoadCredential)")
            value = Path(path).read_bytes().strip()
        if len(value) < 32:
            raise RuntimeError("route secret must contain at least 32 bytes")
        return value


settings = Settings()
