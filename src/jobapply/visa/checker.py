"""Employment Pass compatibility check (SPEC section 7).

All thresholds come from config/visa.yaml; nothing is hard-coded here.

Age is computed as `start year - birth year`: the birth month is unknown, so this is the
oldest the candidate can be at start date, which gives the most conservative threshold.
"""

from __future__ import annotations

import math
from datetime import date
from enum import StrEnum

from pydantic import BaseModel

from jobapply.config import SalaryThreshold, VisaConfig
from jobapply.models.offer import Sector


class VisaVerdict(StrEnum):
    OK = "ok"
    AT_RISK = "at_risk"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


class VisaCheck(BaseModel):
    verdict: VisaVerdict
    threshold_sgd: int
    sector: Sector
    age_at_start: int
    start_date: date
    thresholds_effective_from: date
    salary_min_sgd: int | None
    salary_max_sgd: int | None
    compass_exempt: bool | None  # None when the salary does not settle it
    notes: list[str]


def _sector_threshold(cfg: VisaConfig, sector: Sector, start: date) -> tuple[SalaryThreshold, date]:
    upcoming = cfg.ep.upcoming
    if upcoming is not None and start >= upcoming.effective_from:
        return getattr(upcoming, sector), upcoming.effective_from
    return getattr(cfg.ep.min_salary_sgd, sector), cfg.effective_from


def age_adjusted_threshold(
    threshold: SalaryThreshold, age: int, base_until_age: int, max_from_age: int
) -> int:
    """Linear interpolation between the base and the 45+ threshold.

    TODO: replace with MOM's official age table.
    """
    if threshold.at_45_plus is None or age <= base_until_age:
        return threshold.base
    if age >= max_from_age:
        return threshold.at_45_plus
    ratio = (age - base_until_age) / (max_from_age - base_until_age)
    return math.ceil(threshold.base + (threshold.at_45_plus - threshold.base) * ratio)


def _verdict(low: int | None, high: int | None, threshold: int) -> VisaVerdict:
    if low is None and high is None:
        return VisaVerdict.UNKNOWN
    if high is not None and high < threshold:
        return VisaVerdict.INCOMPATIBLE
    if low is not None and low >= threshold:
        return VisaVerdict.OK
    # The range straddles the threshold, or only one bound is known and does not settle it.
    return VisaVerdict.AT_RISK


def _compass_exempt(low: int | None, high: int | None, exempt_salary: int) -> bool | None:
    if low is not None:
        return low >= exempt_salary
    if high is not None and high < exempt_salary:
        return False
    return None


def check_visa(
    cfg: VisaConfig,
    *,
    birth_year: int,
    start_date: date,
    sector: Sector,
    salary_min_sgd: int | None,
    salary_max_sgd: int | None,
) -> VisaCheck:
    sector_threshold, effective_from = _sector_threshold(cfg, sector, start_date)
    age = start_date.year - birth_year
    curve = cfg.ep.age_curve
    threshold = age_adjusted_threshold(
        sector_threshold, age, curve.base_until_age, curve.max_from_age
    )
    verdict = _verdict(salary_min_sgd, salary_max_sgd, threshold)

    notes: list[str] = []
    if sector_threshold.at_45_plus is None and age > curve.base_until_age:
        notes.append(
            "Seuil 45+ non configuré pour cette période : seuil de base appliqué sans "
            "ajustement d'âge."
        )
    upcoming = cfg.ep.upcoming
    if upcoming is not None and start_date < upcoming.effective_from:
        notes.append(
            f"Nouveaux seuils au {upcoming.effective_from.isoformat()} (base "
            f"{getattr(upcoming, sector).base} SGD) : ils s'appliqueront si le démarrage glisse "
            "après cette date."
        )
    if verdict is VisaVerdict.UNKNOWN:
        notes.append("Salaire non indiqué : à demander avant d'aller plus loin.")

    compass_exempt = _compass_exempt(
        salary_min_sgd, salary_max_sgd, cfg.ep.compass_exempt_salary_sgd
    )
    if compass_exempt is False:
        notes.append(f"COMPASS requis ({cfg.ep.compass_pass_mark} points) : score non calculé ici.")

    return VisaCheck(
        verdict=verdict,
        threshold_sgd=threshold,
        sector=sector,
        age_at_start=age,
        start_date=start_date,
        thresholds_effective_from=effective_from,
        salary_min_sgd=salary_min_sgd,
        salary_max_sgd=salary_max_sgd,
        compass_exempt=compass_exempt,
        notes=notes,
    )
