# Tests for to_hubspot_ms and to_graph_datetime (pure logic, no network).
from datetime import datetime, timezone

import app as app_module


class TestToHubspotMs:
    def test_date_only_returns_midnight_utc_ms_string(self):
        result = app_module.to_hubspot_ms("2026-04-28")
        expected = str(int(datetime(2026, 4, 28, tzinfo=timezone.utc).timestamp() * 1000))
        assert result == expected
        assert isinstance(result, str)

    def test_iso_datetime_with_z_suffix(self):
        result = app_module.to_hubspot_ms("2026-04-28T17:00:00Z")
        expected = str(int(datetime(2026, 4, 28, 17, 0, 0, tzinfo=timezone.utc).timestamp() * 1000))
        assert result == expected

    def test_iso_datetime_with_explicit_offset(self):
        z_form = app_module.to_hubspot_ms("2026-04-28T17:00:00Z")
        offset_form = app_module.to_hubspot_ms("2026-04-28T17:00:00+00:00")
        assert z_form == offset_form

    def test_naive_datetime_treated_as_utc(self):
        result = app_module.to_hubspot_ms("2026-04-28T17:00:00")
        expected = str(int(datetime(2026, 4, 28, 17, 0, 0, tzinfo=timezone.utc).timestamp() * 1000))
        assert result == expected

    def test_result_is_all_digits(self):
        result = app_module.to_hubspot_ms("2026-03-04T17:00:00Z")
        assert result.isdigit()

    def test_none_returns_none(self):
        assert app_module.to_hubspot_ms(None) is None

    def test_empty_string_returns_none(self):
        assert app_module.to_hubspot_ms("") is None

    def test_whitespace_returns_none(self):
        assert app_module.to_hubspot_ms("   ") is None

    def test_garbage_returns_none(self):
        assert app_module.to_hubspot_ms("not-a-date") is None
        assert app_module.to_hubspot_ms("2026-13-45") is None


class TestToGraphDatetime:
    def test_date_only_defaults_to_midnight(self):
        assert app_module.to_graph_datetime("2026-04-28") == "2026-04-28T00:00:00"

    def test_iso_datetime_z_suffix_stripped(self):
        assert app_module.to_graph_datetime("2026-04-28T17:00:00Z") == "2026-04-28T17:00:00"

    def test_iso_datetime_offset_stripped(self):
        # Graph wants naive ISO paired with a separate timeZone field
        assert app_module.to_graph_datetime("2026-04-28T17:00:00+00:00") == "2026-04-28T17:00:00"

    def test_no_timezone_suffix_in_output(self):
        result = app_module.to_graph_datetime("2026-04-28T09:30:00Z")
        assert "Z" not in result
        assert "+" not in result

    def test_none_returns_none(self):
        assert app_module.to_graph_datetime(None) is None

    def test_empty_string_returns_none(self):
        assert app_module.to_graph_datetime("") is None

    def test_garbage_returns_none(self):
        assert app_module.to_graph_datetime("garbage") is None
