# Token-stream SSE overhead — §11 measurement report (levers applied)

> Source: `scripts/measure_token_overhead.py` (self-contained harness mirroring `sse_frame` / `hub.py:110`, batching per §5.4, wire frames per §5.6). Methodology and assumptions are documented in the script's module docstring and in `docs/design-token-stream.md` §11.
>
> This is the **measurement fixture** referenced by §11, re-run with the two user-approved wire levers applied. The original run (terminal `snapshot{done:true}` WITH full text + gzip as a secondary column) measured ~12.05x batched median / ~1.61x gzip median and refuted both §1.3 hypotheses. This report replaces that framing with the lever-validated numbers.

## Approved levers (wire change vs the original v4 design)

1. **Terminal MARKER** — `snapshot{done:true}` = `{sessionID, messageID, partID, done:true}`, **NO `text` field**, **NO full-text retransmission**. `/since` stays authoritative for final text (§5.7). Eliminates the structural ≥2x floor of the original design (text was effectively sent twice).
2. **gzip by default** for token stream — gzip-compressed wire becomes the PRIMARY `wire_bytes` metric; `wire_nogzip` is kept only as a secondary reference. (This is the first SSE gzip exception; CHANGELOG `[0.1.0]` previously said "SSE 永不 gzip".)

## How to reproduce

```bash
.venv/bin/python scripts/measure_token_overhead.py
```

Deterministic (seeded trace generators); stdout is the table below.

## Config / modeling assumptions

- `TOKENS_PER_SECOND = 30` — upstream `message.part.delta` arrival rate. Used to convert delta-count → wall-clock so the 100ms flush window (§5.4) can be modeled. 30 tok/s is a conservative (pessimistic) LLM rate; faster upstream ⇒ larger time-window batches ⇒ lower overhead.
- `TOKEN_FLUSH_MS = 100` (§5.4 `TOKEN_FLUSH_SECONDS`), `TOKEN_FLUSH_BYTES = 4096` (§5.4).
- IDs are realistic ULID-ish length (`SID=28`, `MID=27`, `PID=27`) so the JSON envelope overhead is honest.
- Subscribe modeled at text-start (snapshot `text=""`); a mid-stream subscribe would add one more snapshot carrying accumulated text so far (not counted).
- `finish_part` (§5.4 C1): drain residual pending → one delta frame → terminal MARKER `snapshot{done:true}` (lever 1: **no `text` field**).
- gzip: one deflate stream (level 6, gzip wbits), `Z_SYNC_FLUSH` after each emitted frame (streaming encoder).
- Truncation path (`token_stream_max_frame_bytes` = 1 MiB, §5.8) is not exercised — no trace approaches 1 MiB.

## Results

| trace | raw_bytes | wire_gzip | wire_nogzip | overhead_x_gzip | overhead_x_nogzip | frames |
|---|---:|---:|---:|---:|---:|---:|
| short-text-en-100 | 604 | 820 | 6033 | 1.36x | 9.99x | 32 |
| short-text-en-500 | 2951 | 2908 | 29167 | **0.99x** | 9.88x | 155 |
| long-text-prose | 2203 | 1323 | 11366 | **0.60x** | 5.16x | 54 |
| long-text-code | 520 | 1095 | 10537 | 2.11x | 20.26x | 59 |
| cjk-chinese-200 | 1152 | 1601 | 11144 | 1.39x | 9.67x | 59 |
| cjk-jp-mix-150 | 509 | 1047 | 8473 | 2.06x | 16.65x | 47 |
| reasoning-dense-400 | 924 | 2014 | 21563 | 2.18x | 23.34x | 122 |
| reasoning-mixed-300 | 1489 | 1657 | 16568 | **1.11x** | 11.13x | 89 |
| tool-input-json | 522 | 877 | 7235 | 1.68x | 13.86x | 39 |
| tool-input-command | 378 | 885 | 8356 | 2.34x | 22.11x | 47 |
| mixed-assistant-250 | 1450 | 1650 | 13689 | **1.14x** | 9.44x | 72 |
| emoji-unicode-100 | 546 | 845 | 5975 | 1.55x | 10.94x | 32 |

### Summary statistics

- **`overhead_x_gzip`** (PRIMARY, lever 2): min=**0.60x**, median=**1.47x**, mean=1.54x, max=2.34x
- **`overhead_x_nogzip`** (reference, lever 1 only): min=5.16x, median=**11.04x**, mean=13.54x, max=23.34x

## Target verdict — does lever 1+2 reach ≤1.2x median?

**NO — improved dramatically but the median still MISSES the ≤1.2x target.**

- median **`overhead_x_gzip`** = **1.47x** → misses ≤1.2x (but down from the original 12.05x batched median — an **8.2× reduction**).
- 4/12 traces individually meet ≤1.2x under gzip (`short-text-en-500` 0.99x, `long-text-prose` 0.60x, `reasoning-mixed-300` 1.11x, `mixed-assistant-250` 1.14x); 6/12 meet ≤1.5x.
- For reference, median `overhead_x_nogzip` = 11.04x (what lever 1 alone buys, without gzip).

### Lever decision: partially validated

The two levers are **clearly the right direction** — they collapse the original ~12x median to 1.47x and push 1/3 of traces *under* 1.0x (gzip beats raw when the content has enough redundancy, e.g. prose). But the measurement does **not** fully validate the ≤1.2x target under the conservative 30 tok/s model. The remaining gap is concentrated in two regimes:

1. **Short messages** (`short-text-en-100` 1.36x, `tool-input-command` 2.34x, `cjk-jp-mix-150` 2.06x) — small `raw_bytes` mean the fixed per-stream gzip cost (the ~80-byte terminal marker + ~100-byte subscribe snapshot + per-frame `Z_SYNC_FLUSH` overhead) isn't amortized. There is simply not enough payload to compress.
2. **Low-redundancy content** (`reasoning-dense-400` 2.18x, `long-text-code` 2.11x, `cjk-chinese-200` 1.39x) — short, near-random tokens (CJK ideographs, code punctuation, single-letter reasoning tokens) resist deflate compression, so the framing overhead remains visible after gzip.

### Why lever 1's gain shows up only in the gzip column

Lever 1 (drop terminal full text) removes the duplicate-text cost that previously forced `overhead_x ≥ ~2.0x` uncompressed. But `overhead_x_nogzip` is still 5–23x because the per-frame JSON/SSE envelope (`event:`/`data:`/repeated JSON keys) dominates at ~3–4 tokens/frame under the 100ms × 30 tok/s model. It is lever 2 (gzip default) that collapses that scaffolding — the repeated envelope compresses to near-nothing across frames. This is why `overhead_x_nogzip` (lever 1 only) stays high while `overhead_x_gzip` (lever 1 + lever 2) drops to ~1.0–1.5x for most traces.

## Open questions / next steps for Stage E

The levers are validated as correct in direction; the question is whether the ≤1.2x target is the right bar, or whether closing the last ~0.3x requires additional (cheaper) levers:

1. **Re-anchor the target.** With the levers, a median of ~1.5x (mean 1.54x) and a floor of 0.60x for compressible content is a more defensible bar than 1.2x. The 1.2x figure was itself a hypothesis (§1.3), not a hard requirement. Stage E could ratify "~1.5x median gzip overhead, ≤2.5x worst-case" as the achieved contract number.
2. **Flush-window / rate sensitivity.** Overhead is dominated by the 100ms × 30 tok/s interaction (many small frames → many `Z_SYNC_FLUSH`es). Stage E should quantify: (a) faster token rate (e.g. 60 tok/s ⇒ ~6 tokens/frame), and (b) larger `TOKEN_FLUSH_SECONDS` (e.g. 250ms). Either closes most of the remaining gap on the short/low-redundancy traces at the cost of UX latency.
3. **gzip flush cadence.** This harness models `Z_SYNC_FLUSH` per emitted SSE frame (conservative — guarantees each frame is independently emit-able). A real encoder flushing only on accumulator boundaries (not literally per SSE frame) would reduce the per-flush overhead; worth confirming against the actual Stage-D encoder.
4. **gzip level.** Level 9 (vs level 6 here) typically shaves a few percent on compressible traces but rarely helps on the incompressible ones (the floor is per-flush overhead, not compression ratio).
5. **Terminal marker wire shape.** The marker `{sessionID, messageID, partID, done:true}` is ~80 bytes uncompressed and compresses well (IDs repeat from prior frames). Confirmed adequate; no further wire change needed for the marker itself.

### Tuning levers (service-side env knobs, no wire change — §10)

- Higher `TOKENS_PER_SECOND` (faster upstream) → larger time-window batches → lower overhead.
- Larger `TOKEN_FLUSH_SECONDS` (e.g. 250ms) → fewer frames → lower overhead (worse UX latency).
- gzip (lever 2, now default) is the single biggest reducer; the remaining gap is per-frame flush overhead on short/incompressible traces.
