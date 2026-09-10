import json
from pathlib import Path

import pytest

from pinchana_inst import main
from pinchana_inst.age_gate import (
    AgeGateAwareInstagramGraphScraper,
    ExplicitAgeRestrictedMediaError,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "instagram"


class FakeResponse:
    def __init__(self, *, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)


class FakeSession:
    def __init__(self, page_html: str, *, graphql_payload=None):
        self.page_html = page_html
        self.graphql_payload = graphql_payload or {"data": {"xig_polaris_media": None}}
        self.cookies = {}
        self.gets = []
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return FakeResponse(status_code=200, text=self.page_html)

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(status_code=200, payload=self.graphql_payload)


def gating_payload(shortcode: str) -> dict:
    return {
        "data": {
            "xig_polaris_media": {
                "__typename": "XIGPolarisVideoMedia",
                "pk": "3982501452437781755",
                "code": shortcode,
                "if_not_gated_logged_out": None,
                "gating_ruling": {
                    "gating_type": 3,
                    "description": (
                        "This content is age-restricted based on your age or account settings."
                    ),
                    "title": "Age-restricted content",
                },
                "id": "POLARIS_3982501452437781755",
            }
        },
        "errors": [
            {
                "message": "A server error field_exception occured. Check server logs for details.",
                "severity": "ERROR",
            }
        ],
    }


@pytest.mark.asyncio
async def test_initial_html_gating_ruling_is_explicit_age_restriction():
    page_html = (
        '<!doctype html><script type="application/json">'
        + json.dumps(gating_payload("AGEHTML123"))
        + "</script>"
    )
    session = FakeSession(page_html)
    scraper = AgeGateAwareInstagramGraphScraper(session_factory=lambda **_kwargs: session)

    with pytest.raises(ExplicitAgeRestrictedMediaError) as exc_info:
        await scraper.extract_media("AGEHTML123")

    assert exc_info.value.age_restricted is True
    assert "gating_type=3" in str(exc_info.value)
    assert session.posts == []


@pytest.mark.asyncio
async def test_relay_gating_ruling_is_explicit_age_restriction():
    page_html = (FIXTURE_ROOT / "preloader-only-page.html").read_text()
    session = FakeSession(page_html, graphql_payload=gating_payload("PRE123"))
    scraper = AgeGateAwareInstagramGraphScraper(session_factory=lambda **_kwargs: session)

    with pytest.raises(ExplicitAgeRestrictedMediaError) as exc_info:
        await scraper.extract_media("PRE123")

    assert exc_info.value.age_restricted is True
    assert len(session.posts) == 1


def test_main_paid_fallback_accepts_explicit_gating_ruling_error():
    error = ExplicitAgeRestrictedMediaError(
        "Instagram explicitly age-restricted post AGE123 (gating_type=3)."
    )

    assert main._is_explicit_age_restriction(error) is True


def test_non_age_gating_ruling_is_not_classified_as_age_restricted():
    scraper = AgeGateAwareInstagramGraphScraper()
    media = {
        "xig_polaris_media": {
            "code": "OTHER123",
            "if_not_gated_logged_out": None,
            "gating_ruling": {
                "gating_type": 7,
                "title": "Restricted content",
                "description": "This content is unavailable.",
            },
        }
    }

    unwrapped = scraper._unwrap_polaris_media(media)
    assert unwrapped["code"] == "OTHER123"
