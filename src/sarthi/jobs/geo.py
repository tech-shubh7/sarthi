# Adapted from StillOpen (Apache-2.0), tracker/services/geo.py @ ce7fb22
# Copyright 2026 Zahid Hasan and the StillOpen contributors. Modifications (c) Sarthi contributors.
# Changes: removed Django-bound fill_details(); added type annotations; split resolve_place() helper;
# added more Indian cities; location_codes() resolves 'Place, XX' endings by place first (was: any
# state-like code meant US, so 'Toronto, CA' became US+CA); added Indianapolis and the Zürich spelling.
"""Places and posting details, with no external service.

resolve_place(text)  ->  set of ISO country codes, {"*"} for "anywhere", or None when the place is not recognised.
detect(title, text)  ->  guesses for a posting's location, work arrangement, sponsorship, relocation, type and seniority.

Coverage is a built-in table of the countries, regions and cities job seekers use most. An unrecognised place is
reported as such rather than guessed. Detection only suggests: the person can always correct it.
"""

import re

from sarthi.jobs.countries import ALL as _ISO_NAMES
from sarthi.jobs.countries import name as _iso_country_name

_ISO_CODES = frozenset(_ISO_NAMES)
_Place = tuple[re.Pattern[str], str, str, str]
_Hit = tuple[str, str, int]

# ISO code: (name, aliases). Aliases listed in CASE_SENSITIVE only match in that exact case (US, UK, UAE).
COUNTRIES = {
    "BD": ("Bangladesh", []),
    "IN": ("India", []),
    "PK": ("Pakistan", []),
    "LK": ("Sri Lanka", []),
    "NP": ("Nepal", []),
    "MY": ("Malaysia", []),
    "SG": ("Singapore", []),
    "ID": ("Indonesia", []),
    "TH": ("Thailand", []),
    "VN": ("Vietnam", ["Viet Nam"]),
    "PH": ("Philippines", []),
    "JP": ("Japan", []),
    "KR": ("South Korea", ["Korea", "Republic of Korea"]),
    "CN": ("China", []),
    "HK": ("Hong Kong", []),
    "TW": ("Taiwan", []),
    "AU": ("Australia", []),
    "NZ": ("New Zealand", []),
    "AE": ("United Arab Emirates", ["UAE", "Emirates"]),
    "SA": ("Saudi Arabia", ["KSA"]),
    "QA": ("Qatar", []),
    "KW": ("Kuwait", []),
    "BH": ("Bahrain", []),
    "OM": ("Oman", []),
    "JO": ("Jordan", []),
    "LB": ("Lebanon", []),
    "IL": ("Israel", []),
    "TR": ("Turkey", ["Turkiye", "Türkiye"]),
    "EG": ("Egypt", []),
    "MA": ("Morocco", []),
    "TN": ("Tunisia", []),
    "NG": ("Nigeria", []),
    "GH": ("Ghana", []),
    "KE": ("Kenya", []),
    "ZA": ("South Africa", []),
    "ET": ("Ethiopia", []),
    "RW": ("Rwanda", []),
    "TZ": ("Tanzania", []),
    "UG": ("Uganda", []),
    "GB": (
        "United Kingdom",
        ["UK", "Great Britain", "Britain", "England", "Scotland", "Wales", "Northern Ireland"],
    ),
    "IE": ("Ireland", []),
    "DE": ("Germany", []),
    "FR": ("France", []),
    "ES": ("Spain", []),
    "PT": ("Portugal", []),
    "IT": ("Italy", []),
    "NL": ("Netherlands", ["Holland"]),
    "BE": ("Belgium", []),
    "LU": ("Luxembourg", []),
    "CH": ("Switzerland", []),
    "AT": ("Austria", []),
    "SE": ("Sweden", []),
    "NO": ("Norway", []),
    "DK": ("Denmark", []),
    "FI": ("Finland", []),
    "IS": ("Iceland", []),
    "PL": ("Poland", []),
    "CZ": ("Czech Republic", ["Czechia"]),
    "SK": ("Slovakia", []),
    "HU": ("Hungary", []),
    "RO": ("Romania", []),
    "BG": ("Bulgaria", []),
    "GR": ("Greece", []),
    "HR": ("Croatia", []),
    "RS": ("Serbia", []),
    "SI": ("Slovenia", []),
    "EE": ("Estonia", []),
    "LV": ("Latvia", []),
    "LT": ("Lithuania", []),
    "UA": ("Ukraine", []),
    "CY": ("Cyprus", []),
    "MT": ("Malta", []),
    "US": ("United States", ["USA", "US", "U.S.", "U.S.A.", "America"]),
    "CA": ("Canada", []),
    "MX": ("Mexico", []),
    "BR": ("Brazil", []),
    "AR": ("Argentina", []),
    "CL": ("Chile", []),
    "CO": ("Colombia", []),
    "PE": ("Peru", []),
    "UY": ("Uruguay", []),
    "CR": ("Costa Rica", []),
    "PA": ("Panama", []),
    "RU": ("Russia", []),
}
CASE_SENSITIVE = {"UK", "US", "UAE", "USA", "KSA", "U.S.", "U.S.A."}

_GULF = {"AE", "SA", "QA", "KW", "BH", "OM"}
_MIDDLE_EAST = _GULF | {"JO", "LB", "IL", "TR", "EG"}
_EUROPE = {
    "GB",
    "IE",
    "DE",
    "FR",
    "ES",
    "PT",
    "IT",
    "NL",
    "BE",
    "LU",
    "CH",
    "AT",
    "SE",
    "NO",
    "DK",
    "FI",
    "IS",
    "PL",
    "CZ",
    "SK",
    "HU",
    "RO",
    "BG",
    "GR",
    "HR",
    "RS",
    "SI",
    "EE",
    "LV",
    "LT",
    "UA",
    "CY",
    "MT",
    "RU",
}
_EU = _EUROPE - {"GB", "CH", "NO", "IS", "UA", "RS", "RU"}
_SEA = {"MY", "SG", "ID", "TH", "VN", "PH"}
_SOUTH_ASIA = {"BD", "IN", "PK", "LK", "NP"}
_EAST_ASIA = {"JP", "KR", "CN", "HK", "TW"}
_ASIA = _SEA | _SOUTH_ASIA | _EAST_ASIA
_AFRICA = {"EG", "MA", "TN", "NG", "GH", "KE", "ZA", "ET", "RW", "TZ", "UG"}
_LATAM = {"MX", "BR", "AR", "CL", "CO", "PE", "UY", "CR", "PA"}

REGIONS = {
    "gulf": _GULF,
    "gcc": _GULF,
    "middle east": _MIDDLE_EAST,
    "mena": _MIDDLE_EAST | {"MA", "TN"},
    "europe": _EUROPE,
    "eu": _EU,
    "european union": _EU,
    "asia": _ASIA,
    "southeast asia": _SEA,
    "south east asia": _SEA,
    "sea": _SEA,
    "south asia": _SOUTH_ASIA,
    "east asia": _EAST_ASIA,
    "apac": _ASIA | {"AU", "NZ"},
    "asia pacific": _ASIA | {"AU", "NZ"},
    "africa": _AFRICA,
    "north america": {"US", "CA", "MX"},
    "latin america": _LATAM,
    "latam": _LATAM,
    "oceania": {"AU", "NZ"},
    "emea": _EUROPE | _MIDDLE_EAST | _AFRICA,
}
ANYWHERE = {
    "anywhere",
    "worldwide",
    "world wide",
    "global",
    "globally",
    "everywhere",
    "any country",
    "anywhere in the world",
    "remote worldwide",
    "remote anywhere",
    "work from anywhere",
}

CITIES = {
    "BD": ["Dhaka", "Chittagong", "Chattogram", "Sylhet", "Khulna", "Rajshahi", "Gazipur", "Narayanganj"],
    "MY": [
        "Kuala Lumpur",
        "Penang",
        "George Town",
        "Johor Bahru",
        "Cyberjaya",
        "Petaling Jaya",
        "Selangor",
        "Putrajaya",
        "Kota Kinabalu",
        "Kuching",
    ],
    "SG": [],
    "AE": ["Dubai", "Abu Dhabi", "Sharjah", "Ajman", "Ras Al Khaimah"],
    "SA": ["Riyadh", "Jeddah", "Dammam", "Khobar", "Mecca"],
    "QA": ["Doha"],
    "KW": ["Kuwait City"],
    "BH": ["Manama"],
    "OM": ["Muscat"],
    "JO": ["Amman"],
    "LB": ["Beirut"],
    "IN": [
        "Bangalore",
        "Bengaluru",
        "Mumbai",
        "New Delhi",
        "Delhi",
        "Hyderabad",
        "Chennai",
        "Pune",
        "Gurgaon",
        "Gurugram",
        "Noida",
        "Kolkata",
        "Ahmedabad",
        "Gandhinagar",
        "Vadodara",
        "Surat",
        "Jaipur",
        "Chandigarh",
        "Mohali",
        "Indore",
        "Kochi",
        "Thiruvananthapuram",
        "Coimbatore",
        "Mysuru",
        "Bhubaneswar",
        "Nagpur",
        "Lucknow",
        "Visakhapatnam",
    ],
    "PK": ["Karachi", "Lahore", "Islamabad", "Rawalpindi"],
    "LK": ["Colombo"],
    "NP": ["Kathmandu"],
    "ID": ["Jakarta", "Bali", "Surabaya", "Bandung"],
    "TH": ["Bangkok", "Chiang Mai", "Phuket"],
    "VN": ["Hanoi", "Ho Chi Minh City", "Ho Chi Minh", "Saigon", "Da Nang"],
    "PH": ["Manila", "Makati", "Cebu", "Taguig"],
    "JP": ["Tokyo", "Osaka", "Kyoto", "Yokohama"],
    "KR": ["Seoul", "Busan"],
    "CN": ["Shanghai", "Beijing", "Shenzhen", "Guangzhou", "Hangzhou"],
    "TW": ["Taipei"],
    "AU": ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide", "Canberra"],
    "NZ": ["Auckland", "Wellington"],
    "GB": ["London", "Manchester", "Edinburgh", "Birmingham", "Bristol", "Glasgow", "Leeds", "Cambridge"],
    "IE": ["Dublin", "Cork"],
    "DE": ["Berlin", "Munich", "Hamburg", "Frankfurt", "Cologne"],
    "FR": ["Paris", "Lyon"],
    "ES": ["Madrid", "Barcelona", "Valencia"],
    "PT": ["Lisbon", "Porto"],
    "IT": ["Milan", "Rome"],
    "NL": ["Amsterdam", "Rotterdam", "Eindhoven", "The Hague"],
    "BE": ["Brussels"],
    "CH": ["Zurich", "Zürich", "Geneva", "Basel"],
    "AT": ["Vienna"],
    "SE": ["Stockholm", "Gothenburg"],
    "NO": ["Oslo"],
    "DK": ["Copenhagen"],
    "FI": ["Helsinki"],
    "PL": ["Warsaw", "Krakow", "Wroclaw", "Gdansk"],
    "CZ": ["Prague", "Brno"],
    "HU": ["Budapest"],
    "RO": ["Bucharest", "Cluj"],
    "BG": ["Sofia"],
    "GR": ["Athens"],
    "EE": ["Tallinn"],
    "LV": ["Riga"],
    "LT": ["Vilnius"],
    "UA": ["Kyiv", "Kiev"],
    "TR": ["Istanbul", "Ankara"],
    "EG": ["Cairo"],
    "MA": ["Casablanca"],
    "NG": ["Lagos"],
    "KE": ["Nairobi"],
    "ZA": ["Johannesburg", "Cape Town"],
    "GH": ["Accra"],
    "RW": ["Kigali"],
    "US": [
        "Indianapolis",
        "New York",
        "San Francisco",
        "Seattle",
        "Austin",
        "Boston",
        "Chicago",
        "Los Angeles",
        "Atlanta",
        "Denver",
        "Washington DC",
        "San Jose",
        "Palo Alto",
        "Mountain View",
        "Dallas",
        "Miami",
    ],
    "CA": ["Toronto", "Vancouver", "Montreal", "Ottawa", "Calgary"],
    "MX": ["Mexico City"],
    "BR": ["Sao Paulo", "São Paulo", "Rio de Janeiro"],
    "AR": ["Buenos Aires"],
    "CL": ["Santiago"],
    "CO": ["Bogota", "Bogotá", "Medellin"],
    "PE": ["Lima"],
}


def _build() -> list[_Place]:
    patterns: list[tuple[str, str, str]] = []
    for code, (name, aliases) in COUNTRIES.items():
        for alias in [name, *aliases]:
            patterns.append((alias, code, "country"))
    for code, cities in CITIES.items():
        for city in cities:
            patterns.append((city, code, "city"))
    compiled: list[_Place] = []
    for text, code, kind in sorted(
        patterns, key=lambda p: -len(p[0])
    ):  # longest first: "Kuala Lumpur" before "Lumpur"
        flags = 0 if text in CASE_SENSITIVE else re.IGNORECASE
        compiled.append(
            (re.compile(r"(?<![A-Za-z])" + re.escape(text) + r"(?![A-Za-z])", flags), code, kind, text)
        )
    return compiled


_PLACES = _build()


def _combine() -> list[tuple[re.Pattern[str], list[int]]]:
    """One regex per case rule instead of ~290 separate searches: a posting's text is scanned once, not once per place name.

    Same answer as trying each name in turn (earliest position wins; at the same position the earlier name in the list wins).
    """
    groups: dict[int, list[int]] = {}
    for index, (pattern, _, _, _) in enumerate(_PLACES):
        groups.setdefault(pattern.flags, []).append(index)
    combined: list[tuple[re.Pattern[str], list[int]]] = []
    for flags, indexes in groups.items():
        body = "|".join(
            "(" + _PLACES[i][0].pattern[len(r"(?<![A-Za-z])") : -len(r"(?![A-Za-z])")] + ")" for i in indexes
        )
        combined.append((re.compile(r"(?<![A-Za-z])(?:" + body + r")(?![A-Za-z])", flags), indexes))
    return combined


_COMBINED = _combine()


def _exact_names() -> tuple[dict[str, str], dict[str, str]]:
    """Whole-string lookups ("Dhaka", "Malaysia") by dictionary instead of trying every pattern; first listed wins."""
    folded: dict[str, str] = {}
    exact: dict[str, str] = {}
    for pattern, code, _, text in _PLACES:
        if pattern.flags & re.IGNORECASE:
            folded.setdefault(text.lower(), code)
        else:
            exact.setdefault(text, code)
    return folded, exact


_FOLDED, _EXACT = _exact_names()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower().replace("-", " ")).strip(" .")


def _resolve_part(part: str) -> set[str] | None:
    """Codes for one comma/slash-separated piece of a place, or None."""
    key = _norm(part)
    if key in ANYWHERE:
        return {"*"}
    if key in REGIONS:
        return set(REGIONS[key])
    stripped = part.strip()
    code = _EXACT.get(stripped) or _FOLDED.get(stripped.lower())
    return {code} if code else None


def resolve_place(text: str) -> set[str] | None:
    """Country codes a place stands for. {"*"} = anywhere. None = not recognised."""
    cleaned = _norm(text or "")
    if not cleaned:
        return None
    if cleaned in ANYWHERE:
        return {"*"}
    if cleaned in REGIONS:
        return set(REGIONS[cleaned])
    # "City, Country": the last part is the most specific statement about the country
    for part in reversed([p for p in re.split(r"[,/|()]|\s-\s|–|—", text) if p.strip()]):
        codes = _resolve_part(part)
        if codes:
            return codes
    hit = _first_place(text)
    return {hit[0]} if hit else None


def first_country(text: str) -> str | None:
    """The first country or city named anywhere in ``text`` ("I moved to Dubai" -> "AE"), or None."""
    hit = _first_place(text)
    return hit[0] if hit else None


def _first_place(text: str) -> _Hit | None:
    """(code, matched text, start) of the earliest recognised country or city in `text`."""
    best: _Hit | None = None
    best_key: tuple[int, int] | None = None
    for regex, indexes in _COMBINED:
        m = regex.search(text)
        if m and m.lastindex:
            index = indexes[m.lastindex - 1]
            key = (m.start(), index)
            if best_key is None or key < best_key:
                best, best_key = (_PLACES[index][1], m.group(0), m.start()), key
    return best


def _first_place_slow(text: str) -> _Hit | None:
    """The plain one-name-at-a-time version, kept so a test can prove the fast one gives the same answers."""
    best: _Hit | None = None
    for pattern, code, _, _name in _PLACES:
        m = pattern.search(text)
        if m and (best is None or m.start() < best[2]):
            best = (code, m.group(0), m.start())
    return best


def country_name(code: str) -> str:
    """Display name for an ISO 3166-1 alpha-2 code; the code itself when unknown."""
    code = (code or "").upper()
    if code in COUNTRIES:
        return COUNTRIES[code][0]
    return _iso_country_name(code)


# --- detection from posting text ---------------------------------------------------------------------------------

_NEGATION = re.compile(r"(?:\bnot\b|\bno\b|\bnon\b|n't|\bwithout\b)[\s-]*(?:\w+\s+){0,2}$", re.I)


def _positive(pattern: str, text: str) -> bool:
    for m in re.finditer(pattern, text, re.I):
        if not _NEGATION.search(text[max(0, m.start() - 24) : m.start()]):
            return True
    return False


def _arrangement(text: str) -> str:
    hybrid = _positive(r"\bhybrid\b", text)
    remote = _positive(
        r"\b(?:fully[- ]|100%[- ]|remote[- ]first|work from home|work from anywhere|wfh|distributed team)|\bremote\b",
        text,
    )
    onsite = _positive(r"\bon[- ]?site\b|\bin[- ]office\b|\boffice[- ]based\b|\bin[- ]person\b", text)
    if hybrid:
        return "hybrid"
    if remote and not onsite:
        return "remote"
    if onsite and not remote:
        return "onsite"
    return ""


def _location(text: str) -> tuple[str, str]:
    """(display text, country code). A 'Location:' line is trusted most; else the earliest place in the opening."""
    m = re.search(
        r"(?:^|\n)\s*(?:location|based in|office location|work location)\s*[:\-]\s*([^\n.;|]{2,80})",
        text,
        re.I,
    )
    if m:
        place = m.group(1).strip().rstrip(".")
        codes = resolve_place(place)
        if codes and "*" not in codes and len(codes) == 1:
            return place, next(iter(codes))
        if codes and "*" in codes:
            return place, ""
    hit = _first_place(text[:2500])
    return (hit[1], hit[0]) if hit else ("", "")


_US_STATES = "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
_US_STATE_CODES = frozenset(_US_STATES.split("|"))
_ALPHA3 = {
    "USA": "US", "CAN": "CA", "GBR": "GB", "IND": "IN", "DEU": "DE", "FRA": "FR", "AUS": "AU", "NLD": "NL",
    "IRL": "IE", "SGP": "SG", "CHE": "CH", "ESP": "ES", "ITA": "IT", "BRA": "BR", "MEX": "MX", "JPN": "JP",
    "POL": "PL", "SWE": "SE", "ARE": "AE", "ISR": "IL", "NZL": "NZ", "ZAF": "ZA", "PRT": "PT", "BEL": "BE",
}  # fmt: skip
_WORK_MODE_ONLY = re.compile(r"(?i)^\s*(?:remote|hybrid|on-?site)\s*$")


def _code_after_comma(segment: str) -> set[str]:
    """Countries for a 'Place, XX' / 'Place, XXX' ending; empty when the ending is not a code.

    A known place before the comma wins ('Toronto, CA' is Canada); otherwise a US state code means the US
    ('Palo Alto, CA'), unless only a work mode precedes it ('Remote, DE' is Germany); otherwise an ISO code.
    """
    head, comma, tail = segment.rpartition(",")
    code = tail.strip(" )]")
    if not comma or not re.fullmatch(r"[A-Z]{2,3}", code):
        return set()
    if len(code) == 3:  # noqa: PLR2004  (ISO 3166-1 alpha-3)
        return {_ALPHA3[code]} if code in _ALPHA3 else set()
    place = resolve_place(head)
    if place and "*" not in place:
        return set(place)
    if code in _US_STATE_CODES and not _WORK_MODE_ONLY.match(head):
        return {"US"}
    return {code} if code in _ISO_CODES else set()


_US_STATE_NAMES = frozenset(
    [
        "alabama",
        "alaska",
        "arizona",
        "arkansas",
        "california",
        "colorado",
        "connecticut",
        "delaware",
        "florida",
        "hawaii",
        "idaho",
        "illinois",
        "indiana",
        "iowa",
        "kansas",
        "kentucky",
        "louisiana",
        "maine",
        "maryland",
        "massachusetts",
        "michigan",
        "minnesota",
        "mississippi",
        "missouri",
        "montana",
        "nebraska",
        "nevada",
        "new hampshire",
        "new jersey",
        "new mexico",
        "new york",
        "north carolina",
        "north dakota",
        "ohio",
        "oklahoma",
        "oregon",
        "pennsylvania",
        "rhode island",
        "south carolina",
        "south dakota",
        "tennessee",
        "texas",
        "utah",
        "vermont",
        "virginia",
        "washington",
        "west virginia",
        "wisconsin",
        "wyoming",
    ]
)


_MIN_PLACE_CHARS = 2


def location_codes(location: str) -> set[str]:
    """The countries a job-board LOCATION field names.

    Examples: 'Bastrop, TX' -> {US}, 'Hong Kong; Singapore' -> {HK, SG}, 'Remote - European Union' -> the EU's
    countries, 'Remote, Global' -> nothing. That field is structured and reliable; prose in a description (a company
    calling itself "global") is not. Never guesses: a place we cannot place adds nothing.
    """
    location = re.sub(r"\bU\.?S\.?A?\.?(?=[\s,)\]]|$)", "United States", (location or "").strip())
    if not location:
        return set()
    codes: set[str] = set()
    for segment in re.split(r"[;|/]", location):
        codes |= _code_after_comma(segment)
    parts = [p for p in re.split(r"[;|/]|\s[-\u2013\u2014]\s|,|\band\b", location) if p.strip()]
    for part in [location, *parts]:
        cleaned = re.sub(r"[()\[\]]|\bremote\b|\bhybrid\b|\bon-?site\b", " ", part, flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -,:")
        if len(cleaned) < _MIN_PLACE_CHARS:
            continue
        if cleaned.lower() in _US_STATE_NAMES:
            codes.add("US")
            continue
        resolved = resolve_place(cleaned)
        if resolved and "*" not in resolved:
            codes |= set(resolved)
    return codes


_LEVELLED = r"\b(?:engineer|developer|sde|swe|programmer|analyst|scientist|consultant)\s*[-\u2013(]?\s*"
_NOT_YEARS = r"(?!\s*(?:[-\u2013+]|to\b|\(|years?\b|yrs?\b))"  # "Engineer 5-8 yrs" is years, not level 5
_SENIORITY = [
    ("manager", r"\b(?:manager|head of|director|vp\b)"),
    ("principal", r"\b(?:principal|architect|distinguished)\b"),
    ("lead", r"\b(?:lead|leader|staff|team lead|tech lead)\b"),
    (
        "senior",
        r"\b(?:senior|sr\.?)(?=\W|$)|"
        + _LEVELLED
        + r"(?:iii\b|iv\b|v\b|[345]\b"
        + _NOT_YEARS
        + ")"
        + r"|\bl[5-7]\b",
    ),
    (
        "junior",
        r"\b(?:intern|junior|jr\.?|entry[- ]level|graduate|new grad|trainee|fresher|associate|assoc\.?)(?=\W|$)|"
        + _LEVELLED
        + r"(?:i|1)\b(?!\.)",
    ),
    ("mid", r"\b(?:mid[- ]level|intermediate)\b|" + _LEVELLED + r"(?:ii\b|2\b" + _NOT_YEARS + ")"),
]


def seniority_of(title: str) -> str | None:
    """The level a job title states (junior, mid, senior, lead, principal, manager), or None.

    Reads words ("Senior", "Sr.", "Staff", "Associate") and level numbers ("SDE III", "Engineer 2", "L5").
    """
    for level, pattern in _SENIORITY:
        if re.search(pattern, title or "", re.I):
            return level
    return None


def detect_terms(title: str, text: str) -> dict[str, str]:
    """Visa sponsorship, relocation help, kind of contract and seniority, only when the posting says so."""
    text = text or ""
    title = title or ""
    whole = f"{title}\n{text}"
    found: dict[str, str] = {}
    if re.search(
        r"\b(?:no (?:visa )?sponsorship|unable to sponsor|cannot sponsor|can't sponsor|not (?:able to )?sponsor|without (?:visa )?sponsorship|"
        r"must (?:already )?(?:have|hold|possess) (?:the |valid )?(?:right|authori[sz]ation|work permit)|right to work in)\b",
        whole,
        re.I,
    ):
        found["sponsorship"] = "no"
    elif re.search(
        r"\b(?:visa sponsorship|sponsor(?:s|ship)? (?:your |a )?(?:work )?visa|we sponsor|work permit (?:is )?(?:provided|sponsored)|visa (?:support|assistance)|visa and relocation)\b",
        whole,
        re.I,
    ):
        found["sponsorship"] = "yes"
    if re.search(
        r"\b(?:no relocation|relocation (?:is )?not (?:provided|offered|available)|unable to (?:offer|provide) relocation)\b",
        whole,
        re.I,
    ):
        found["relocation"] = "no"
    elif re.search(
        r"\brelocation (?:package|assistance|support|bonus|allowance|help|provided|offered|available)\b|\b(?:help|assist)(?:ing)? (?:you )?(?:with )?relocat",
        whole,
        re.I,
    ):
        found["relocation"] = "yes"
    for kind, pattern in [
        ("internship", r"\bintern(?:ship)?\b"),
        ("contract", r"\b(?:contract(?:or)?|fixed[- ]term)\b"),
        ("part_time", r"\bpart[- ]time\b"),
        ("freelance", r"\bfreelance\b"),
        ("full_time", r"\b(?:full[- ]time|permanent)\b"),
    ]:
        if re.search(pattern, title, re.I) or (
            kind != "internship" and re.search(pattern, text[:3000], re.I)
        ):
            found["employment_type"] = kind
            break
    if level := seniority_of(title):
        found["seniority"] = level
    return found


def detect(title: str, text: str) -> dict[str, str]:
    """Best guesses; only fields that were actually found are returned."""
    text = text or ""
    title = title or ""
    whole = f"{title}\n{text}"
    found: dict[str, str] = {}
    arrangement = _arrangement(whole)
    if arrangement:
        found["arrangement"] = arrangement
    place, code = _location(whole)
    anywhere = arrangement == "remote" and _positive(
        r"\b(?:worldwide|anywhere in the world|work from anywhere|global(?:ly)?)\b", whole
    )
    if anywhere and not code:
        found["location_text"] = "Remote (worldwide)"
    elif place:
        found["location_text"] = place[:160]
    if code and not anywhere:
        found["country"] = code
    found.update(detect_terms(title, text))
    return found
