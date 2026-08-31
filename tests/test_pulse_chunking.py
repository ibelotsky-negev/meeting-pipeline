# Tests for chunked pulse analysis (2.31.0).
#
# Context: every extraction pass used to be a single Claude call whose prompt
# was hard-truncated at PULSE_MAX_INPUT_CHARS. The 2026-08-30 run logged
# "Truncating input from 233726 to 80000 chars" -- about two thirds of the
# week's email never reached the model, behind one WARNING. The pass is now
# split into as many calls as the budget requires and the per-chunk signals
# are merged, so the whole corpus is analyzed.
import time

import pytest

import app as app_module


@pytest.fixture(autouse=True)
def no_briefing(monkeypatch):
    """Keep the input budget deterministic across environments."""
    monkeypatch.setattr(app_module, "load_briefing_book", lambda: "")


@pytest.fixture
def no_sleep(monkeypatch):
    """Record rate-limit pauses without actually waiting."""
    calls = []
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(s))
    return calls


def make_emails(n, preview="x" * 200):
    return [{"date": "2026-08-2%d" % (d % 10), "subject": "Subject number %d" % d,
             "bodyPreview": preview, "from_addr": "a%d@negevlabs.com" % d,
             "to_count": 2} for d in range(n)]


# ----------------------------------------------------------------------
# packing
# ----------------------------------------------------------------------

class TestPackChunks:
    def test_small_input_is_one_chunk(self):
        blocks = ["a" * 100 for _ in range(5)]
        assert len(app_module._pulse_pack_chunks(blocks, 80000)) == 1

    def test_empty_input_makes_no_chunks(self):
        assert app_module._pulse_pack_chunks([], 1000) == []

    def test_splits_when_over_budget(self):
        blocks = ["a" * 100 for _ in range(10)]
        chunks = app_module._pulse_pack_chunks(blocks, 300)
        assert len(chunks) > 1

    def test_no_item_is_lost_or_duplicated(self):
        blocks = ["item-%03d" % i for i in range(97)]
        chunks = app_module._pulse_pack_chunks(blocks, 40)
        flat = [b for c in chunks for b in c]
        assert flat == blocks, "packing must preserve every item, in order, once"

    def test_every_chunk_respects_budget(self):
        blocks = ["a" * 90 for _ in range(50)]
        budget = 500
        for chunk in app_module._pulse_pack_chunks(blocks, budget):
            assert len("\n".join(chunk)) <= budget

    def test_oversized_item_is_truncated_not_dropped(self):
        """One huge email must not cost us the chunk it sits in."""
        blocks = ["small-a", "B" * 5000, "small-b"]
        chunks = app_module._pulse_pack_chunks(blocks, 500)
        flat = [b for c in chunks for b in c]
        assert len(flat) == 3
        assert "small-a" in flat and "small-b" in flat
        big = [b for b in flat if b.startswith("B")][0]
        assert "item truncated" in big
        assert len(big) <= 500 + 40

    def test_single_item_larger_than_budget_still_yields_a_chunk(self):
        chunks = app_module._pulse_pack_chunks(["Z" * 9000], 1000)
        assert len(chunks) == 1 and len(chunks[0]) == 1


# ----------------------------------------------------------------------
# merging
# ----------------------------------------------------------------------

class TestMergeSignals:
    def test_concatenates_across_chunks(self):
        merged = app_module._pulse_merge_signals([
            {"green": ["g1"], "yellow": ["y1"], "red": [], "key_entities": ["MJFF"]},
            {"green": ["g2"], "yellow": [], "red": ["r1"], "key_entities": ["FFG"]},
        ])
        assert merged["green"] == ["g1", "g2"]
        assert merged["yellow"] == ["y1"]
        assert merged["red"] == ["r1"]
        assert merged["key_entities"] == ["MJFF", "FFG"]

    def test_dedups_case_insensitively(self):
        merged = app_module._pulse_merge_signals([
            {"key_entities": ["Ariadne Bio"]},
            {"key_entities": ["ariadne bio", "Ariadne Bio", "Galilee CBR"]},
        ])
        assert merged["key_entities"] == ["Ariadne Bio", "Galilee CBR"]

    def test_ignores_non_dict_parts(self):
        merged = app_module._pulse_merge_signals([None, "oops", {"green": ["kept"]}])
        assert merged["green"] == ["kept"]

    def test_missing_and_null_keys_are_safe(self):
        merged = app_module._pulse_merge_signals([{}, {"green": None}, {"red": ["r"]}])
        assert merged["green"] == [] and merged["red"] == ["r"]

    def test_blank_strings_dropped(self):
        merged = app_module._pulse_merge_signals([{"green": ["  ", "", "real"]}])
        assert merged["green"] == ["real"]

    def test_non_string_items_survive(self):
        """Defensive: a model may emit objects instead of strings."""
        merged = app_module._pulse_merge_signals([
            {"green": [{"text": "a"}]}, {"green": [{"text": "a"}, {"text": "b"}]}])
        assert merged["green"] == [{"text": "a"}, {"text": "b"}]

    def test_all_four_keys_always_present(self):
        merged = app_module._pulse_merge_signals([])
        assert set(merged) == {"green", "yellow", "red", "key_entities"}


# ----------------------------------------------------------------------
# budget
# ----------------------------------------------------------------------

class TestInputBudget:
    def test_subtracts_prompt_scaffold(self, monkeypatch):
        monkeypatch.setattr(app_module, "PULSE_MAX_INPUT_CHARS", 10000)
        template = "S" * 3000 + "{emails_text}"
        assert app_module._pulse_input_budget(template, "{emails_text}") == \
            10000 - 3000 - 512

    def test_subtracts_briefing_book(self, monkeypatch):
        """The system prompt counts against the same request budget."""
        monkeypatch.setattr(app_module, "PULSE_MAX_INPUT_CHARS", 10000)
        monkeypatch.setattr(app_module, "load_briefing_book", lambda: "B" * 2000)
        budget = app_module._pulse_input_budget("{x}", "{x}")
        assert budget == 10000 - (2000 + 32) - 512

    def test_briefing_failure_is_not_fatal(self, monkeypatch):
        def boom():
            raise OSError("briefing unavailable")
        monkeypatch.setattr(app_module, "load_briefing_book", boom)
        assert app_module._pulse_input_budget("{x}", "{x}") > 0

    def test_floors_at_a_usable_size(self, monkeypatch):
        monkeypatch.setattr(app_module, "PULSE_MAX_INPUT_CHARS", 100)
        assert app_module._pulse_input_budget("S" * 9000 + "{x}", "{x}") == 4000


# ----------------------------------------------------------------------
# the pass runner
# ----------------------------------------------------------------------

class TestRunChunkedPass:
    def _capture(self, monkeypatch, reply='{"green": [], "yellow": [], "red": []}'):
        seen = []

        def fake_call(prompt, model=None, use_briefing=True):
            seen.append(prompt)
            return reply if isinstance(reply, str) else reply(len(seen))

        monkeypatch.setattr(app_module, "_pulse_call_claude", fake_call)
        return seen

    def test_no_items_makes_one_call_with_empty_text(self, monkeypatch, no_sleep):
        seen = self._capture(monkeypatch)
        app_module._pulse_run_chunked_pass(
            "Pass 1", "PROMPT {emails_text}", "{emails_text}", [],
            "(No emails collected)", 65)
        assert len(seen) == 1
        assert "(No emails collected)" in seen[0]
        assert no_sleep == []

    def test_single_chunk_passes_result_through_unchanged(self, monkeypatch, no_sleep):
        """Extra keys the model returns must survive when there is no merge."""
        self._capture(monkeypatch, '{"green": ["g"], "extra": "kept"}')
        out = app_module._pulse_run_chunked_pass(
            "Pass 1", "P {emails_text}", "{emails_text}", ["a", "b"], "(none)", 65)
        assert out["green"] == ["g"]
        assert out["extra"] == "kept"

    def test_large_corpus_is_split_and_fully_sent(self, monkeypatch, no_sleep):
        """The 2026-08-30 regression: nothing may be silently dropped."""
        monkeypatch.setattr(app_module, "PULSE_MAX_INPUT_CHARS", 8000)
        seen = self._capture(monkeypatch)
        blocks = app_module._pulse_email_blocks(make_emails(120))
        app_module._pulse_run_chunked_pass(
            "Pass 1", "PROMPT {emails_text}", "{emails_text}", blocks, "(none)", 65)

        assert len(seen) > 1, "corpus should not fit in one call"
        combined = "\n".join(seen)
        for i in range(120):
            assert "Subject number %d" % i in combined, "email %d never reached Claude" % i
        # One pause between calls, none before the first.
        assert no_sleep == [65] * (len(seen) - 1)

    def test_chunk_results_are_merged(self, monkeypatch, no_sleep):
        monkeypatch.setattr(app_module, "PULSE_MAX_INPUT_CHARS", 8000)
        replies = ['{"green": ["from chunk 1"], "key_entities": ["MJFF"]}',
                   '{"green": ["from chunk 2"], "key_entities": ["MJFF", "FFG"]}']
        self._capture(monkeypatch, lambda n: replies[min(n, len(replies)) - 1])
        blocks = app_module._pulse_email_blocks(make_emails(120))
        out = app_module._pulse_run_chunked_pass(
            "Pass 1", "PROMPT {emails_text}", "{emails_text}", blocks, "(none)", 65)
        assert "from chunk 1" in out["green"] and "from chunk 2" in out["green"]
        assert out["key_entities"] == ["MJFF", "FFG"]

    def test_zero_delay_skips_sleeping(self, monkeypatch, no_sleep):
        monkeypatch.setattr(app_module, "PULSE_MAX_INPUT_CHARS", 8000)
        self._capture(monkeypatch)
        blocks = app_module._pulse_email_blocks(make_emails(60))
        app_module._pulse_run_chunked_pass(
            "Pass 1", "P {emails_text}", "{emails_text}", blocks, "(none)", 0)
        assert no_sleep == []

    def test_chunk_cap_is_loud_about_what_it_drops(self, monkeypatch, no_sleep, caplog):
        """A cap may bound cost, but never silently."""
        import logging
        monkeypatch.setattr(app_module, "PULSE_MAX_INPUT_CHARS", 4000)
        monkeypatch.setattr(app_module, "PULSE_MAX_CHUNKS", 2)
        seen = self._capture(monkeypatch)
        blocks = app_module._pulse_email_blocks(make_emails(200))
        with caplog.at_level(logging.WARNING):
            app_module._pulse_run_chunked_pass(
                "Pass 1", "P {emails_text}", "{emails_text}", blocks, "(none)", 65)
        assert len(seen) == 2
        assert "will NOT be analyzed" in caplog.text
        assert "PULSE_MAX_CHUNKS" in caplog.text

    def test_parse_failure_in_one_chunk_does_not_lose_the_others(
            self, monkeypatch, no_sleep):
        monkeypatch.setattr(app_module, "PULSE_MAX_INPUT_CHARS", 8000)
        replies = ["I cannot help with that.",
                   '{"green": ["survived"], "yellow": [], "red": []}']
        self._capture(monkeypatch, lambda n: replies[min(n, len(replies)) - 1])
        blocks = app_module._pulse_email_blocks(make_emails(120))
        out = app_module._pulse_run_chunked_pass(
            "Pass 1", "P {emails_text}", "{emails_text}", blocks, "(none)", 65)
        assert "survived" in out["green"]


# ----------------------------------------------------------------------
# formatters keep their existing contract
# ----------------------------------------------------------------------

class TestFormattersUnchanged:
    def test_emails_render_as_before(self):
        out = app_module._pulse_format_emails(make_emails(2, preview="hello"))
        assert "1. [2026-08-2" in out and "Subject number 0" in out
        assert "Preview: hello" in out

    def test_empty_placeholders_preserved(self):
        assert app_module._pulse_format_emails([]) == "(No emails collected)"
        assert app_module._pulse_format_teams([]) == "(No Teams messages collected)"
        assert app_module._pulse_format_meetings([]) == "(No meetings collected)"

    def test_blocks_join_to_the_formatted_string(self):
        emails = make_emails(4)
        assert "\n".join(app_module._pulse_email_blocks(emails)) == \
            app_module._pulse_format_emails(emails)

    def test_meeting_blocks_carry_summary_and_actions(self):
        meetings = [{"date": "2026-08-26", "title": "Kesha <> Ivan",
                     "duration_minutes": 49, "summary": "longevity pipeline",
                     "action_items": "send blurb",
                     "fireflies_url": "https://app.fireflies.ai/view/abc"}]
        block = app_module._pulse_meeting_blocks(meetings)[0]
        assert "Kesha <> Ivan" in block
        assert "Summary: longevity pipeline" in block
        assert "Action items: send blurb" in block
        assert "app.fireflies.ai/view/abc" in block
