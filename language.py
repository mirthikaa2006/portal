"""Locale-aware classification of portal files and tests."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from impact_selector.models import ChangeKind, ChangedFile, TestCase, TestLayer

SUPPORTED_LOCALES = ("en", "hi", "ta", "te", "bn")

_LOCALE_PATH = re.compile(
    r"(?:locales?|i18n|content|translations)[/\\]("
    + "|".join(SUPPORTED_LOCALES)
    + r")(?:[/\\]|\.|$)",
    re.IGNORECASE,
)
_LOCALE_FILENAME = re.compile(
    r"[._-](" + "|".join(SUPPORTED_LOCALES) + r")\.(json|po|yml|yaml|md)$",
    re.IGNORECASE,
)


def detect_locale(path: str) -> str | None:
    match = _LOCALE_PATH.search(path.replace("\\", "/"))
    if match:
        return match.group(1).lower()
    match = _LOCALE_FILENAME.search(path.replace("\\", "/"))
    if match:
        return match.group(1).lower()
    return None


def infer_kind(path: str) -> ChangeKind:
    normalized = path.replace("\\", "/").lower()
    if detect_locale(normalized) and any(
        token in normalized for token in ("locale", "i18n", "content", "translation", ".po", ".json")
    ):
        if any(token in normalized for token in ("src/", "app/", "services/", ".py", ".ts", ".js")):
            if "i18n" in normalized or "locale" in normalized:
                suffix = PurePosixPath(normalized).suffix
                if suffix in {".json", ".po", ".yml", ".yaml", ".md"}:
                    return ChangeKind.CONTENT
        if PurePosixPath(normalized).suffix in {".json", ".po", ".yml", ".yaml", ".md"}:
            return ChangeKind.CONTENT
    if any(normalized.endswith(ext) for ext in (".yml", ".yaml", ".toml", ".env.example")):
        return ChangeKind.CONFIG
    if "schema" in normalized or normalized.endswith(".sql"):
        return ChangeKind.SCHEMA
    return ChangeKind.CODE


def classify_change(files: list[ChangedFile]) -> tuple[ChangeKind, list[str], bool]:
    locales = sorted({f.locale or detect_locale(f.path) for f in files if (f.locale or detect_locale(f.path))})
    kinds = {f.kind for f in files}
    content_only = bool(files) and all(f.kind == ChangeKind.CONTENT for f in files)
    if len(kinds) == 1:
        kind = next(iter(kinds))
    elif kinds == {ChangeKind.CONTENT, ChangeKind.CODE}:
        kind = ChangeKind.MIXED
    elif not kinds:
        kind = ChangeKind.CODE
    else:
        kind = ChangeKind.MIXED
    return kind, locales, content_only


def test_is_locale_specific(test: TestCase) -> bool:
    return bool(test.locales) and test.layer in {TestLayer.LOCALE, TestLayer.E2E}


def locale_relevance(test: TestCase, locales_touched: list[str], content_only: bool) -> str:
    """Return keep, skip_locale, or shared."""
    if not test.locales:
        return "shared"
    if not locales_touched:
        return "keep"
    overlap = set(test.locales) & set(locales_touched)
    if overlap:
        return "keep"
    if content_only and test.layer.value in {"locale", "e2e"} and "shared-smoke" not in test.tags:
        return "skip_locale"
    return "shared"
