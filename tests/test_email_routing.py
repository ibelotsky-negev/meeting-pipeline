# Tests for email alias normalization, internal-domain detection, and
# HubSpot owner routing (HUBSPOT_OWNER_MAP -> API lookup -> fallback).
import app as app_module
import hubspot_client as hc


class TestNormalizeTeamEmail:
    def test_ariadnebio_alias_maps_to_negevlabs(self):
        assert app_module.normalize_team_email("shlomi@ariadnebio.com") == "shlomi@negevlabs.com"

    def test_negevcap_alias_maps_to_negevlabs(self):
        assert app_module.normalize_team_email("kostia@negevcap.com") == "ka@negevlabs.com"

    def test_palomar_labs_aliases_map_to_negevlabs(self):
        # Negev Labs -> Palomar Labs rename (2026-08-06): team also sends
        # from @palomar-labs.com now; canonical identity stays @negevlabs.com.
        assert app_module.normalize_team_email("ken@palomar-labs.com") == "bk@negevlabs.com"
        assert app_module.normalize_team_email("shlomi@palomar-labs.com") == "shlomi@negevlabs.com"
        assert app_module.normalize_team_email("kostia@palomar-labs.com") == "ka@negevlabs.com"
        assert app_module.normalize_team_email("dan@palomar-labs.com") == "dan@negevlabs.com"

    def test_case_insensitive_and_strips_whitespace(self):
        assert app_module.normalize_team_email("  Shlomi@AriadneBio.COM ") == "shlomi@negevlabs.com"

    def test_unknown_email_passes_through_lowercased(self):
        assert app_module.normalize_team_email("Someone@External.com") == "someone@external.com"

    def test_empty_passes_through(self):
        assert app_module.normalize_team_email("") == ""


class TestIsInternalEmail:
    def test_all_internal_domains_detected(self):
        for domain in ("negevlabs.com", "negevcap.com", "ariadnebio.com",
                       "adres.bio", "zirmania.onmicrosoft.com"):
            assert app_module.is_internal_email(f"user@{domain}"), domain

    def test_case_insensitive(self):
        assert app_module.is_internal_email("BK@NegevLabs.COM")

    def test_palomar_labs_domain_is_internal(self):
        assert app_module.is_internal_email("ken@palomar-labs.com")

    def test_external_domains_not_matched(self):
        assert not app_module.is_internal_email("user@gmail.com")
        assert not app_module.is_internal_email("user@negevlabs.com.evil.com")

    def test_garbage_input(self):
        assert not app_module.is_internal_email("")
        assert not app_module.is_internal_email(None)
        assert not app_module.is_internal_email("no-at-sign")


class TestResolveHubspotOwner:
    def test_ken_resolves_from_map(self):
        assert app_module.resolve_hubspot_owner("bk@negevlabs.com") == "241153249"

    def test_shlomi_resolves_from_map(self):
        assert app_module.resolve_hubspot_owner("shlomi@negevlabs.com") == "31267643"

    def test_shlomi_alias_email_resolves_via_normalization(self):
        assert app_module.resolve_hubspot_owner("shlomi@ariadnebio.com") == "31267643"

    def test_shlomi_palomar_labs_alias_resolves_via_normalization(self):
        assert app_module.resolve_hubspot_owner("shlomi@palomar-labs.com") == "31267643"

    def test_ken_palomar_labs_alias_resolves_via_normalization(self):
        assert app_module.resolve_hubspot_owner("ken@palomar-labs.com") == "241153249"

    def test_kostia_no_seat_falls_back_to_ken(self, monkeypatch):
        api_calls = []

        def fake_hubspot_request(method, endpoint, data=None, params=None):
            api_calls.append((method, endpoint, params))
            return {"results": []}  # no HubSpot seat for this email

        monkeypatch.setattr(hc, "hubspot_request", fake_hubspot_request)
        result = app_module.resolve_hubspot_owner("ka@negevlabs.com")
        assert result == "241153249"
        # The API lookup chain was attempted before falling back
        assert len(api_calls) == 1

    def test_empty_email_returns_fallback(self):
        assert app_module.resolve_hubspot_owner("") == "241153249"

    def test_api_lookup_result_is_cached(self, monkeypatch):
        call_count = {"n": 0}

        def fake_hubspot_request(method, endpoint, data=None, params=None):
            call_count["n"] += 1
            return {"results": [{"id": 99001}]}

        monkeypatch.setattr(hc, "hubspot_request", fake_hubspot_request)
        first = app_module.resolve_hubspot_owner("new.hire@negevlabs.com")
        second = app_module.resolve_hubspot_owner("new.hire@negevlabs.com")
        assert first == second == "99001"
        assert call_count["n"] == 1


class TestResolveInternalOrganizerCanonicalIdentity:
    """resolve_internal_organizer must hand downstream systems the CANONICAL
    team address.

    HUBSPOT_OWNER_MAP, Asana and the To-Do sync are all keyed on
    @negevlabs.com. Returning a raw @palomar-labs.com alias was silent
    breakage: HubSpot normalizes internally so it looked fine, but
    find_asana_user_by_email() missed the user and the To-Do sync dropped the
    task (it filters on @negevlabs.com).
    """

    def test_palomar_organizer_resolves_to_canonical(self):
        assert app_module.resolve_internal_organizer("ken@palomar-labs.com", []) == "bk@negevlabs.com"

    def test_negevlabs_organizer_is_unchanged(self):
        assert app_module.resolve_internal_organizer("bk@negevlabs.com", []) == "bk@negevlabs.com"

    def test_organizer_is_lowercased(self):
        assert app_module.resolve_internal_organizer("BK@NegevLabs.com", []) == "bk@negevlabs.com"

    def test_palomar_internal_lead_resolves_to_canonical(self):
        resolved = app_module.resolve_internal_organizer(
            "outsider@example.com", [], internal_lead_email="shlomi@palomar-labs.com"
        )
        assert resolved == "shlomi@negevlabs.com"

    def test_palomar_participant_matches_owner_map(self):
        """The owner-map fallback is keyed on canonical addresses, so a
        palomar participant has to be normalized BEFORE the membership test --
        otherwise it never matches and routing falls through."""
        resolved = app_module.resolve_internal_organizer(
            "outsider@example.com", ["guest@example.com", "dan@palomar-labs.com"]
        )
        assert resolved == "dan@negevlabs.com"

    def test_palomar_participant_fallback_resolves_to_canonical(self):
        """kostia has no HubSpot seat, so this exercises the any-internal
        fallback rather than the owner-map branch."""
        resolved = app_module.resolve_internal_organizer(
            "outsider@example.com", ["kostia@palomar-labs.com"]
        )
        assert resolved == "ka@negevlabs.com"

    def test_external_organizer_with_no_internal_participant_is_kept(self):
        assert app_module.resolve_internal_organizer("outsider@example.com", []) == "outsider@example.com"

    def test_notification_recipient_stays_internal_after_normalization(self):
        """Guard the notify_organizer filter: the canonical form must still
        pass is_internal_email, or the fix would silently suppress mail."""
        resolved = app_module.resolve_internal_organizer("ken@palomar-labs.com", [])
        assert app_module.is_internal_email(resolved)
