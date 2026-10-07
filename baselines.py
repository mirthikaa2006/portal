"""Naive baselines used in the experiment notebook."""

from __future__ import annotations

from impact_selector.language import detect_locale
from impact_selector.models import ChangedFile, Decision, TestCase


def run_all(tests: list[TestCase]) -> set[str]:
    return {test.test_id for test in tests}


def file_pattern_baseline(tests: list[TestCase], files: list[ChangedFile]) -> set[str]:
    """Keep tests whose name/tags share a path token with a changed file."""
    keep: set[str] = set()
    tokens: set[str] = set()
    for item in files:
        for part in item.path.replace("\\", "/").lower().split("/"):
            if part and part not in {"src", "app", "tests", "locales", "i18n"}:
                tokens.add(part.split(".")[0])
        locale = item.locale or detect_locale(item.path)
        if locale:
            tokens.add(locale)
    for test in tests:
        hay = {test.name.lower(), *{tag.lower() for tag in test.tags}, *test.locales}
        joined = " ".join(hay)
        if test.critical or test.layer.value in {"smoke", "security"}:
            keep.add(test.test_id)
            continue
        if any(token in joined or token in test.name.lower() for token in tokens):
            keep.add(test.test_id)
    return keep


def decisions_from_keep(tests: list[TestCase], keep: set[str]) -> dict[str, Decision]:
    return {
        test.test_id: Decision.MUST_RUN if test.test_id in keep else Decision.SKIP
        for test in tests
    }
