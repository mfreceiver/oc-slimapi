"""Tests for directory validation (S5)."""

from __future__ import annotations

import pytest
from oc_slimapi.directory import normalize_directory, validate_directory
from oc_slimapi.errors import CodedHTTPException


class TestNormalizeDirectory:
    def test_root(self):
        assert normalize_directory("/") == "/"

    def test_root_slash(self):
        assert normalize_directory("") == "/"

    def test_normal(self):
        assert normalize_directory("/app") == "/app"

    def test_trailing(self):
        assert normalize_directory("/app/") == "/app"

    def test_double_slash_preserved(self):
        # normalize_directory only strips trailing, not collapses //
        assert normalize_directory("//app") == "//app"


class TestValidateDirectory:
    def test_empty(self):
        """Empty string is allowed (normalizes to root /)."""
        assert validate_directory("") == "/"

    def test_root(self):
        assert validate_directory("/") == "/"

    def test_normal(self):
        assert validate_directory("/app") == "/app"

    def test_trailing(self):
        assert validate_directory("/app/") == "/app"

    def test_double_slash(self):
        """Double slashes are NOT collapsed by normalize_directory (only rstrips)."""
        assert validate_directory("//app") == "//app"

    def test_path_traversal_parent(self):
        with pytest.raises(CodedHTTPException) as exc:
            validate_directory("/../etc")
        assert exc.value.status_code == 400
        assert exc.value.code == "invalid_directory"

    def test_path_traversal_current(self):
        with pytest.raises(CodedHTTPException) as exc:
            validate_directory("/./here")
        assert exc.value.status_code == 400
        assert exc.value.code == "invalid_directory"

    def test_null_byte(self):
        with pytest.raises(CodedHTTPException) as exc:
            validate_directory("/app\0")
        assert exc.value.status_code == 400
        assert exc.value.code == "invalid_directory"

    def test_control_char(self):
        with pytest.raises(CodedHTTPException) as exc:
            validate_directory("/app\x01")
        assert exc.value.status_code == 400
        assert exc.value.code == "invalid_directory"

    def test_del_char(self):
        with pytest.raises(CodedHTTPException) as exc:
            validate_directory("/app\x7f")
        assert exc.value.status_code == 400
        assert exc.value.code == "invalid_directory"

    def test_too_long(self):
        long = "/" + "a" * 4096
        with pytest.raises(CodedHTTPException) as exc:
            validate_directory(long)
        assert exc.value.status_code == 400
        assert exc.value.code == "invalid_directory"

    def test_edge_4095_ok(self):
        edge = "/" + "a" * 4095
        assert validate_directory(edge) == edge
