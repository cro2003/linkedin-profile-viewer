"""URL classification during login.

This is exactly where a sloppy substring match cost us a wasted login attempt:
LinkedIn returns a refused login as `/login/?errorKey=challenge_global_internal_error`,
which contains "challenge" but is not a verification page.
"""

import pytest

from app.session import _logged_out, is_challenge, login_error

REAL_CHALLENGES = [
    "https://www.linkedin.com/checkpoint/challenge/AQFLSLwTa1nvTQ?ut=2UVBqIgWA",
    "https://www.linkedin.com/checkpoint/lg/login-submit",
]

NOT_CHALLENGES = [
    "https://www.linkedin.com/flagship-web/login/?errorKey=challenge_global_internal_error",
    "https://www.linkedin.com/feed/",
    "https://www.linkedin.com/in/someone/",
    "https://www.linkedin.com/login?session_redirect=%2Fcheckpoint%2Fchallenge",
]


@pytest.mark.parametrize("url", REAL_CHALLENGES)
def test_detects_real_challenge(url):
    assert is_challenge(url) is True


@pytest.mark.parametrize("url", NOT_CHALLENGES)
def test_ignores_challenge_in_query_string(url):
    assert is_challenge(url) is False


def test_extracts_login_error_key():
    assert (
        login_error(
            "https://www.linkedin.com/flagship-web/login/?errorKey=challenge_global_internal_error"
        )
        == "challenge_global_internal_error"
    )
    assert login_error("https://www.linkedin.com/feed/") is None


def test_logged_out_matches_path_only():
    assert _logged_out("https://www.linkedin.com/uas/login?session_redirect=x") is True
    assert _logged_out("https://www.linkedin.com/authwall?trk=bf") is True
    assert _logged_out("https://www.linkedin.com/feed/") is False
    # a redirect target in the query must not count as being logged out
    assert _logged_out("https://www.linkedin.com/in/me?next=/login") is False
