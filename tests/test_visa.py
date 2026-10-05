from datetime import date
from pathlib import Path

import pytest
import yaml

from jobapply.config import SalaryThreshold, VisaConfig
from jobapply.visa.checker import VisaVerdict, age_adjusted_threshold, check_visa

BEFORE_2027 = date(2026, 9, 1)
AFTER_2027 = date(2027, 1, 1)


@pytest.fixture(scope="module")
def visa(request: pytest.FixtureRequest) -> VisaConfig:
    root = Path(request.config.rootpath)
    return VisaConfig.model_validate(yaml.safe_load((root / "config" / "visa.yaml").read_text()))


def check(
    visa: VisaConfig,
    *,
    birth_year: int = 2002,
    start: date = BEFORE_2027,
    sector: str = "other",
    low: int | None = None,
    high: int | None = None,
):
    return check_visa(
        visa,
        birth_year=birth_year,
        start_date=start,
        sector=sector,  # type: ignore[arg-type]
        salary_min_sgd=low,
        salary_max_sgd=high,
    )


# --- Threshold by sector, age and date --------------------------------------------------


@pytest.mark.parametrize(
    ("sector", "birth_year", "start", "expected"),
    [
        ("other", 2003, BEFORE_2027, 5600),  # 23 at start: base
        ("other", 2010, BEFORE_2027, 5600),  # younger than base age: base
        ("financial_services", 2003, BEFORE_2027, 6200),
        ("other", 1981, BEFORE_2027, 10700),  # exactly 45
        ("other", 1970, BEFORE_2027, 10700),  # older than 45
        ("financial_services", 1981, BEFORE_2027, 11800),
        ("other", 1992, BEFORE_2027, 8150),  # 34: 5600 + 5100 * 11/22
        ("financial_services", 2002, BEFORE_2027, 6455),  # 24: 6200 + 5600/22, rounded up
        ("other", 2004, AFTER_2027, 6000),  # 2027 thresholds
        ("financial_services", 2004, AFTER_2027, 6600),
        ("other", 1970, AFTER_2027, 6000),  # no 45+ value configured for 2027
    ],
)
def test_threshold(visa: VisaConfig, sector: str, birth_year: int, start: date, expected: int):
    assert check(visa, birth_year=birth_year, start=start, sector=sector).threshold_sgd == expected


def test_switch_to_2027_thresholds(visa: VisaConfig) -> None:
    last_day = check(visa, birth_year=2003, start=date(2026, 12, 31))
    first_day = check(visa, birth_year=2004, start=AFTER_2027)
    assert last_day.thresholds_effective_from == date(2026, 1, 1)
    assert last_day.threshold_sgd == 5600
    assert first_day.thresholds_effective_from == date(2027, 1, 1)
    assert first_day.threshold_sgd == 6000


def test_age_is_computed_conservatively(visa: VisaConfig) -> None:
    assert check(visa, birth_year=2002, start=date(2026, 1, 1)).age_at_start == 24


def test_age_adjusted_threshold_without_45_plus() -> None:
    assert age_adjusted_threshold(SalaryThreshold(base=6000), 40, 23, 45) == 6000


# --- Verdicts -----------------------------------------------------------------------------
# Candidate born 2003, starting 2026-09: threshold 5600 ("other").


@pytest.mark.parametrize(
    ("low", "high", "verdict"),
    [
        (None, None, VisaVerdict.UNKNOWN),
        (3800, 4500, VisaVerdict.INCOMPATIBLE),
        (5000, 5599, VisaVerdict.INCOMPATIBLE),  # max just below
        (5000, 5600, VisaVerdict.AT_RISK),  # max exactly at threshold
        (5000, 7000, VisaVerdict.AT_RISK),
        (5600, 7000, VisaVerdict.OK),  # min exactly at threshold
        (7000, 9000, VisaVerdict.OK),
        (5000, None, VisaVerdict.AT_RISK),  # "from 5000"
        (6000, None, VisaVerdict.OK),  # "from 6000"
        (None, 5000, VisaVerdict.INCOMPATIBLE),  # "up to 5000"
        (None, 7000, VisaVerdict.AT_RISK),  # "up to 7000"
    ],
)
def test_verdict(visa: VisaConfig, low: int | None, high: int | None, verdict: VisaVerdict):
    result = check(visa, birth_year=2003, low=low, high=high)
    assert result.verdict is verdict
    assert (result.salary_min_sgd, result.salary_max_sgd) == (low, high)


def test_sector_changes_verdict(visa: VisaConfig) -> None:
    assert check(visa, birth_year=2003, low=6000, high=6100).verdict is VisaVerdict.OK
    finance = check(visa, birth_year=2003, sector="financial_services", low=6000, high=6100)
    assert finance.verdict is VisaVerdict.INCOMPATIBLE
    assert finance.sector == "financial_services"


def test_age_changes_verdict(visa: VisaConfig) -> None:
    assert check(visa, birth_year=2003, low=6000, high=7000).verdict is VisaVerdict.OK
    assert check(visa, birth_year=1990, low=6000, high=7000).verdict is VisaVerdict.INCOMPATIBLE


def test_2027_switch_changes_verdict(visa: VisaConfig) -> None:
    assert check(visa, birth_year=2004, low=5800, high=5900).verdict is VisaVerdict.OK
    later = check(visa, birth_year=2004, start=AFTER_2027, low=5800, high=5900)
    assert later.verdict is VisaVerdict.INCOMPATIBLE


# --- COMPASS and notes --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("low", "high", "exempt"),
    [
        (22500, 25000, True),
        (None, None, None),
        (7000, 9000, False),
        (None, 9000, False),  # max below the exemption salary
        (None, 30000, None),  # "up to 30000": not settled
    ],
)
def test_compass_exemption(
    visa: VisaConfig, low: int | None, high: int | None, exempt: bool | None
):
    result = check(visa, low=low, high=high)
    assert result.compass_exempt is exempt
    assert any("COMPASS" in n for n in result.notes) is (exempt is False)


def test_notes(visa: VisaConfig) -> None:
    unknown = check(visa)
    assert any("Salaire non indiqué" in n for n in unknown.notes)
    assert any("2027-01-01" in n and "6000" in n for n in unknown.notes)

    after = check(visa, birth_year=2002, start=AFTER_2027, low=7000, high=8000)
    assert not any("2027-01-01" in n for n in after.notes)
    assert any("45+ non configuré" in n for n in after.notes)

    young = check(visa, birth_year=2004, start=AFTER_2027, low=7000, high=8000)
    assert not any("45+" in n for n in young.notes)


def test_without_upcoming_thresholds(visa: VisaConfig) -> None:
    cfg = visa.model_copy(update={"ep": visa.ep.model_copy(update={"upcoming": None})})
    result = check(cfg, start=date(2028, 1, 1), low=7000, high=8000)
    assert result.thresholds_effective_from == date(2026, 1, 1)
    assert not any("Nouveaux seuils" in n for n in result.notes)
