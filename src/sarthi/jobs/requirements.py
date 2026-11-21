"""What a job's own text asks for, read without AI: the minimum years of experience.

The years pattern follows StillOpen's ``matchcard._YEARS`` (Apache-2.0), narrowed to phrases that mention
experience, so "10 years of company history" or "a 2 year contract" is not read as a requirement.
"""

from __future__ import annotations

import re

_YEARS_OF_EXPERIENCE = re.compile(
    r"\b(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|\u2013|to)\s*\d{1,2}\s*)?(?:years?|yrs?)\b"
    r"(?:\s+of)?(?:\s+[\w/+#.-]+){0,4}?\s+(?:experience|exp\b)",
    re.IGNORECASE,
)
_MAX_PLAUSIBLE_YEARS = 30
# Titles often carry the range themselves: "Java Support Engineer - (8-10) YRS", "Generative AI 5- 9yrs".
_YEARS_IN_TITLE = re.compile(
    r"(?<![\d.])(\d{1,2})\s*(?:\+|plus)?\s*\)?\s*(?:(?:-|\u2013|to)\s*\(?\d{1,2}\s*\)?\s*)?(?:years?|yrs?)(?![a-z])",
    re.IGNORECASE,
)


def min_years_in_title(title: str) -> int | None:
    """The years a job title itself asks for ("Data Scientist 5 To 12 Years" -> 5), or None."""
    years = [int(m.group(1)) for m in _YEARS_IN_TITLE.finditer(title or "")]
    plausible = [n for n in years if 0 < n <= _MAX_PLAUSIBLE_YEARS]
    return min(plausible) if plausible else None


def min_years_required(text: str) -> int | None:
    """The smallest "N years of experience" the text asks for, or None when it states none.

    The smallest is used so a nice-to-have "10+ years of leadership experience" never hides a job whose
    main requirement is "3+ years of Python experience".
    """
    years = [int(m.group(1)) for m in _YEARS_OF_EXPERIENCE.finditer(text or "")]
    plausible = [n for n in years if 0 < n <= _MAX_PLAUSIBLE_YEARS]
    return min(plausible) if plausible else None


# A job asking a little more than the user has is still worth showing: 1 year early in a career (1 year of
# experience vs "3+ years" is not a fit), 2 years later on.
EARLY_CAREER_YEARS = 3
# Typical minimum years for a level stated in a title ("SDE II", "Senior", "SDE III", "Staff", "Engineering
# Manager"). "II" asks for 2-3 years, so with the early-career slack it shows from 2 years. "Manager" is gated
# only for people who do not target manager roles themselves ("Product Manager" is a role, not a level).
LEVEL_MIN_YEARS: dict[str, int] = {"mid": 3, "senior": 4, "lead": 6, "manager": 6, "principal": 8}


def years_tolerance(years: float) -> int:
    """How many years above the user's experience a job may still ask for."""
    return 1 if years < EARLY_CAREER_YEARS else 2


def too_experienced_for(needed: float, years: float) -> bool:
    """True when a job asking for ``needed`` years is out of reach with ``years`` of experience."""
    return needed > years + years_tolerance(years)
