"""Unit tests for the unified error model."""

from __future__ import annotations

from deskweave_contracts import ErrorInfo, ValidationError


def test_error_to_dict() -> None:
    error = ValidationError("bad input", details={"field": "name"})
    assert error.to_dict() == {
        "code": "validation_error",
        "message": "bad input",
        "details": {"field": "name"},
    }


def test_error_to_info() -> None:
    info = ValidationError("nope").to_info()
    assert isinstance(info, ErrorInfo)
    assert info.code == "validation_error"
    assert info.message == "nope"


def test_str_includes_code() -> None:
    assert "validation_error" in str(ValidationError("x"))
