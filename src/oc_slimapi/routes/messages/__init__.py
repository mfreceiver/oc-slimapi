"""``oc_slimapi.routes.messages`` — package form of the historical single
``routes/messages.py`` module (F-302 three-family split; pure move, zero
behaviour change).

``_list`` is imported first, then ``_full_merge``, then ``_expand`` — the
original in-file definition order. (``_list`` imports ``_full_merge`` for
the merged splice, so ``/full`` registers on the shared router a moment
before the list route; the four route paths are disjoint and the
route-table digest sorts rows, so the observable surface is unchanged —
see ``tests/test_refactor_equivalence.py::_scenario_route_table``.)

Every historical module-level name is re-exported below so both
``messages.<name>`` and ``from oc_slimapi.routes.messages import <name>``
keep resolving — including private helpers that tests import directly.
Monkeypatch targets moved to the family submodule that actually resolves
them (design §3 migration table; see
``docs/ocmar/reviews/2026-08-21-wave3-refactor-design.md``).
"""

from ._router import (  # noqa: F401  (compat re-exports)
    TRANSFORM_RETRY_AFTER_SECONDS,
    _busy_response,
    _resolve_messages_directory,
    router,
)
from ._list import (  # noqa: F401
    _REL_PARAM_RE,
    _V4_EXPAND_FEATURE,
    _canonical_list_query,
    _created_sort_key,
    _expand_wire_view,
    _extract_before_verbatim,
    _fetch_list_raw,
    _judge_pack_tail,
    _link_rel_tokens,
    _messages_list_key,
    _messages_via_lease,
    _parse_link_next_cursor,
    _parse_sort_project,
    _project_list_sorted_and_pack,
    _stream_upstream,
    compress_if_beneficial,
    messages,
    read_with_cap,
)
from ._full_merge import (  # noqa: F401
    _CapExceeded,
    _DEGRADED,
    _PLACEHOLDER_PART_ID_PREFIX,
    _dedicated_full_get,
    _expand_ref_pairs,
    _fetch_full_shared,
    _merge_fulls,
    _merge_fulls_and_pack,
    _merged_candidate_pairs,
    _placeholder_pairs,
    full_fetch_key,
    fulls,
    message,
    recompute_fingerprint,
    strip_diagnostics_and_pack,
    strip_diagnostics_message,
)
from ._expand import (  # noqa: F401
    _EXPAND_APPLICABLE_TYPES,
    _EXPAND_CATEGORIES,
    _EXPAND_CATEGORIES_SET,
    _EXPAND_EXTRACTORS,
    _EXPAND_MESSAGE_LEVEL_CATEGORIES,
    _expand_fragment,
    _expand_fragment_worker,
    _expand_locate_part,
    _expand_shape_error,
    _expand_state,
    _expand_str_field,
    _extract_compaction_full,
    _extract_info_summary_diffs,
    _extract_part_reasoning,
    _extract_part_snapshot,
    _extract_part_source,
    _extract_part_state_attachments,
    _extract_part_state_error,
    _extract_part_state_input_full,
    _extract_part_state_metadata_full,
    _extract_part_state_output,
    _extract_part_text,
    _extract_part_url,
    expand_message_fragment,
    expand_part_fragment,
)
