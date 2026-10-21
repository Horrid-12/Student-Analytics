"""Tests for LinkedIn profile URL resolution and sanitization."""

import pytest
from app import services as app_services
from app import ui_helpers


def test_linkedin_profile_url_rejects_names_with_spaces():
    # Names with spaces should never generate broken /in/Name URLs
    assert app_services.linkedin_profile_url("Animesh Misra") == ""
    assert ui_helpers.linkedin_profile_url("Animesh Misra") == ""


def test_linkedin_profile_url_accepts_clean_slug():
    assert app_services.linkedin_profile_url("animesh-misra") == "https://www.linkedin.com/in/animesh-misra"
    assert ui_helpers.linkedin_profile_url("animesh-misra") == "https://www.linkedin.com/in/animesh-misra"


def test_linkedin_profile_url_normalizes_full_url():
    url = "https://www.linkedin.com/in/animesh-misra/"
    assert app_services.linkedin_profile_url(url) == "https://www.linkedin.com/in/animesh-misra"
    assert ui_helpers.linkedin_profile_url(url) == "https://www.linkedin.com/in/animesh-misra"


def test_linkedin_profile_url_empty_and_none():
    assert app_services.linkedin_profile_url("") == ""
    assert app_services.linkedin_profile_url(None) == ""
    assert ui_helpers.linkedin_profile_url("") == ""
    assert ui_helpers.linkedin_profile_url(None) == ""
