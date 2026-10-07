"""Generate realistic portal datasets for the prototype (deterministic seed)."""

from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from impact_selector.language import infer_kind, detect_locale
from impact_selector.models import ChangedFile

SEED = 42
ORG = "ministry_interior"
PARTNER_ORG = "partner_nid"
STATE_ORG = "state_karnataka"
LOCALES = ["en", "hi", "ta", "te", "bn"]

FILES = [
    "src/auth/session.py",
    "src/auth/otp.py",
    "src/citizen/profile.py",
    "src/benefits/eligibility.py",
    "src/benefits/application.py",
    "src/payments/gateway.py",
    "src/payments/receipt.py",
    "src/documents/upload.py",
    "src/notifications/sms.py",
    "src/notifications/email.py",
    "src/search/index.py",
    "src/appointments/slots.py",
    "src/i18n/formatter.py",
    "src/i18n/fallback.py",
    "config/app.yaml",
    "schemas/citizen.sql",
]
for loc in LOCALES:
    FILES.append(f"locales/{loc}/common.json")
    FILES.append(f"locales/{loc}/benefits.json")
    FILES.append(f"locales/{loc}/appointments.json")

# source -> modules that import / depend on it
DEPENDENCY_MAP = {
    "src/auth/session.py": ["src/auth/otp.py", "src/citizen/profile.py", "src/benefits/application.py"],
    "src/auth/otp.py": ["src/citizen/profile.py"],
    "src/citizen/profile.py": ["src/benefits/application.py", "src/documents/upload.py", "src/appointments/slots.py"],
    "src/benefits/eligibility.py": ["src/benefits/application.py"],
    "src/payments/gateway.py": ["src/payments/receipt.py", "src/benefits/application.py"],
    "src/payments/receipt.py": ["src/notifications/email.py"],
    "src/i18n/formatter.py": [
        "src/benefits/application.py",
        "src/notifications/sms.py",
        "src/notifications/email.py",
        "src/appointments/slots.py",
    ],
    "src/i18n/fallback.py": ["src/i18n/formatter.py"],
    "src/search/index.py": ["src/benefits/application.py"],
    "src/documents/upload.py": ["src/benefits/application.py"],
    "config/app.yaml": ["src/auth/session.py", "src/payments/gateway.py"],
    "schemas/citizen.sql": ["src/citizen/profile.py"],
}


def _file(path: str, added: int = 12, removed: int = 3) -> dict:
    kind = infer_kind(path)
    return ChangedFile(
        path=path,
        kind=kind,
        locale=detect_locale(path),
        lines_added=added,
        lines_removed=removed,
    ).model_dump(mode="json")


def build_tests() -> list[dict]:
    tests: list[dict] = []

    def add(**kwargs):
        tests.append(kwargs)

    units = [
        ("t_unit_session", "test_auth_session", ["auth", "session"], ["src/auth/session.py"], 8, False),
        ("t_unit_otp", "test_auth_otp", ["auth", "otp"], ["src/auth/otp.py", "src/auth/session.py"], 9, False),
        ("t_unit_profile", "test_citizen_profile", ["citizen", "profile"], ["src/citizen/profile.py"], 10, False),
        ("t_unit_eligibility", "test_benefits_eligibility", ["benefits", "eligibility"], ["src/benefits/eligibility.py"], 11, False),
        ("t_unit_application", "test_benefits_application", ["benefits", "application"], ["src/benefits/application.py", "src/benefits/eligibility.py"], 14, False),
        ("t_unit_gateway", "test_payments_gateway", ["payments", "gateway", "payment"], ["src/payments/gateway.py"], 12, False),
        ("t_unit_receipt", "test_payments_receipt", ["payments", "receipt", "payment"], ["src/payments/receipt.py", "src/payments/gateway.py"], 8, False),
        ("t_unit_upload", "test_documents_upload", ["documents", "upload"], ["src/documents/upload.py"], 9, False),
        ("t_unit_sms", "test_notifications_sms", ["notifications", "sms"], ["src/notifications/sms.py"], 6, False),
        ("t_unit_email", "test_notifications_email", ["notifications", "email"], ["src/notifications/email.py"], 7, False),
        ("t_unit_search", "test_search_index", ["search", "index"], ["src/search/index.py"], 10, False),
        ("t_unit_slots", "test_appointments_slots", ["appointments", "slots"], ["src/appointments/slots.py"], 9, False),
        ("t_unit_formatter", "test_i18n_formatter", ["i18n", "formatter"], ["src/i18n/formatter.py"], 7, False),
        ("t_unit_fallback", "test_i18n_fallback", ["i18n", "fallback", "shared-smoke"], ["src/i18n/fallback.py", "src/i18n/formatter.py"], 8, True),
        ("t_unit_config", "test_config_app", ["config"], ["config/app.yaml"], 5, False),
        ("t_unit_schema", "test_schema_citizen", ["schema", "citizen"], ["schemas/citizen.sql", "src/citizen/profile.py"], 13, False),
    ]
    for test_id, name, tags, files, seconds, critical in units:
        add(
            test_id=test_id,
            name=name,
            org_id=ORG,
            layer="unit",
            estimated_seconds=seconds,
            tags=tags,
            locales=[],
            critical=critical,
            partner_visible=True,
            owner_team="platform",
            _files=files,
        )

    add(
        test_id="t_int_benefits_flow",
        name="test_integration_benefits_application",
        org_id=ORG,
        layer="integration",
        estimated_seconds=42,
        tags=["benefits", "application", "profile"],
        locales=[],
        critical=False,
        partner_visible=True,
        owner_team="benefits",
        _files=[
            "src/benefits/application.py",
            "src/benefits/eligibility.py",
            "src/citizen/profile.py",
            "src/i18n/formatter.py",
        ],
    )
    add(
        test_id="t_int_payments",
        name="test_integration_payments",
        org_id=ORG,
        layer="integration",
        estimated_seconds=38,
        tags=["payments", "payment", "gateway"],
        locales=[],
        critical=False,
        partner_visible=False,
        owner_team="payments",
        _files=["src/payments/gateway.py", "src/payments/receipt.py", "src/benefits/application.py"],
    )
    add(
        test_id="t_int_notify",
        name="test_integration_notifications",
        org_id=ORG,
        layer="integration",
        estimated_seconds=22,
        tags=["notifications", "sms", "email"],
        locales=[],
        critical=False,
        partner_visible=True,
        owner_team="notify",
        _files=["src/notifications/sms.py", "src/notifications/email.py", "src/i18n/formatter.py"],
    )
    add(
        test_id="t_int_appointments",
        name="test_integration_appointments",
        org_id=ORG,
        layer="integration",
        estimated_seconds=28,
        tags=["appointments", "slots", "profile"],
        locales=[],
        critical=False,
        partner_visible=True,
        owner_team="appointments",
        _files=["src/appointments/slots.py", "src/citizen/profile.py"],
    )
    add(
        test_id="t_sec_auth",
        name="test_security_auth_session",
        org_id=ORG,
        layer="security",
        estimated_seconds=35,
        tags=["security", "auth", "session"],
        locales=[],
        critical=True,
        partner_visible=False,
        owner_team="security",
        _files=["src/auth/session.py", "src/auth/otp.py"],
    )
    add(
        test_id="t_smoke_portal",
        name="test_smoke_citizen_portal",
        org_id=ORG,
        layer="smoke",
        estimated_seconds=30,
        tags=["smoke", "shared-smoke"],
        locales=["en"],
        critical=True,
        partner_visible=True,
        owner_team="qa",
        _files=["src/auth/session.py", "src/citizen/profile.py", "src/benefits/application.py"],
    )
    add(
        test_id="t_a11y_home",
        name="test_accessibility_home",
        org_id=ORG,
        layer="accessibility",
        estimated_seconds=26,
        tags=["accessibility", "home"],
        locales=[],
        critical=False,
        partner_visible=True,
        owner_team="a11y",
        _files=["src/i18n/formatter.py"],
    )

    for loc in LOCALES:
        add(
            test_id=f"t_locale_{loc}_common",
            name=f"test_locale_{loc}_common_copy",
            org_id=ORG,
            layer="locale",
            estimated_seconds=15,
            tags=["locale", loc, "common"],
            locales=[loc],
            critical=False,
            partner_visible=True,
            owner_team="i18n",
            _files=[f"locales/{loc}/common.json", "src/i18n/formatter.py"],
        )
        add(
            test_id=f"t_locale_{loc}_benefits",
            name=f"test_locale_{loc}_benefits_copy",
            org_id=ORG,
            layer="locale",
            estimated_seconds=18,
            tags=["locale", loc, "benefits"],
            locales=[loc],
            critical=False,
            partner_visible=True,
            owner_team="i18n",
            _files=[f"locales/{loc}/benefits.json", "src/benefits/application.py"],
        )
        add(
            test_id=f"t_e2e_benefits_{loc}",
            name=f"test_e2e_benefits_{loc}",
            org_id=ORG,
            layer="e2e",
            estimated_seconds=55 if loc == "en" else 62,
            tags=["e2e", "benefits", loc],
            locales=[loc],
            critical=False,
            partner_visible=True,
            owner_team="qa",
            _files=[
                f"locales/{loc}/benefits.json",
                "src/benefits/application.py",
                "src/benefits/eligibility.py",
                "src/citizen/profile.py",
            ],
        )
        add(
            test_id=f"t_e2e_appointments_{loc}",
            name=f"test_e2e_appointments_{loc}",
            org_id=ORG,
            layer="e2e",
            estimated_seconds=48,
            tags=["e2e", "appointments", loc],
            locales=[loc],
            critical=False,
            partner_visible=True,
            owner_team="qa",
            _files=[f"locales/{loc}/appointments.json", "src/appointments/slots.py"],
        )

    add(
        test_id="t_partner_nid_hook",
        name="test_partner_nid_identity_hook",
        org_id=ORG,
        layer="integration",
        estimated_seconds=20,
        tags=["partner", "nid", "auth"],
        locales=[],
        critical=False,
        partner_visible=True,
        owner_team="partners",
        _files=["src/auth/session.py", "src/citizen/profile.py"],
    )
    add(
        test_id="t_nid_internal",
        name="test_nid_internal_sync",
        org_id=PARTNER_ORG,
        layer="integration",
        estimated_seconds=25,
        tags=["partner", "internal"],
        locales=[],
        critical=True,
        partner_visible=True,
        owner_team="nid",
        _files=["src/auth/session.py"],
    )
    add(
        test_id="t_karnataka_scheme",
        name="test_state_karnataka_scheme_overlay",
        org_id=STATE_ORG,
        layer="e2e",
        estimated_seconds=40,
        tags=["state", "benefits", "kn"],
        locales=["en", "ta"],
        critical=False,
        partner_visible=False,
        owner_team="state",
        _files=["src/benefits/eligibility.py"],
    )
    return tests


def build_changes(rng: random.Random) -> tuple[list[dict], dict[str, list[str]]]:
    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    ground: dict[str, list[str]] = {}
    changes: list[dict] = []

    def push(change_id, summary, files, affected, partner_visible=False, hours=0):
        changes.append(
            {
                "change_id": change_id,
                "org_id": ORG,
                "files": files,
                "author": rng.choice(["a.rao", "m.iyer", "s.khan", "l.das"]),
                "timestamp": (now + timedelta(hours=hours)).isoformat(),
                "summary": summary,
                "partner_visible": partner_visible,
            }
        )
        ground[change_id] = sorted(set(affected))

    push(
        "chg_hi_benefits_copy",
        "Update Hindi benefits copy for scholarship scheme rename",
        [_file("locales/hi/benefits.json", 40, 22)],
        [
            "t_locale_hi_benefits",
            "t_e2e_benefits_hi",
        ],
        hours=1,
    )
    push(
        "chg_auth_session",
        "Harden session expiry for citizen login",
        [_file("src/auth/session.py", 28, 9)],
        [
            "t_unit_session",
            "t_unit_otp",
            "t_sec_auth",
            "t_smoke_portal",
            "t_partner_nid_hook",
            "t_int_benefits_flow",
        ],
        partner_visible=True,
        hours=2,
    )
    push(
        "chg_payments_gateway",
        "Retry policy for UPI timeout in payment gateway",
        [_file("src/payments/gateway.py", 33, 6)],
        [
            "t_unit_gateway",
            "t_unit_receipt",
            "t_int_payments",
            "t_int_benefits_flow",
        ],
        hours=3,
    )
    push(
        "chg_eligibility_rule",
        "Adjust rural eligibility threshold in benefits engine",
        [_file("src/benefits/eligibility.py", 18, 4)],
        [
            "t_unit_eligibility",
            "t_unit_application",
            "t_int_benefits_flow",
            "t_e2e_benefits_en",
            "t_e2e_benefits_hi",
            "t_e2e_benefits_ta",
            "t_e2e_benefits_te",
            "t_e2e_benefits_bn",
        ],
        hours=4,
    )
    push(
        "chg_i18n_fallback",
        "Fix missing-key fallback so Hindi UI does not render English stubs",
        [_file("src/i18n/fallback.py", 15, 2), _file("src/i18n/formatter.py", 8, 1)],
        [
            "t_unit_fallback",
            "t_unit_formatter",
            "t_int_benefits_flow",
            "t_int_notify",
            "t_a11y_home",
            "t_locale_en_common",
            "t_locale_hi_common",
            "t_locale_ta_common",
            "t_locale_te_common",
            "t_locale_bn_common",
        ],
        hours=5,
    )
    push(
        "chg_ta_and_te_content",
        "Tamil and Telugu appointment slot labels",
        [
            _file("locales/ta/appointments.json", 20, 10),
            _file("locales/te/appointments.json", 19, 9),
        ],
        [
            "t_locale_ta_common",
            "t_e2e_appointments_ta",
            "t_e2e_appointments_te",
        ],
        hours=6,
    )
    # Ground truth for ta/te appointments: locale tests for those files
    ground["chg_ta_and_te_content"] = [
        "t_e2e_appointments_ta",
        "t_e2e_appointments_te",
    ]
    push(
        "chg_schema_profile",
        "Add optional caste_category column to citizen profile schema",
        [_file("schemas/citizen.sql", 6, 0), _file("src/citizen/profile.py", 22, 3)],
        [
            "t_unit_schema",
            "t_unit_profile",
            "t_int_benefits_flow",
            "t_int_appointments",
            "t_documents_wait",
        ],
        hours=7,
    )
    # t_documents_wait is not a real test — fix ground truth
    ground["chg_schema_profile"] = [
        "t_unit_schema",
        "t_unit_profile",
        "t_unit_upload",
        "t_int_benefits_flow",
        "t_int_appointments",
        "t_smoke_portal",
    ]
    push(
        "chg_search_typo",
        "Correct analyzer config for multilingual search index",
        [_file("src/search/index.py", 11, 4)],
        ["t_unit_search", "t_int_benefits_flow"],
        hours=8,
    )
    push(
        "chg_config_timeout",
        "Increase gateway timeout in app config",
        [_file("config/app.yaml", 2, 2)],
        ["t_unit_config", "t_unit_session", "t_unit_gateway", "t_int_payments", "t_sec_auth"],
        hours=9,
    )
    push(
        "chg_sms_template",
        "OTP SMS template uses formatter for locale digits",
        [_file("src/notifications/sms.py", 9, 2)],
        ["t_unit_sms", "t_int_notify"],
        hours=10,
    )
    push(
        "chg_bn_common",
        "Bengali common dictionary punctuation fixes",
        [_file("locales/bn/common.json", 7, 7)],
        ["t_locale_bn_common"],
        hours=11,
    )
    push(
        "chg_mixed_benefits_hi",
        "Eligibility copy + rule change for Hindi-majority states",
        [
            _file("src/benefits/eligibility.py", 10, 2),
            _file("locales/hi/benefits.json", 16, 8),
        ],
        [
            "t_unit_eligibility",
            "t_unit_application",
            "t_int_benefits_flow",
            "t_locale_hi_benefits",
            "t_e2e_benefits_hi",
            "t_e2e_benefits_en",
            "t_e2e_benefits_ta",
            "t_e2e_benefits_te",
            "t_e2e_benefits_bn",
        ],
        hours=12,
    )
    return changes, ground


def build_coverage(tests: list[dict], rng: random.Random) -> list[dict]:
    coverage = []
    stale_ids = {"t_a11y_home", "t_unit_search"}
    captured = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    for test in tests:
        files = list(test["_files"])
        extra = rng.sample(FILES, k=0)
        coverage.append(
            {
                "test_id": test["test_id"],
                "files": sorted(set(files + extra)),
                "captured_at": captured.isoformat(),
                "stale": test["test_id"] in stale_ids,
            }
        )
    return coverage


def build_failures(tests: list[dict], changes: list[dict], ground: dict[str, list[str]], rng: random.Random) -> list[dict]:
    events = []
    stamp = datetime(2026, 8, 1, tzinfo=timezone.utc)
    flaky = {"t_e2e_benefits_ta", "t_int_appointments"}
    by_id = {t["test_id"]: t for t in tests if t["org_id"] == ORG}
    for i, change in enumerate(changes):
        affected = set(ground[change["change_id"]])
        for test_id, test in by_id.items():
            failed = test_id in affected and rng.random() < 0.55
            is_flaky = test_id in flaky and rng.random() < 0.4
            if not failed and not is_flaky and rng.random() > 0.08:
                continue
            related = []
            if test_id in affected:
                related = [f["path"] for f in change["files"]]
            events.append(
                {
                    "test_id": test_id,
                    "change_id": change["change_id"],
                    "failed": failed or (is_flaky and rng.random() < 0.5),
                    "flaky": is_flaky,
                    "related_files": related,
                    "timestamp": (stamp + timedelta(days=i, hours=rng.randint(0, 10))).isoformat(),
                }
            )
    return events


def strip_private(tests: list[dict]) -> list[dict]:
    out = []
    for test in tests:
        item = {k: v for k, v in test.items() if not k.startswith("_")}
        out.append(item)
    return out


def main() -> None:
    rng = random.Random(SEED)
    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    tests = build_tests()
    changes, ground = build_changes(rng)
    coverage = build_coverage(tests, rng)
    failures = build_failures(tests, changes, ground, rng)
    dependencies = [{"source": src, "dependents": deps} for src, deps in DEPENDENCY_MAP.items()]

    (data_dir / "tests.json").write_text(json.dumps(strip_private(tests), indent=2), encoding="utf-8")
    (data_dir / "changes.json").write_text(json.dumps(changes, indent=2), encoding="utf-8")
    (data_dir / "coverage.json").write_text(json.dumps(coverage, indent=2), encoding="utf-8")
    (data_dir / "dependencies.json").write_text(json.dumps(dependencies, indent=2), encoding="utf-8")
    (data_dir / "failures.json").write_text(json.dumps(failures, indent=2), encoding="utf-8")
    (data_dir / "ground_truth.json").write_text(json.dumps(ground, indent=2), encoding="utf-8")
    print(f"Wrote dataset to {data_dir}")
    print(f"tests={len(tests)} changes={len(changes)} coverage={len(coverage)} failures={len(failures)}")


if __name__ == "__main__":
    main()
