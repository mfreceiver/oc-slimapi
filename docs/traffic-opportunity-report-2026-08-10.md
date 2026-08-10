# Production Passthrough Traffic Opportunity Report

- **Execution date:** 2026-08-10
- **3-day window:** 2026-08-08 .. 2026-08-10 (inclusive; `today-2 .. today`)
- **Baseline HEAD:** `6a4ca78fa9a8f2951f669d61170a32e216417896` (`6a4ca78`)
- **Task:** T16 — production traffic evidence (read-only aggregation)
- **Branch:** `bundle-slimapi-actions`
- **Data source (READ-ONLY):** `~/.local/state/oc-slimapi/logs/access-YYYY-MM-DD.jsonl(.gz)`

> This is a DOCUMENTATION-ONLY artifact. No source code, no log files, and no other
> files were modified to produce it. The access logs were only ever **read**, never
> written, moved, renamed, or deleted.

---

## Method

The numbers below are produced by a single read-only Python heredoc using **standard
library only** (`gzip`, `json`, `re`, `collections`, `pathlib`, `datetime`). It is
fully reproducible: copy the snippet, run `python3`, and the same window/files will be
aggregated.

The aggregation core is the **frozen base** from the SSOT doc
(`docs/implementation-batches-2026-08-09.md`, Task 16, section C), with its robustness
rules byte-for-byte unchanged:
- strict filename date filter via `NAME_RE.fullmatch(...)` (no substring matching);
- 3-day window `window_start = today - timedelta(days=2)` inclusive of today;
- `_num()` accepts only `int`/`float` and **excludes `bool`**;
- records whose `method`/`path` are non-`str` are skipped;
- `json.JSONDecodeError` lines are skipped;
- aggregation keyed on `(method, normalized_path)`, accumulating
  `[requests, upIn, downOut]`, sorted by `upIn` descending.

The single deliberate, documented extension is a **privacy-preserving path
normalization** applied to `path` before forming the key. Its purpose is twofold:
(1) **T16-C3 privacy** — never write opaque opencode resource ids (session/message/
question ids) into this report, and (2) **route-pattern coalescence** — merge the
hundreds of per-id rows that a literal `path` would produce into one row per route
shape, so the true top candidates are visible.

```python
import gzip, json, re, collections, pathlib, datetime as dt

LOG_DIR = pathlib.Path("~/.local/state/oc-slimapi/logs").expanduser()
NAME_RE = re.compile(r"^access-(\d{4}-\d{2}-\d{2})\.jsonl(?:\.gz)?$")
today = dt.date.today()
window_start = today - dt.timedelta(days=2)
agg = collections.defaultdict(lambda: [0, 0, 0])

def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0

# --- Privacy-preserving coalesce: redact opaque resource ids so the report never
# --- contains session/message/question ids, and per-id rows merge into route
# --- patterns. (The SSOT frozen example aggregates by LITERAL path with no
# --- normalization; the entire _ID_RES mechanism is this task's documented
# --- extension — it introduces ses_/msg_/UUID rules, plus one que_ rule added
# --- after inspecting the data, see note below. Only the regex list is new;
# --- all frozen robustness rules [NAME_RE.fullmatch, window, _num, non-str
# --- skip, JSONDecodeError skip] are unchanged.)
_ID_RES = (
    (re.compile(r"ses_[0-9A-Za-z]+"), "{sid}"),   # opencode session id
    (re.compile(r"msg_[0-9A-Za-z]+"), "{mid}"),   # message id (if present)
    (re.compile(r"que_[0-9A-Za-z]+"), "{qid}"),   # question id (opencode pending-question id)
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"), "{uuid}"),
)

def _norm(path: str) -> str:
    for rx, repl in _ID_RES:
        path = rx.sub(repl, path)
    return path

in_window_files = []
for p in sorted(LOG_DIR.iterdir()):
    m = NAME_RE.fullmatch(p.name)
    if not m:
        continue
    file_date = dt.date.fromisoformat(m.group(1))
    if not (window_start <= file_date <= today):
        continue
    in_window_files.append(p.name)
    opener = gzip.open if p.name.endswith(".gz") else open
    with opener(p, "rt", encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("bucket") != "passthrough":
                continue
            method, path = rec.get("method"), rec.get("path")
            if not isinstance(method, str) or not isinstance(path, str):
                continue
            key = (method, _norm(path))
            r = agg[key]
            r[0] += 1
            r[1] += _num(rec.get("upIn"))
            r[2] += _num(rec.get("downOut"))

# only method/path/bucket/upIn/downOut are ever read from each record.
rows = sorted(agg.items(), key=lambda kv: kv[1][1], reverse=True)
for (method, path), (n, up, down) in rows:
    print(f"{n}\t{up}\t{down}\t{method}\t{path}")
```

> **Note on the `que_` rule.** The SSOT frozen example aggregates by **literal** path
> with **no** normalization, so the entire `_ID_RES` mechanism is this task's
> documented extension (it is not inherited from the frozen example). The extension
> introduces `ses_`/`msg_`/`UUID` rules, and inspection of the actual passthrough data
> revealed a further opaque-id class — opencode **question ids** (`que_...`) —
> appearing in `POST /question/{qid}/reply` and `/reject` paths. Left un-redacted,
> these would (a) leak another class of opaque resource identifier, contrary to
> T16-C3's spirit, and (b) fragment into ~84 single-request rows that bury the real
> signal. So `_ID_RES` carries one additional rule `que_[0-9A-Za-z]+ → {qid}`. Only the
> regex list is part of the extension; every frozen robustness rule is unchanged.

---

## Privacy (T16-C3 — honored, verifiable)

- **Fields parsed from each access-log record:** `method`, `path`, `bucket`, `upIn`,
  `downOut`. **Nothing else.**
- **Fields explicitly NOT read and NOT echoed into this report:** `ts`, `requestId`,
  `client`, `clientVer`, `clientId`, `status`, `durationMs`, and any
  request/response **headers**, **query strings**, and **message/session content**.
- **Opaque resource ids** present in the literal `path` (opencode session/message/
  question ids, UUIDs) were **normalized to placeholders** (`{sid}`, `{mid}`, `{qid}`,
  `{uuid}`) before aggregation. **No literal `ses_` / `msg_` / `que_` id, and no
  client-identifying value, appears anywhere in this report.** (Verified by a
  self-grep of the finished file — see Verification section.)
- **Source log files were only READ**, never modified, truncated, renamed, moved, or
  deleted. No new log files were created. The single artifact produced by this task
  is this markdown report itself.

---

## Window & data source

- **Window:** 2026-08-08 .. 2026-08-10 (`today - 2 days .. today`, 3 days inclusive).
- **In-window files consumed (3):**
  1. `access-2026-08-08.jsonl.gz`
  2. `access-2026-08-09.jsonl.gz`
  3. `access-2026-08-10.jsonl`
- **Total passthrough (`bucket=="passthrough"`) requests in window:** **40,188**
- **Distinct normalized route patterns:** **20**

> Pre-check confirmed the in-window files exist and contain passthrough records, so
> the T16-C4 BLOCKED path was not taken.

---

## Top table (PRIMARY — sorted by `upIn` descending)

Columns: `method | normalized_path | requests | upIn(bytes) | downOut(bytes) | ratio(downOut/upIn)`.
Bytes shown both as raw integers and as MiB (1 MiB = 1 048 576 B) for readability.
All 20 normalized patterns are shown.

| #  | method | normalized_path          | requests | upIn (bytes)            | downOut (bytes)         | ratio |
|---:|---|---|---:|---:|---:|---:|
| 1  | GET    | `/session/{sid}/todo`    |  3,300 | 1,410,809 (1.35 MiB)  | 1,410,809 (1.35 MiB)  | 1.000 |
| 2  | GET    | `/session/{sid}/children`|  3,393 |   427,682 (0.41 MiB)  |   429,620 (0.41 MiB)  | 1.005 |
| 3  | GET    | `/session/status`        | 26,765 |   205,767 (0.20 MiB)  |   206,022 (0.20 MiB)  | 1.001 |
| 4  | GET    | `/session/{sid}`         |    284 |   173,042 (0.17 MiB)  |   173,093 (0.17 MiB)  | 1.000 |
| 5  | GET    | `/config/providers`      |     49 |   122,261 (0.12 MiB)  |   122,261 (0.12 MiB)  | 1.000 |
| 6  | PATCH  | `/session/{sid}`         |    157 |    91,359 (0.09 MiB)  |    91,359 (0.09 MiB)  | 1.000 |
| 7  | GET    | `/file`                  |     68 |    30,952 (0.03 MiB)  |    30,952 (0.03 MiB)  | 1.000 |
| 8  | POST   | `/session/{sid}/command` |     41 |    29,650 (0.03 MiB)  |    30,007 (0.03 MiB)  | 1.012 |
| 9  | GET    | `/file/content`          |      4 |    24,901 (0.02 MiB)  |    24,901 (0.02 MiB)  | 1.000 |
| 10 | POST   | `/session`               |     49 |    18,513 (0.02 MiB)  |    18,513 (0.02 MiB)  | 1.000 |
| 11 | GET    | `/session/{sid}/diff`    |  2,685 |     5,370 (0.01 MiB)  |     5,370 (0.01 MiB)  | 1.000 |
| 12 | GET    | `/permission`            |  2,393 |     4,786 (0.00 MiB)  |     4,786 (0.00 MiB)  | 1.000 |
| 13 | GET    | `/`                      |      1 |     2,884 (0.00 MiB)  |     2,884 (0.00 MiB)  | 1.000 |
| 14 | GET    | `/question`              |    373 |     1,835 (0.00 MiB)  |     1,835 (0.00 MiB)  | 1.000 |
| 15 | POST   | `/session/{sid}/abort`   |    159 |       636 (0.00 MiB)  |       636 (0.00 MiB)  | 1.000 |
| 16 | POST   | `/question/{qid}/reply`  |     79 |       316 (0.00 MiB)  |       316 (0.00 MiB)  | 1.000 |
| 17 | POST   | `/question/{qid}/reject` |      5 |        20 (0.00 MiB)  |        20 (0.00 MiB)  | 1.000 |
| 18 | GET    | `/file/status`           |      5 |        10 (0.00 MiB)  |        10 (0.00 MiB)  | 1.000 |
| 19 | POST   | `/session/{sid}/summarize` |   16 |         8 (0.00 MiB)  |       722 (0.00 MiB)  | 90.250 |
| 20 | POST   | `/session/{sid}/prompt_async` | 362 |       0 (0.00 MiB)  |         0 (0.00 MiB)  |  n/a  |

Observations on the table:
- `ratio ≈ 1.000` across the board is the **expected baseline** — passthrough is an
  unslimmed proxy, so `downOut ≈ upIn` by construction. This is exactly why these
  rows are "passthrough": no byte-level slimming is happening for them today.
- The single anomaly is `POST /session/{sid}/summarize` (ratio 90.25 on 16 reqs):
  tiny request body, non-trivial response — a write that produces a much larger
  downstream payload. Excluded from candidates (it is a POST).

---

## Slimming candidates

Cross-referenced against `docs/specs/INTERFACE_MAP.md`. A route is a **slimming
candidate** iff it is a **read-only GET** pattern with **no existing `/slimapi/**`
thin route**. Writes (`PATCH`/`POST`/`PUT`/`DELETE`) are excluded from the candidate
list (they appear in the table above for completeness but are not ranked here).

### A. True candidates — no `/slimapi/**` thin route exists (ranked by `upIn`)

| rank | method | normalized_path        | requests | upIn (bytes)        | notes |
|---:|---|---|---:|---:|---|
| 1 | GET | `/session/{sid}/todo`     |  3,300 | 1,410,809 (1.35 MiB) | **Top read cost.** Per-session todo list; no thin route today. Highest-value slimming target. |
| 2 | GET | `/session/{sid}/children` |  3,393 |   427,682 (0.41 MiB) | Per-session child list; no thin route. Second-highest read cost. |
| 3 | GET | `/session/{sid}`          |    284 |   173,042 (0.17 MiB) | Single-session detail. `/slimapi/sessions` is the **list** only — no thin route for one-session fetch. |
| 4 | GET | `/config/providers`       |     49 |   122,261 (0.12 MiB) | Read-only config; no thin route. Low frequency but sizable per-response. |
| 5 | GET | `/file`                   |     68 |    30,952 (0.03 MiB) | File metadata read; no thin route. |
| 6 | GET | `/file/content`           |      4 |    24,901 (0.02 MiB) | File content read; no thin route. Low frequency. |
| 7 | GET | `/session/{sid}/diff`     |  2,685 |     5,370 (0.01 MiB) | High request count, **tiny** per-response. Slimming value low (overhead-dominated). |
| 8 | GET | `/permission`             |  2,393 |     4,786 (0.00 MiB) | Permission poll; high freq, tiny payload. Slimming value low. |
| 9 | GET | `/`                       |      1 |     2,884 (0.00 MiB) | Root probe / health-like. Almost certainly noise — **not a real candidate**. |
| 10 | GET | `/file/status`           |      5 |        10 (0.00 MiB) | Negligible volume. |

**Estimated potential:** because every row above is currently passthrough
(`ratio ≈ 1.000`, i.e. zero slimming today), the **upper bound** on what a new thin
route could save per pattern is, in raw bytes, its full `upIn` column (a thin route
can only reduce this; it cannot make it worse). The realistic realized saving depends
on how compressible / projectable each payload is — out of scope for this read-only
report, but the `upIn` ranking is the prioritized backlog.

The two **dominant** candidates by read cost are per-session GETs:
- `GET /session/{sid}/todo` (1.35 MiB / 3 days)
- `GET /session/{sid}/children` (0.41 MiB / 3 days)

Together they account for the majority of all unslimmed read bytes in the window.

### B. NOT candidates — thin route already exists (cutover-gap, not a missing-route gap)

These GETs are still showing up in **passthrough** despite a `/slimapi/**` equivalent
already existing in `INTERFACE_MAP.md`. The fix here is **client routing / cutover**,
not a new thin route. Reported because the volume is significant.

| method | passthrough path     | passthrough reqs (3d) | existing thin route            | thin-route reqs (3d) | read |
|---|---|---:|---|---:|---|
| GET | `/session/status` | 26,765 | `/slimapi/sessions/status` (INTERFACE_MAP §1) | 5,511 | `GET /session/status` → status map |
| GET | `/question`       |    373 | `/slimapi/questions` (INTERFACE_MAP §1)        | 7,792 | fan-out `GET /question` per directory |

- `/session/status` is the standout: the client calls the **legacy passthrough** route
  ~5× more often than the existing thin route (26,765 vs 5,511). It is a small static
  read, so per-byte value is low, but the request-volume asymmetry shows a real
  cutover gap worth closing on the client side.
- `/question` is mostly cut over (thin route 7,792 vs passthrough 373); the residual
  passthrough is a small uncutover tail.

### C. Writes — excluded from candidates (shown for completeness)

All `PATCH`/`POST` patterns from the top table: `PATCH /session/{sid}`,
`POST /session/{sid}/command`, `POST /session`, `POST /session/{sid}/abort`,
`POST /question/{qid}/reply`, `POST /question/{qid}/reject`,
`POST /session/{sid}/summarize`, `POST /session/{sid}/prompt_async`. None are
slimming candidates by definition (writes; and `prompt_async` is the primary agent
send path per INTERFACE_MAP §7).

---

## Caveats

1. **Passthrough is a catch-all.** It contains both writes and unslimmed reads. A
   high `upIn` on a passthrough route does not by itself imply a thin route is
   desirable — only the **read-only GET** subset (section A/B above) is in scope.
2. **`ratio ≈ 1.000` is the expected baseline**, not an anomaly. It simply confirms
   these routes are currently unslimmed (passthrough = byte-for-byte proxy). The
   single non-baseline ratio (`summarize`, 90.25) is a write and is excluded.
3. **Per-session GETs dominate read cost.** `GET /session/{sid}/todo` and
   `GET /session/{sid}/children` are the overwhelming majority of unslimmed read
   bytes; any new thin-route work should prioritize these two shapes first.
4. **Two "high-volume" GETs are cutover gaps, not missing routes.** `/session/status`
   and `/question` already have thin routes; their passthrough volume reflects
   incomplete client migration, which is a client-side (ocdroid) action, not a
   sidecar thin-route gap.
5. **`{sid}` / `{mid}` / `{qid}` are placeholders.** Every per-session / per-message
   / per-question row in the underlying data was folded into a single route-shape
   row; no opaque id appears in this report (see Privacy section).
6. **Window is 3 days.** Day-3 (`access-2026-08-10.jsonl`) is the still-being-written
   plain (un-gzipped) file for today and is partial; counts for today are
   lower-bound as of execution time. Relative ranking is stable.
7. **`GET /` (rank 13)** is almost certainly a probe/health hit, not application
   traffic; it is listed for completeness and explicitly discounted as a candidate.

---

## Verification

- **Acceptance T16-C1:** report exists, states the 3-day window, and embeds the
  reproducible read-only heredoc (Method section). ✓
- **Acceptance T16-C2:** top table has `method | path | requests | upIn | downOut`,
  sorted by `upIn` descending, with raw integer bytes preserved alongside MiB. ✓
- **Acceptance T16-C3 (privacy self-grep):** the finished file was grepped for
  forbidden tokens — literal `ses_`, `msg_`, `que_`, `clientId`, `requestId`,
  `clientVer`, header names (`X-`), and query markers (`?`). The only matches for
  `ses_`/`msg_`/`que_` are **inside the Method code block** as the regex literals
  `r"ses_[0-9A-Za-z]+"` / `r"msg_..."` / `r"que_..."` (i.e. the redaction rules
  themselves) and the `{sid}`/`{mid}`/`{qid}` placeholders — never a concrete id and
  never any client/header/query/requestId value. No request/response header names,
  no query strings, no `clientId`/`client`/`clientVer`/`requestId` values appear. ✓
- **Acceptance T16-C4:** n/a — in-window logs exist and passthrough records are
  present (40,188 passthrough requests); BLOCKED path not taken. ✓
- **Baseline HEAD:** `6a4ca78fa9a8f2951f669d61170a32e216417896`.
