"""v4-contract §12 provider safe projection — pure logic, no IO.

``GET /slimapi/config/providers?v=4`` replaces the v3 verbatim
controlled proxy with a **whitelist projection** of the upstream
``ConfigProvidersResult`` (opencode ``packages/opencode/src/provider/
provider.ts``): ``{providers: Info[], default: Record<string,string>}``
where ``Info.models`` is a ``Record<string, Model>`` map.

Everything in this module is a pure function of its inputs so the whole
decode→validate→project→count→serialize→cap→gzip→ETag chain (§12.5.2
steps ⑥-⑪) runs as ONE job on a transform worker — the route owns only
the async I/O (fetch, status mapping, cap-read, permit) and the final
conditional judgment (step ⑫).

Frozen wire semantics (docs/specs/v4-contract.md §12 — authoritative):

* **§12.1 schema** — top level is EXACTLY ``providers`` + ``default``
  (extra/missing ⇒ malformed); unknown nested fields are discarded
  RECURSIVELY (never an error); ``models`` must be a map whose key
  equals ``Model.id``; ``variants`` emits only the sorted map-key array
  (a present-but-non-map ``variants`` is the one optional-key error);
  optional keys (``source``/``status``) pass verbatim iff string, else
  the key is omitted (never ``null``).
* **§12.2 ordering** — providers by id UTF-8 byte order (globally
  unique), models by upstream map-key byte order, variants by key byte
  order, ``default`` keys sorted at serialization (OPT_SORT_KEYS).
* **§12.3 default triple** — key ∈ emitted provider ids; value ∈ THAT
  provider's model ids; that model's ``providerID`` == key.
* **§12.4 limits** — fixed wire constants (NO env override), fail-closed,
  first-triggered-wins, no truncation: providers=256,
  models_per_provider=1024, variants_per_model=64,
  projected_body_bytes=8 MiB (identity, pre-gzip).
* **§12.6 ETag** — canonical bytes ARE the wire body (no reordered
  copy); identity strong / gzip weak; REP_VERSION carries the
  wire-view + projection fingerprint so v3 passthrough validators never
  cross-match the v4 projection domain.
"""

from __future__ import annotations

import gzip
import json
from typing import Any, Final

import orjson

from . import etag as etag_mod
from .gzip_util import compress_if_beneficial

# --- §12.4 frozen wire constants (do NOT make these configurable) ----------

MAX_PROVIDERS: Final[int] = 256
MAX_MODELS_PER_PROVIDER: Final[int] = 1024
MAX_VARIANTS_PER_MODEL: Final[int] = 64
MAX_PROJECTED_BODY_BYTES: Final[int] = 8_388_608  # 8 MiB identity pre-gzip

# §12.6 REP_VERSION domain: the projection's representation version. Any
# change to the projection semantics (field policy, ordering, limits, gzip
# gate) MUST bump this so every validator rotates (a stale client can never
# receive a false 304). Joined with the frozen limits and the ``wire=v4``
# marker this is a domain DISTINCT from the v3 passthrough REP_VERSION
# (which is skeleton-based) — the passthrough→projection switch itself
# rotates all validators by construction.
PROVIDERS_REPRESENTATION_VERSION: Final[bytes] = b"providers-projection-v1"

_ETAG_SCHEME_VERSION: Final[bytes] = b"etag-v1"


class ProviderUpstreamMalformed(ValueError):
    """§12.5.3: deterministic upstream shape breach → 502.

    Raised for JSON decode failure, duplicate JSON member names, a
    non-exactly-two-key top level, wrong types, missing required fields,
    ``models`` map-key ≠ ``Model.id``, nested ``providerID`` mismatch,
    duplicate provider ids, ``variants`` present but non-map, a failed
    default triple, and non-200 2xx upstream statuses (route-mapped).
    """


class ProviderProjectionLimit(Exception):
    """§12.5.3: over a §12.4 frozen limit → 413 ``provider_projection_limit``.

    ``limit`` is the wire constant NAME (``providers`` /
    ``models_per_provider`` / ``variants_per_model`` /
    ``projected_body_bytes``); ``limit_value`` its frozen integer.
    """

    def __init__(self, limit: str, limit_value: int) -> None:
        self.limit = limit
        self.limit_value = limit_value
        super().__init__(f"{limit}={limit_value}")


def providers_rep_version(config: Any) -> bytes | None:
    """§12.6 REP_VERSION for the v4 providers projection, ``None`` when
    ``etag_enabled=false`` (no ETag / no 304; ``Vary`` is still sent).

    Mirrors :func:`oc_slimapi.etag.response_rep_version` gating but builds
    a providers-specific fingerprint: the scheme version, the projection
    representation version, the four frozen limits, and the ``wire=v4``
    domain marker. The skeleton config fields do NOT participate — they
    cannot change these bytes.
    """
    if config is None or not getattr(config, "etag_enabled", True):
        return None
    return b"\x00".join([
        _ETAG_SCHEME_VERSION,
        PROVIDERS_REPRESENTATION_VERSION,
        str(MAX_PROVIDERS).encode("ascii"),
        str(MAX_MODELS_PER_PROVIDER).encode("ascii"),
        str(MAX_VARIANTS_PER_MODEL).encode("ascii"),
        str(MAX_PROJECTED_BODY_BYTES).encode("ascii"),
        b"wire=v4",
    ])


# --- ⑥ strict decode (duplicate member names always rejected) --------------


def _reject_duplicate_members(pairs: list[tuple[str, Any]]) -> dict:
    """``object_pairs_hook``: any duplicated member name anywhere in the
    document ⇒ malformed (fail-closed — orjson silently keeps the last
    duplicate, so decoding goes through the stdlib parser)."""
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ProviderUpstreamMalformed(f"duplicate member: {key!r}")
        obj[key] = value
    return obj


def _loads_strict(body: bytes) -> Any:
    try:
        return json.loads(body, object_pairs_hook=_reject_duplicate_members)
    except ProviderUpstreamMalformed:
        raise
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ProviderUpstreamMalformed("undecodable providers body") from exc


# --- ⑦ schema / relational validation (§12.5.2 order) -----------------------


def _ensure_utf8(value: str, what: str) -> None:
    """rev-cgpt P0-4: every string that participates in the projection
    or in a canonical sort must be UTF-8 ENCODABLE.

    The stdlib JSON decoder materialises escaped lone surrogates
    (``\\ud800``) as str objects that ``str.encode("utf-8")`` rejects.
    Such a string anywhere in a projected/sorted position (ids, names,
    source/status, variant keys, default key/value) is a deterministic
    upstream shape breach → :class:`ProviderUpstreamMalformed` (§12.5.3
    uniform 502), never an uncaught ``UnicodeEncodeError`` leaking as
    an internal 500.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ProviderUpstreamMalformed(
            f"{what} is not UTF-8 encodable") from exc


def _require_str(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise ProviderUpstreamMalformed(f"{what} is not a string")
    _ensure_utf8(value, what)
    return value


def _validate(parsed: Any) -> tuple[list[tuple[str, dict]], dict[str, str]]:
    """Full §12.1/§12.2/§12.3 validation BEFORE any projection/count.

    §12.5.2: validation (⑦) completes before projection+limits (⑧), so a
    document that is both malformed and over-limit reports malformed.

    Returns ``(provider_entries, default_map)`` — provider entries in
    upstream order as ``(id, entry)`` pairs (the caller sorts/projects/
    counts) plus the raw (already triple-validated) default map.
    """
    # ⑦-1: top level is an object with EXACTLY the two wire keys.
    if not isinstance(parsed, dict):
        raise ProviderUpstreamMalformed("top level is not an object")
    if set(parsed.keys()) != {"providers", "default"}:
        raise ProviderUpstreamMalformed(
            "top level is not exactly {providers, default}")
    raw_providers = parsed["providers"]
    raw_default = parsed["default"]
    if not isinstance(raw_providers, list):
        raise ProviderUpstreamMalformed("providers is not an array")
    if not isinstance(raw_default, dict):
        raise ProviderUpstreamMalformed("default is not a map")

    # ⑦-2: array entries are objects; required fields present + typed.
    #      Every string bound for the projection or a canonical sort is
    #      UTF-8-encodable-checked HERE (P0-4) — required fields via
    #      _require_str, optional/source-of-keys below.
    for entry in raw_providers:
        if not isinstance(entry, dict):
            raise ProviderUpstreamMalformed("provider entry is not an object")
        _require_str(entry.get("id"), "provider.id")
        _require_str(entry.get("name"), "provider.name")
        source = entry.get("source")
        if isinstance(source, str):
            # optional: any non-string shape is silently omitted — but a
            # STRING that cannot hit the wire as UTF-8 is malformed.
            _ensure_utf8(source, "provider.source")
        models = entry.get("models")
        if not isinstance(models, dict):
            raise ProviderUpstreamMalformed("provider.models is not a map")
        for model in models.values():
            if not isinstance(model, dict):
                raise ProviderUpstreamMalformed("model entry is not an object")
            _require_str(model.get("id"), "model.id")
            _require_str(model.get("name"), "model.name")
            _require_str(model.get("providerID"), "model.providerID")
            status = model.get("status")
            if isinstance(status, str):
                _ensure_utf8(status, "model.status")
            if "variants" in model:
                variants = model["variants"]
                if not isinstance(variants, dict):
                    # the ONE optional-key error path (§12.1)
                    raise ProviderUpstreamMalformed(
                        "model.variants is not a map")
                for variant_key in variants:
                    _ensure_utf8(variant_key, "model.variants key")

    seen_ids: set[str] = set()
    for entry in raw_providers:
        pid = entry["id"]
        # ⑦-3: models map key == Model.id.
        for key, model in entry["models"].items():
            if model["id"] != key:
                raise ProviderUpstreamMalformed(
                    "models map key != Model.id")
        # ⑦-4: nested providerID == container provider id.
        for model in entry["models"].values():
            if model["providerID"] != pid:
                raise ProviderUpstreamMalformed(
                    "model.providerID != container provider id")
        # §12.2: provider id globally unique.
        if pid in seen_ids:
            raise ProviderUpstreamMalformed("duplicate provider id")
        seen_ids.add(pid)

    # ⑦-5: default triple (§12.3) — key hits an emitted provider id; value
    # hits THAT provider's Model.id; that model's providerID == key.
    by_id = {entry["id"]: entry for entry in raw_providers}
    for key, value in raw_default.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ProviderUpstreamMalformed("default entry is not string:string")
        _ensure_utf8(key, "default key")
        _ensure_utf8(value, "default value")
        entry = by_id.get(key)
        if entry is None:
            raise ProviderUpstreamMalformed("default key is not a provider id")
        model = entry["models"].get(value)
        if model is None:
            raise ProviderUpstreamMalformed(
                "default value is not a model of that provider")
        if model["providerID"] != key:
            raise ProviderUpstreamMalformed(
                "default model providerID mismatch")

    return [(entry["id"], entry) for entry in raw_providers], dict(raw_default)


# --- ⑧ projection + counts --------------------------------------------------


def _utf8_key(value: str) -> bytes:
    """§12.2 ordering key: UTF-8 byte order (explicit, not code-point
    coincidence — identical for valid str, but the byte intent is the
    frozen contract language)."""
    return value.encode("utf-8")


def _project(validated: list[tuple[str, dict]],
             default: dict[str, str]) -> dict:
    """Whitelist projection with the §12.4 count tripwires, evaluated in
    the §12.5.2 ⑧ order: providers → per-provider models → per-model
    variants (first trigger wins; no truncation)."""
    if len(validated) > MAX_PROVIDERS:
        raise ProviderProjectionLimit("providers", MAX_PROVIDERS)

    providers_out: list[dict] = []
    for pid, entry in sorted(validated, key=lambda pair: _utf8_key(pair[0])):
        models_in = entry["models"]
        if len(models_in) > MAX_MODELS_PER_PROVIDER:
            raise ProviderProjectionLimit(
                "models_per_provider", MAX_MODELS_PER_PROVIDER)

        models_out: list[dict] = []
        # §12.2: models array follows the upstream map-key byte order.
        for key in sorted(models_in, key=_utf8_key):
            model = models_in[key]
            projected_model: dict[str, Any] = {
                "id": model["id"],
                "name": model["name"],
                "providerID": model["providerID"],
            }
            # optional keys: any shape that is not a string is OMITTED
            # (never an error, never null) — §12.1 field policy.
            status = model.get("status")
            if isinstance(status, str):
                projected_model["status"] = status
            variants_in = model.get("variants")
            if variants_in is not None:  # absent → key omitted
                if len(variants_in) > MAX_VARIANTS_PER_MODEL:
                    raise ProviderProjectionLimit(
                        "variants_per_model", MAX_VARIANTS_PER_MODEL)
                # only the map keys survive; empty map → []
                projected_model["variants"] = sorted(
                    variants_in, key=_utf8_key)
            models_out.append(projected_model)

        provider_out: dict[str, Any] = {
            "id": pid,
            "name": entry["name"],
            "models": models_out,
        }
        source = entry.get("source")
        if isinstance(source, str):
            provider_out["source"] = source
        providers_out.append(provider_out)

    return {
        "providers": providers_out,
        # key order finalized by orjson OPT_SORT_KEYS at serialization
        # (§12.2) — same canonical bytes regardless of upstream order.
        "default": default,
    }


def project_and_pack(
    body: bytes,
    *,
    accept_encoding: str | None,
    rep_version: bytes | None,
) -> tuple[bytes, dict[str, str]]:
    """The single §12.5.2 ⑥-⑪ worker job (pure; no IO).

    ⑥ strict decode (duplicate members rejected) → ⑦ full validation →
    ⑧ projection + count limits → ⑨ canonical serialization
    (``OPT_SORT_KEYS``) → ⑩ projected-body byte limit → ⑪ gzip
    negotiation + compression + ETag derivation.

    rev-cgpt P0-4 worker-boundary defence: serialization-stage value
    errors (``orjson.JSONEncodeError`` on a surrogate that somehow
    slipped past ⑦, ``UnicodeEncodeError`` from a sort key, …) are
    NORMALIZED to :class:`ProviderUpstreamMalformed` so no unclassified
    exception ever escapes the worker as a route 500.
    :class:`ProviderProjectionLimit` is re-raised untouched — it is the
    legal 413 path, never a normalization candidate.

    Returns ``(encoded, headers)``: ``encoded`` is the exact wire body
    (identity or gzip), ``headers`` carries ``Vary: Accept-Encoding``,
    ``ETag`` (when ``rep_version`` is set — strong for identity, weak for
    gzip, always hashing the CANONICAL identity bytes = the wire body)
    and ``Content-Encoding: gzip`` when compression was applied. The
    If-None-Match judgment deliberately does NOT happen here — §12.5.2
    ⑫ keeps it in the caller's (main) context.
    """
    try:
        parsed = _loads_strict(body)                                   # ⑥
        validated, default = _validate(parsed)                         # ⑦
        value = _project(validated, default)                           # ⑧
        canonical = orjson.dumps(value, option=orjson.OPT_SORT_KEYS)   # ⑨
        if len(canonical) > MAX_PROJECTED_BODY_BYTES:                  # ⑩
            raise ProviderProjectionLimit(
                "projected_body_bytes", MAX_PROJECTED_BODY_BYTES)
        # ⑪: negotiation decision + compression both stay in the worker —
        # the event loop never serializes or gzips.
        encoded, coding_headers = compress_if_beneficial(
            canonical, accept_encoding)
    except ProviderProjectionLimit:
        raise
    except ProviderUpstreamMalformed:
        raise
    except ValueError as exc:
        # UnicodeEncodeError ⊂ UnicodeError ⊂ ValueError; orjson's
        # JSONEncodeError is a ValueError too — one clause covers the
        # whole serialization hazard class (fail-closed → 502).
        raise ProviderUpstreamMalformed(
            "providers projection serialization failure") from exc
    headers: dict[str, str] = {"Vary": "Accept-Encoding", **coding_headers}
    if rep_version is not None:
        actual = ("gzip"
                  if coding_headers.get("Content-Encoding") == "gzip"
                  else "identity")
        headers["ETag"] = etag_mod.compute_etag(canonical, actual, rep_version)
    return encoded, headers
