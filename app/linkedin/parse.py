"""Pure parsing of LinkedIn's normalised JSON into our response model.

The payload is `{data, meta, included[]}` where `included[]` is a flat list of
entities keyed by `entityUrn`, and any field named `*foo` holds a URN reference
to be resolved against that list. Nothing here does I/O.
"""

from app.models import (
    Certification,
    DateParts,
    DateRange,
    Education,
    Experience,
    Language,
    Profile,
    SectionInfo,
    Skill,
)
from app.urls import canonical_url

# collection references on the profile entity, and the section each one fills
COLLECTION_REFS = {
    "skills": "*profileSkills",
    "certifications": "*profileCertifications",
    "languages": "*profileLanguages",
}


class ProfileNotInPayload(ValueError):
    pass


def _index(payload: dict) -> dict:
    return {e["entityUrn"]: e for e in payload.get("included", []) if "entityUrn" in e}


def _of_type(payload: dict, suffix: str) -> list[dict]:
    return [e for e in payload.get("included", []) if e.get("$type", "").endswith(suffix)]


def _best_image(node: dict | None) -> str | None:
    """Vector images arrive as a rootUrl plus per-size path segments; pick the largest."""
    if not node:
        return None
    vector = node.get("vectorImage") or (node.get("displayImageReference") or {}).get("vectorImage")
    if not vector:
        return None
    root, artifacts = vector.get("rootUrl"), vector.get("artifacts") or []
    if not root or not artifacts:
        return None
    best = max(artifacts, key=lambda a: a.get("width") or 0)
    segment = best.get("fileIdentifyingUrlPathSegment")
    return f"{root}{segment}" if segment else None


def _date_parts(node: dict | None) -> DateParts | None:
    if not node:
        return None
    parts = DateParts(year=node.get("year"), month=node.get("month"), day=node.get("day"))
    return parts if (parts.year or parts.month or parts.day) else None


def _date_range(node: dict | None) -> DateRange | None:
    if not node:
        return None
    start, end = _date_parts(node.get("start")), _date_parts(node.get("end"))
    return DateRange(start=start, end=end) if (start or end) else None


def _sort_key(item) -> tuple:
    """Current roles first, then most recent; no start date sinks to the bottom.

    Matches how LinkedIn itself orders the section — sorting purely by start date
    floats board seats above the person's actual current job.
    """
    start = (item.date_range.start if item.date_range else None) or DateParts()
    return (1 if getattr(item, "is_current", False) else 0, start.year or 0, start.month or 0)


def _collection(index: dict, ref) -> list[dict]:
    """Resolve a `*collection` reference into its element entities."""
    if not isinstance(ref, str):
        return []
    node = index.get(ref) or {}
    elements = node.get("*elements") or node.get("elements") or []
    resolved = [index.get(e) if isinstance(e, str) else e for e in elements]
    return [e for e in resolved if e]


def _collection_total(index: dict, ref) -> int | None:
    """What LinkedIn says the collection holds, which can exceed what it returned."""
    if not isinstance(ref, str):
        return None
    paging = (index.get(ref) or {}).get("paging") or {}
    total = paging.get("total")
    return int(total) if isinstance(total, (int, float)) else None


def _experience(payload: dict, index: dict) -> list[Experience]:
    groups = {g.get("companyUrn"): g for g in _of_type(payload, "profile.PositionGroup")}
    out = []
    for pos in _of_type(payload, "profile.Position"):
        company_urn = pos.get("companyUrn") or pos.get("*company")
        company = index.get(company_urn) or {}
        # roles grouped under one employer can carry the name only on the group
        name = pos.get("companyName") or (groups.get(company_urn) or {}).get("companyName")
        date_range = _date_range(pos.get("dateRange"))
        out.append(Experience(
            title=pos.get("title"),
            company=name or company.get("name"),
            company_url=company.get("url"),
            company_logo=_best_image(company.get("logo")),
            location=pos.get("locationName"),
            description=pos.get("description"),
            employment_type=pos.get("employmentType"),
            date_range=date_range,
            is_current=bool(date_range and date_range.start and not date_range.end),
        ))
    return sorted(out, key=_sort_key, reverse=True)


def _education(payload: dict, index: dict) -> list[Education]:
    out = []
    for ed in _of_type(payload, "profile.Education"):
        school = index.get(ed.get("schoolUrn") or ed.get("*school")) or {}
        out.append(Education(
            school=ed.get("schoolName") or school.get("name"),
            school_url=school.get("url"),
            school_logo=_best_image(school.get("logo")),
            degree=ed.get("degreeName"),
            field_of_study=ed.get("fieldOfStudy"),
            grade=ed.get("grade"),
            activities=ed.get("activities"),
            description=ed.get("description"),
            date_range=_date_range(ed.get("dateRange")),
        ))
    return sorted(out, key=_sort_key, reverse=True)


def _skills(index: dict, profile: dict) -> list[Skill]:
    return [Skill(name=s["name"], endorsement_count=s.get("endorsementCount"))
            for s in _collection(index, profile.get("*profileSkills")) if s.get("name")]


def _certifications(index: dict, profile: dict) -> list[Certification]:
    return [Certification(
        name=c["name"],
        authority=c.get("authority"),
        license_number=c.get("licenseNumber"),
        url=c.get("url"),
        date_range=_date_range(c.get("dateRange")),
    ) for c in _collection(index, profile.get("*profileCertifications")) if c.get("name")]


def _languages(index: dict, profile: dict) -> list[Language]:
    return [Language(name=lang["name"], proficiency=lang.get("proficiency"))
            for lang in _collection(index, profile.get("*profileLanguages")) if lang.get("name")]


def parse_profile(payload: dict, public_id: str | None = None
                  ) -> tuple[Profile, dict[str, SectionInfo], list[str]]:
    """Returns (profile, section_info, partial_sections).

    A section that blows up is reported as partial rather than failing the whole
    profile — a caller would rather have nine fields than a 500.
    """
    index = _index(payload)
    entities = _of_type(payload, "identity.profile.Profile")
    if not entities:
        raise ProfileNotInPayload("no Profile entity in payload")
    prof = entities[0]

    pid = prof.get("publicIdentifier") or public_id
    if not pid:
        raise ProfileNotInPayload("payload has no publicIdentifier")

    geo_urn = (prof.get("geoLocation") or {}).get("geoUrn") or (prof.get("geoLocation") or {}).get("*geo")
    geo = index.get(geo_urn) or {}
    industry = index.get(prof.get("industryUrn") or "") or {}
    first, last = prof.get("firstName"), prof.get("lastName")

    profile = Profile(
        public_id=pid,
        url=canonical_url(pid),
        member_urn=prof.get("entityUrn"),
        first_name=first,
        last_name=last,
        full_name=" ".join(p for p in (first, last) if p) or None,
        headline=prof.get("headline"),
        about=prof.get("summary"),
        location=geo.get("defaultLocalizedName") or prof.get("locationName"),
        country_code=(prof.get("location") or {}).get("countryCode"),
        industry=industry.get("name"),
        profile_picture=_best_image(prof.get("profilePicture")),
        background_picture=_best_image(prof.get("backgroundPicture")),
        is_premium=bool(prof.get("premium")),
        is_influencer=bool(prof.get("influencer")),
        is_creator=bool(prof.get("creator")),
    )

    partial = []
    sections = {
        "experience": lambda: _experience(payload, index),
        "education": lambda: _education(payload, index),
        "skills": lambda: _skills(index, prof),
        "certifications": lambda: _certifications(index, prof),
        "languages": lambda: _languages(index, prof),
    }
    for name, build in sections.items():
        try:
            setattr(profile, name, build())
        except Exception:
            partial.append(name)

    sections: dict[str, SectionInfo] = {}
    for name in ("experience", "education"):
        returned = len(getattr(profile, name))
        sections[name] = SectionInfo(returned=returned, total=returned, complete=True)
    for name, ref in COLLECTION_REFS.items():
        returned = len(getattr(profile, name))
        total = _collection_total(index, prof.get(ref))
        sections[name] = SectionInfo(
            returned=returned,
            total=total,
            # only the first page of a collection comes back with the profile
            complete=(total is None or returned >= total),
        )
    return profile, sections, partial
