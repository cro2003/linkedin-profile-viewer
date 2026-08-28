from datetime import datetime

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"


class DateParts(BaseModel):
    year: int | None = None
    month: int | None = None
    day: int | None = None


class DateRange(BaseModel):
    start: DateParts | None = None
    end: DateParts | None = None


class Experience(BaseModel):
    title: str | None = None
    company: str | None = None
    company_url: str | None = None
    company_logo: str | None = None
    location: str | None = None
    description: str | None = None
    employment_type: str | None = None
    date_range: DateRange | None = None
    is_current: bool = False


class Education(BaseModel):
    school: str | None = None
    school_url: str | None = None
    school_logo: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    grade: str | None = None
    activities: str | None = None
    description: str | None = None
    date_range: DateRange | None = None


class Skill(BaseModel):
    name: str
    endorsement_count: int | None = None


class Certification(BaseModel):
    name: str
    authority: str | None = None
    license_number: str | None = None
    url: str | None = None
    date_range: DateRange | None = None


class Language(BaseModel):
    name: str
    proficiency: str | None = None


class Profile(BaseModel):
    public_id: str
    url: str
    member_urn: str | None = None

    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    headline: str | None = None
    about: str | None = None

    location: str | None = None
    country_code: str | None = None
    industry: str | None = None

    profile_picture: str | None = None
    background_picture: str | None = None

    is_premium: bool = False
    is_influencer: bool = False
    is_creator: bool = False

    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    languages: list[Language] = Field(default_factory=list)


class Meta(BaseModel):
    fetched_at: datetime
    cache_hit: bool = False
    source: str = "api"
    schema_version: str = SCHEMA_VERSION
    # sections the upstream payload did not resolve, so the client can tell
    # "this person has no certifications" from "we could not read them"
    unavailable_sections: list[str] = Field(default_factory=list)
    partial_sections: list[str] = Field(default_factory=list)


class ProfileResponse(BaseModel):
    data: Profile
    meta: Meta
