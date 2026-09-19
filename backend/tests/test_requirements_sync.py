"""requirements-lambda.txt is a hand-kept subset of requirements.txt. Lambda
must run the same versions CI tests, so a package listed in both has to
carry the same pin, and the Lambda file may not list anything the main file
doesn't (that would be an untested dependency).
"""

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PIN = re.compile(r"^([A-Za-z0-9_.\-]+)(?:\[[^\]]+\])?==(\S+)$")


def _pins(name: str) -> dict[str, str]:
    pins = {}
    for line in (BACKEND / name).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.match(line)
        assert match, f"{name}: not an exact pin: {line!r}"
        pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def test_lambda_requirements_are_a_pinned_subset_of_main():
    main, lam = _pins("requirements.txt"), _pins("requirements-lambda.txt")

    assert lam, "requirements-lambda.txt lists nothing"
    extra = set(lam) - set(main)
    assert not extra, f"in requirements-lambda.txt but not requirements.txt: {sorted(extra)}"
    drifted = {p: (lam[p], main[p]) for p in lam if lam[p] != main[p]}
    assert not drifted, f"version drift (lambda, main): {drifted}"


def test_lambda_requirements_leave_out_the_dashboard_packages():
    lam = _pins("requirements-lambda.txt")
    for dashboard_only in ("fastapi", "uvicorn", "jinja2", "python-multipart"):
        assert dashboard_only not in lam
