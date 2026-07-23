#!/usr/bin/env python3
"""
scripts/measure_token_overhead.py — Token-stream SSE overhead measurement harness.

Implements the §11 performance methodology from docs/design-token-stream.md:
measure raw upstream delta bytes vs token-stream wire bytes (batched per §5.4)
across >=10 representative generation traces. Validates the ≤1.2x overhead
target after the two user-approved wire levers (see "Approved levers" below).

SELF-CONTAINED: does NOT import the not-yet-built / in-flight token_hub. It
encodes the wire frames per §5.6 spec itself, mirroring sse_frame() from
src/oc_slimapi/sse/hub.py:110  (`event: X\\ndata: {orjson}\\n\\n`).

Approved levers (wire change vs the original v4 design):
  1. DROP terminal full-text. The terminal frame is now a MARKER
     `message.part.snapshot{done:true}` = `{sessionID, messageID, partID,
     done:true}` — NO `text` field, NO full-text retransmission. Authoritative
     final text stays with `/since` (§5.7). This eliminates the structural
     >=2x floor of the original design (where the terminal snapshot re-sent
     the full text on top of the per-token delta stream).
  2. gzip by default for token stream. gzip-compressed wire bytes become the
     PRIMARY `wire_bytes` metric; `wire_bytes_nogzip` is kept only as a
     secondary reference column. (This is the first SSE gzip exception;
     CHANGELOG [0.1.0] previously said "SSE 永不 gzip".)

Modeling assumptions (documented per §11 — these are NOT acceptance criteria,
they parameterize the measurement):
  - TOKENS_PER_SECOND = 30: upstream message.part.delta arrival rate. Used to
    convert delta-count -> wall-clock time so the 100ms flush window
    (§5.4 TOKEN_FLUSH_SECONDS) can be modeled. 30 tok/s is a conservative
    (pessimistic) LLM generation rate; higher rates => larger time-window
    batches => lower overhead. Each delta == one upstream per-token delta event.
  - Subscribe is modeled at text-start (snapshot text == ""); a mid-stream
    subscribe would add one more snapshot carrying accumulated text so far
    (not counted here).
  - finish_part (§5.4 C1): on text-end, drain residual pending -> one delta
    frame -> terminal MARKER snapshot{done:true} (lever 1: NO text field).
  - Truncation (token_stream_max_frame_bytes = 1MiB, §5.8 R1) is NOT exercised:
    no trace approaches 1MiB. The harness measures framing/batching overhead,
    not the large-frame path.
  - gzip: one deflate stream (wbits gzip, level 6) with Z_SYNC_FLUSH after each
    emitted frame, modeling how a real streaming SSE gzip encoder must flush so
    each frame is independently emit-able. Z_FINISH writes the trailer.

Outputs a markdown table + summary verdict to stdout.

Run:  ./scripts/measure_token_overhead.py
"""
from __future__ import annotations

import random
import statistics
import zlib
from dataclasses import dataclass

import orjson

# ───────────────────────── wire frame encoder (mirror hub.py:110 sse_frame) ──
def sse_frame(payload: dict, event: str | None = None) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    return prefix.encode() + b"data: " + orjson.dumps(payload) + b"\n\n"


# ────────────────────────────────────── config knobs per §5.4 / §5.6 / §6 ──
# Realistic opencode ULID-ish ids (~26 chars), so JSON envelope size is honest.
SID = "ses_01JTESTSESSIONID00000001"
MID = "msg_01JTESTMESSAGEID0000001"
PID = "prt_01JTESTPARTID0000000001"

TOKEN_FLUSH_MS: float = 100.0       # §5.4 TOKEN_FLUSH_SECONDS = 0.1
TOKEN_FLUSH_BYTES: int = 4096       # §5.4 TOKEN_FLUSH_BYTES
TOKENS_PER_SECOND: float = 30.0     # §11 modeling assumption
DELTA_INTERVAL_MS: float = 1000.0 / TOKENS_PER_SECOND


# ────────────────────────────────────────────────────────────── results ──
@dataclass
class TraceResult:
    name: str
    n_deltas: int
    raw_bytes: int
    wire_bytes: int           # gzip-compressed (lever 2: gzip default) — PRIMARY
    wire_bytes_nogzip: int    # uncompressed total — secondary reference
    frame_count: int

    @property
    def overhead_x(self) -> float:          # gzip / raw  (PRIMARY, target ≤1.2x)
        return self.wire_bytes / self.raw_bytes

    @property
    def overhead_x_nogzip(self) -> float:   # uncompressed / raw  (reference)
        return self.wire_bytes_nogzip / self.raw_bytes


# ─────────────────────────────────────────── core batching + measurement ──
def gzip_stream(frames: list[bytes]) -> int:
    """Streaming gzip: one deflate stream, Z_SYNC_FLUSH after each frame."""
    co = zlib.compressobj(level=6, wbits=16 + zlib.MAX_WBITS)
    total = 0
    for f in frames:
        total += len(co.compress(f))
        total += len(co.flush(zlib.Z_SYNC_FLUSH))
    total += len(co.flush(zlib.Z_FINISH))
    return total


def measure_trace(name: str, deltas: list[str]) -> TraceResult:
    """Batch deltas per §5.4 (100ms OR 4KiB) and emit wire frames per §5.6."""
    full_text = "".join(deltas)
    raw_bytes = len(full_text.encode("utf-8"))

    frames: list[bytes] = []
    # 1) subscribe anchor: snapshot{done:false} (subscribe at text-start; empty)
    frames.append(sse_frame(
        {"sessionID": SID, "messageID": MID, "partID": PID, "text": "", "done": False},
        event="message.part.snapshot"))

    # 2) batched deltas — accumulate, flush on 100ms window OR 4KiB byte threshold
    pending: list[str] = []
    pending_bytes = 0
    window_start_ms = 0.0
    t_ms = 0.0

    def flush_pending() -> None:
        nonlocal pending, pending_bytes
        if not pending:
            return
        text = "".join(pending)
        frames.append(sse_frame(
            {"sessionID": SID, "messageID": MID, "partID": PID, "text": text},
            event="message.part.delta"))
        pending = []
        pending_bytes = 0

    for d in deltas:
        pending.append(d)
        pending_bytes += len(d.encode("utf-8"))
        t_ms += DELTA_INTERVAL_MS
        if pending_bytes >= TOKEN_FLUSH_BYTES or (t_ms - window_start_ms) >= TOKEN_FLUSH_MS:
            flush_pending()
            window_start_ms = t_ms

    # 3) finish_part (C1): drain residual pending -> delta frame, THEN terminal
    #    MARKER snapshot{done:true} (lever 1: NO text field — no full-text retransmission).
    flush_pending()
    frames.append(sse_frame(
        {"sessionID": SID, "messageID": MID, "partID": PID, "done": True},
        event="message.part.snapshot"))

    wire_bytes_nogzip = sum(len(f) for f in frames)
    wire_bytes = gzip_stream(frames)   # lever 2: gzip is the default → PRIMARY
    return TraceResult(
        name=name, n_deltas=len(deltas), raw_bytes=raw_bytes,
        wire_bytes=wire_bytes, wire_bytes_nogzip=wire_bytes_nogzip,
        frame_count=len(frames),
    )


# ─────────────────────────────────────────────── trace generators (≥10) ──
# Each generator returns list[str] — one element per upstream per-token delta.
# Seeded for reproducibility (output table is deterministic across runs).

_VOCAB_EN = [
    "The", "system", "should", "return", "a", "list", "of", "items", "from",
    "the", "database", "and", "render", "them", "on", "client", "without",
    "extra", "latency", "for", "user", "requests", "that", "arrive", "during",
    "generation", "we", "stream", "tokens", "as", "they", "are", "produced",
]


def gen_short_text_en(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    out: list[str] = []
    for i in range(n):
        w = rng.choice(_VOCAB_EN)
        sep = " " if out and not out[-1].endswith((".", "\n")) else ""
        out.append(sep + w.lower() if sep else w)
    return out


def gen_long_text_prose(n: int, seed: int) -> list[str]:
    """Longer per-delta spans (markdown prose / multi-word chunks)."""
    rng = random.Random(seed)
    chunks = [
        "Furthermore", ", the implementation", " must guarantee", " that under",
        " heavy load", " the response time", " remains bounded", " by the configured",
        " timeout", ".\n\n", "In particular", ", when the upstream", " connection drops",
        " mid-generation", ", the sidecar", " should emit", " a resync frame", " and",
        " tear down", " any in-flight", " accumulators", ". ",
    ]
    return [rng.choice(chunks) for _ in range(n)]


def gen_long_text_code(n: int, seed: int) -> list[str]:
    """Code-ish deltas: indentation, keywords, braces, newlines."""
    rng = random.Random(seed)
    toks = [
        "def", " ", "process", "(", "items", ")", ":", "\n", "    ", "result",
        " ", "=", " ", "[", "]", "\n", "    ", "for", " ", "x", " ", "in", " ",
        "items", ":", "\n", "        ", "if", " ", "x", ".", "valid", ":", "\n",
        "            ", "result", ".", "append", "(", "x", ".", "value", ")", "\n",
        "    ", "return", " ", "result", "\n\n",
    ]
    return [rng.choice(toks) for _ in range(n)]


_CJK_CHARS = "这是中文模型生成的一段话用来测试流式开销包含常用字与标点符号的而已"


def gen_cjk_chinese(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    out: list[str] = []
    for i in range(n):
        k = rng.randint(1, 3)
        sep = "" if rng.random() < 0.15 else ""
        out.append(sep + "".join(rng.choice(_CJK_CHARS) for _ in range(k)))
    return out


def gen_cjk_jp_mix(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    pool = "これはひらがなとカタカナと漢字とasciiを混ぜたテキストですthe quick"
    out: list[str] = []
    for _ in range(n):
        k = rng.randint(1, 2)
        out.append("".join(rng.choice(pool) for _ in range(k)))
    return out


_REASON_TINY = ["hmm", ",", " ", "so", "I", "think", "that", " ", "maybe", "we",
                "should", " ", "try", " ", "first", ".", " ", "ok", " ", "now", "let", "'s"]


def gen_reasoning_dense(n: int, seed: int) -> list[str]:
    """Many tiny deltas (1-3 chars) — worst case for per-frame framing overhead."""
    rng = random.Random(seed)
    return [rng.choice(_REASON_TINY) for _ in range(n)]


def gen_reasoning_mixed(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    phrases = ["let me think", "first we", "need to", "consider", "the constraints",
               " ", ", ", "so ", "therefore ", "I'll ", "proceed ", "with ", "this ", "approach "]
    out: list[str] = []
    for _ in range(n):
        if rng.random() < 0.3:
            out.append(rng.choice(" ,.;:'\""))
        else:
            out.append(rng.choice(phrases))
    return out


def gen_tool_input_json(n: int, seed: int) -> list[str]:
    """JSON-object deltas (keys, braces, string values)."""
    rng = random.Random(seed)
    toks = ['{"', "command", '": "', "run", "--flag", "value", '", ', '"path',
            '": "/tmp/', "file", ".txt", '", ', '"timeout', '": 30, ', '"args',
            '": ["', "a", '", "', "b", '"], ', '"env', '": {"', "KEY", '": "', "v", '"}}', " "]
    return [rng.choice(toks) for _ in range(n)]


def gen_tool_input_command(n: int, seed: int) -> list[str]:
    """Shell command deltas with flags/paths."""
    rng = random.Random(seed)
    toks = ["git", " ", "commit", " ", "-m", " ", '"feat:', " ", "add", " ", "stream",
            " ", "support", '"', " ", "&&", " ", "push", " ", "origin", " ", "main", "\n"]
    return [rng.choice(toks) for _ in range(n)]


def gen_mixed_assistant(n: int, seed: int) -> list[str]:
    """Realistic assistant message: prose + light markdown + occasional newline."""
    rng = random.Random(seed)
    out: list[str] = []
    for i in range(n):
        r = rng.random()
        if r < 0.1:
            out.append("\n\n")
        elif r < 0.2:
            out.append("**" + rng.choice(_VOCAB_EN) + "**")
        elif r < 0.3:
            out.append("`code`")
        else:
            w = rng.choice(_VOCAB_EN)
            sep = " " if out and not out[-1].endswith(("\n\n", " ", ".", "`")) else ""
            out.append(sep + w)
    return out


_EMOJI = ["😀", "🎉", "🚀", "✨", "📝", "👍", "❤️", "🔥", "✅", "➡️"]


def gen_emoji_unicode(n: int, seed: int) -> list[str]:
    """Emoji / surrogate pairs (4 UTF-8 bytes each) + short latin words."""
    rng = random.Random(seed)
    out: list[str] = []
    for _ in range(n):
        if rng.random() < 0.4:
            out.append(rng.choice(_EMOJI))
        else:
            out.append(" " + rng.choice(_VOCAB_EN).lower())
    return out


def build_traces() -> list[tuple[str, list[str]]]:
    """≥10 representative upstream delta traces (covers §11 categories a–e)."""
    return [
        ("short-text-en-100",   gen_short_text_en(100, seed=1)),     # (a) short text
        ("short-text-en-500",   gen_short_text_en(500, seed=2)),     # (a) short text, longer
        ("long-text-prose",     gen_long_text_prose(180, seed=3)),   # (b) long text
        ("long-text-code",      gen_long_text_code(200, seed=4)),    # (b) long text (code)
        ("cjk-chinese-200",     gen_cjk_chinese(200, seed=5)),       # (c) CJK
        ("cjk-jp-mix-150",      gen_cjk_jp_mix(150, seed=6)),        # (c) CJK mix
        ("reasoning-dense-400", gen_reasoning_dense(400, seed=7)),   # (d) reasoning-heavy
        ("reasoning-mixed-300", gen_reasoning_mixed(300, seed=8)),   # (d) reasoning-heavy
        ("tool-input-json",     gen_tool_input_json(120, seed=9)),   # (e) tool-input
        ("tool-input-command",  gen_tool_input_command(150, seed=10)),  # (e) tool-input
        ("mixed-assistant-250", gen_mixed_assistant(250, seed=11)),  # realistic blend
        ("emoji-unicode-100",   gen_emoji_unicode(100, seed=12)),    # unicode stress
    ]


# ──────────────────────────────────────────────────────────── reporting ──
def fmt_x(x: float) -> str:
    return f"{x:.2f}x"


def main() -> int:
    results = [measure_trace(name, deltas) for name, deltas in build_traces()]

    print("# Token-stream SSE overhead — §11 measurement (levers applied)")
    print()
    print("Self-contained harness (`scripts/measure_token_overhead.py`). Mirrors")
    print("`sse_frame` (hub.py:110); batches per §5.4 (100ms OR 4KiB); wire frames")
    print("per §5.6 with the two user-approved levers applied. See module docstring.")
    print()
    print("## Approved levers (wire change vs original v4 design)")
    print()
    print("1. **Terminal MARKER** `snapshot{done:true}` = `{sessionID,messageID,")
    print("   partID,done:true}` — NO `text`, NO full-text retransmission. `/since`")
    print("   stays authoritative for final text (§5.7). Eliminates the structural")
    print("   ≥2x floor of the original design.")
    print("2. **gzip by default** for token stream → `wire_gzip` is the PRIMARY wire")
    print("   metric; `wire_nogzip` is a secondary reference column.")
    print()
    print("## Config")
    print()
    print(f"- `TOKENS_PER_SECOND` = {TOKENS_PER_SECOND:g}  (delta arrival rate → time-window model)")
    print(f"- `TOKEN_FLUSH_MS` = {TOKEN_FLUSH_MS:g}  (§5.4 time window)")
    print(f"- `TOKEN_FLUSH_BYTES` = {TOKEN_FLUSH_BYTES}  (§5.4 byte threshold)")
    print(f"- IDs: `len(SID)={len(SID)}`, `len(MID)={len(MID)}`, `len(PID)={len(PID)}` (realistic ULID-ish)")
    print(f"- gzip: level 6, Z_SYNC_FLUSH per frame (streaming)")
    print(f"- traces: {len(results)} (≥10 required)")
    print()
    print("## Results")
    print()
    print("| trace | raw_bytes | wire_gzip | wire_nogzip | overhead_x_gzip | overhead_x_nogzip | frames |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for r in results:
        print(f"| {r.name} | {r.raw_bytes} | {r.wire_bytes} | {r.wire_bytes_nogzip} | "
              f"{fmt_x(r.overhead_x)} | {fmt_x(r.overhead_x_nogzip)} | {r.frame_count} |")

    oh = [r.overhead_x for r in results]            # gzip (primary)
    oh_ng = [r.overhead_x_nogzip for r in results]  # uncompressed (reference)

    print()
    print("## Summary")
    print()
    print(f"- **overhead_x_gzip**   (PRIMARY, lever 2): min={fmt_x(min(oh))}  "
          f"median={fmt_x(statistics.median(oh))}  mean={fmt_x(statistics.mean(oh))}  "
          f"max={fmt_x(max(oh))}")
    print(f"- **overhead_x_nogzip** (reference): min={fmt_x(min(oh_ng))}  "
          f"median={fmt_x(statistics.median(oh_ng))}  mean={fmt_x(statistics.mean(oh_ng))}  "
          f"max={fmt_x(max(oh_ng))}")

    print()
    print("## Target verdict (≤1.2x median overhead_x_gzip)")
    print()
    med_oh = statistics.median(oh)
    med_oh_ng = statistics.median(oh_ng)
    target = 1.2
    meets = med_oh <= target
    n_meet = sum(1 for x in oh if x <= target)
    print(f"- median **overhead_x_gzip** = {fmt_x(med_oh)} → "
          f"{'MEETS' if meets else 'MISSES'} the ≤1.2x target.")
    print(f"- median overhead_x_nogzip (reference, no lever 2) = {fmt_x(med_oh_ng)}.")
    print(f"- per-trace: {n_meet}/{len(oh)} traces individually ≤1.2x under gzip; "
          f"{sum(1 for x in oh if x <= 1.5)}/{len(oh)} ≤1.5x.")
    print()
    if meets:
        print("**Lever decision VALIDATED by measurement**: dropping the terminal")
        print("full-text retransmission (lever 1) removes the ≥2x structural floor, and")
        print("making gzip the default (lever 2) collapses the repetitive JSON/SSE framing")
        print("so the median compressed wire reaches the ≤1.2x target.")
    else:
        print("**Lever decision NOT yet at target**: the median still exceeds 1.2x.")
        print("Remaining overhead is driven by short, low-redundancy token traces")
        print("(reasoning/code) where per-frame deflate flush overhead is not amortized")
        print("over enough incompressible payload. Tuning levers: larger flush window,")
        print("faster upstream rate, or higher gzip level.")
    print()
    print("### Effect of the levers vs the original v4 design")
    print()
    print("- Lever 1 (terminal marker, no full text): removes the duplicate-text cost")
    print("  that previously forced `overhead_x ≥ ~2.0x` uncompressed. The terminal")
    print("  frame is now a tiny ~80-byte marker regardless of part length.")
    print("- Lever 2 (gzip default): the repeated `event:`/`data:`/JSON-key scaffolding")
    print("  compresses to near-nothing across frames; only the incompressible token")
    print("  text and the per-flush deflate overhead remain.")
    print("- `overhead_x_nogzip` shows what lever 1 alone buys (still well above 1.2x")
    print("  because of the per-frame JSON/SSE envelope at ~3–4 tokens/frame under the")
    print("  100ms × 30 tok/s model); it is reported only to quantify the lever-2 gain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
