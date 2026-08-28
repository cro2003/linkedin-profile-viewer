import pytest

from app.urls import InvalidProfileURL, canonical_url, public_id_from_url

VALID = [
    ("https://www.linkedin.com/in/williamhgates", "williamhgates"),
    ("https://www.linkedin.com/in/williamhgates/", "williamhgates"),
    ("http://linkedin.com/in/williamhgates", "williamhgates"),
    ("www.linkedin.com/in/williamhgates", "williamhgates"),
    ("linkedin.com/in/williamhgates?originalSubdomain=in", "williamhgates"),
    ("https://m.linkedin.com/in/williamhgates", "williamhgates"),
    ("https://in.linkedin.com/in/test-user-1a2b3c4d", "test-user-1a2b3c4d"),
    ("https://www.linkedin.com/in/williamhgates/details/skills/", "williamhgates"),
    ("https://www.linkedin.com/en-us/in/williamhgates", "williamhgates"),
    ("https://www.linkedin.com/in/%C3%A9lodie-martin", "élodie-martin"),
    ("williamhgates", "williamhgates"),
]

INVALID = [
    "",
    "   ",
    "https://www.linkedin.com/company/microsoft",
    "https://www.linkedin.com/school/lakeside-school/",
    "https://www.linkedin.com/feed/",
    "https://www.linkedin.com/posts/williamhgates_activity-123",
    "https://example.com/in/williamhgates",
    "https://www.linkedin.com/",
    "https://www.linkedin.com/in/",
]


@pytest.mark.parametrize("url,expected", VALID)
def test_accepts(url, expected):
    assert public_id_from_url(url) == expected


@pytest.mark.parametrize("url", INVALID)
def test_rejects(url):
    with pytest.raises(InvalidProfileURL):
        public_id_from_url(url)


def test_canonical_url_round_trips():
    assert public_id_from_url(canonical_url("williamhgates")) == "williamhgates"
