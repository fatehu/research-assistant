#!/usr/bin/env python3
"""Legacy compat module.

The active docling-fast service entrypoint is
`opendataloader_upstream_hybrid_server.py`. This module stays pinned to the
historical compat implementation so old imports and tests remain stable.
"""

from __future__ import annotations

from . import opendataloader_compat_server_backup as _legacy

LocalStructuredPdfDoclingCompatBackend = _legacy.LocalStructuredPdfDoclingCompatBackend
structured_document_to_docling_json = _legacy.structured_document_to_docling_json
build_conversion_response = _legacy.build_conversion_response
sanitize_unicode = _legacy.sanitize_unicode

_CompatConversionStatus = _legacy._CompatConversionStatus
_CompatError = _legacy._CompatError
_CompatInput = _legacy._CompatInput
_CompatDocument = _legacy._CompatDocument
_CompatConversionResult = _legacy._CompatConversionResult


def create_converter(*args, **kwargs):
    return _legacy.create_converter(*args, **kwargs)


def create_app(*args, **kwargs):
    _legacy.create_converter = create_converter
    return _legacy.create_app(*args, **kwargs)


def main():
    return _legacy.main()


__all__ = [
    "LocalStructuredPdfDoclingCompatBackend",
    "structured_document_to_docling_json",
    "build_conversion_response",
    "sanitize_unicode",
    "_CompatConversionStatus",
    "_CompatError",
    "_CompatInput",
    "_CompatDocument",
    "_CompatConversionResult",
    "create_converter",
    "create_app",
    "main",
]
