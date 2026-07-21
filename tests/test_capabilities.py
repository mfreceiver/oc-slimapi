"""Unit tests for ``oc_slimapi.capabilities.parse_capabilities`` (Opt-A grammar)."""

from __future__ import annotations

import pytest

from oc_slimapi.capabilities import CapabilityParse, parse_capabilities


def _assert_parse(result, *, opt_in, duplicate_conflict, malformed_tokens, unknown_tokens):
    assert result.opt_in == opt_in
    assert result.duplicate_conflict == duplicate_conflict
    assert result.malformed_tokens == malformed_tokens
    assert result.unknown_tokens == unknown_tokens


class TestParseNoneOrEmpty:
    def test_none(self):
        result = parse_capabilities(None)
        _assert_parse(result, opt_in=False, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=0)

    def test_empty(self):
        result = parse_capabilities("")
        _assert_parse(result, opt_in=False, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=0)


class TestParseOptIn:
    def test_opt_in_1(self):
        result = parse_capabilities("mid-partial-envelope=1")
        _assert_parse(result, opt_in=True, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=0)

    def test_opt_in_0(self):
        result = parse_capabilities("mid-partial-envelope=0")
        _assert_parse(result, opt_in=False, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=0)

    def test_case_insensitive_name(self):
        result = parse_capabilities("Mid-Partial-Envelope=1")
        _assert_parse(result, opt_in=True, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=0)

    def test_value_case_sensitive(self):
        # Only literal "1" works.
        result = parse_capabilities("mid-partial-envelope=ONE")
        _assert_parse(result, opt_in=False, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=1)


class TestParseUnknownTokens:
    def test_unknown_token_ignored(self):
        result = parse_capabilities("mid-partial-envelope=1, future-cap=7")
        _assert_parse(result, opt_in=True, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=1)

    def test_mixed_known_unknown(self):
        result = parse_capabilities("mid-partial-envelope=1, other=abc")
        _assert_parse(result, opt_in=True, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=1)


class TestParseMalformed:
    def test_no_eq(self):
        result = parse_capabilities("mid-partial-envelope=1, garbage")
        _assert_parse(result, opt_in=True, duplicate_conflict=False, malformed_tokens=1, unknown_tokens=0)

    def test_empty_name(self):
        result = parse_capabilities("=1")
        _assert_parse(result, opt_in=False, duplicate_conflict=False, malformed_tokens=1, unknown_tokens=0)

    def test_trailing_comma(self):
        result = parse_capabilities("mid-partial-envelope=1, ")
        _assert_parse(result, opt_in=True, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=0)

    def test_leading_spaces(self):
        result = parse_capabilities("  mid-partial-envelope = 1  ")
        _assert_parse(result, opt_in=True, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=0)


class TestParseDuplicate:
    def test_duplicate_same_value_idempotent(self):
        result = parse_capabilities("mid-partial-envelope=1, mid-partial-envelope=1")
        _assert_parse(result, opt_in=True, duplicate_conflict=False, malformed_tokens=0, unknown_tokens=0)

    def test_duplicate_conflicting(self):
        result = parse_capabilities("mid-partial-envelope=1, mid-partial-envelope=0")
        _assert_parse(result, opt_in=False, duplicate_conflict=True, malformed_tokens=0, unknown_tokens=0)

    def test_duplicate_more_than_two(self):
        result = parse_capabilities(
            "mid-partial-envelope=1, mid-partial-envelope=0, mid-partial-envelope=1"
        )
        # The conflict is detected; the first conflicting pair wins.
        _assert_parse(result, opt_in=False, duplicate_conflict=True, malformed_tokens=0, unknown_tokens=0)
