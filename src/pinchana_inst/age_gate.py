"""Age-gate aware wrapper around Instagram's anonymous GraphQL scraper."""

from .scraper import InstagramGraphScraper, RestrictedMediaError


class ExplicitAgeRestrictedMediaError(RestrictedMediaError):
    """Instagram explicitly returned an age-restriction gating ruling."""

    age_restricted = True


class AgeGateAwareInstagramGraphScraper(InstagramGraphScraper):
    """Detect first-party ``gating_ruling`` age gates before treating them as misses."""

    @staticmethod
    def _is_explicit_age_ruling(ruling: object) -> bool:
        if not isinstance(ruling, dict):
            return False
        text = " ".join(
            str(ruling.get(key) or "") for key in ("title", "description")
        ).casefold()
        return "age-restricted" in text or "age restricted" in text

    @classmethod
    def _unwrap_polaris_media(cls, data: dict) -> dict | None:
        media = data.get("xig_polaris_media") if isinstance(data, dict) else None
        if isinstance(media, dict):
            ruling = media.get("gating_ruling")
            if cls._is_explicit_age_ruling(ruling):
                shortcode = media.get("code") or media.get("shortcode") or "unknown"
                gating_type = ruling.get("gating_type") if isinstance(ruling, dict) else None
                detail = f"gating_type={gating_type}" if gating_type is not None else "gating_ruling"
                raise ExplicitAgeRestrictedMediaError(
                    f"Instagram explicitly age-restricted post {shortcode} ({detail})."
                )
        return super()._unwrap_polaris_media(data)
