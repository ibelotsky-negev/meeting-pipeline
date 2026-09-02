# Tests for prompt integrity under budget pressure (2.34.0).
#
# _pulse_truncate_input cuts the ASSEMBLED prompt from the END. That is exactly
# where the output contract, the JSON schema, and Ken's standing corrections
# live. Live evidence, 2026-09-02 replay of the Aug 23-30 week:
#
#   Pass 5: Truncating input from 93481 to 80000 -- 13481 chars DROPPED
#           -> model never saw "OUTPUT (JSON)", answered with a markdown report
#   Pass 5 retry: 93797 -> 80000  -- the appended contract was cut too, so the
#           2.33.2 retry could not possibly have helped
#   Pass 4: 80713 -> 80000        -- tail of the standing-corrections block cut
#
# The fix trims the DATA (whole signal items, never mid-JSON) so every
# instruction survives.
import json

import pytest

import app as app_module


def signals(n_green=0, n_yellow=0, n_red=0, n_ent=0, filler="x" * 200):
    return {"green": [filler + " g%d" % i for i in range(n_green)],
            "yellow": [filler + " y%d" % i for i in range(n_yellow)],
            "red": [filler + " r%d" % i for i in range(n_red)],
            "key_entities": ["ent%d" % i for i in range(n_ent)]}


class TestDropOneSignal:
    def test_drops_from_the_longest_list(self):
        work = {"a": signals(n_green=5, n_yellow=1)}
        assert app_module._pulse_drop_one_signal(work) is True
        assert len(work["a"]["green"]) == 4 and len(work["a"]["yellow"]) == 1

    def test_returns_false_when_nothing_left(self):
        work = {"a": signals()}
        assert app_module._pulse_drop_one_signal(work) is False

    def test_ignores_non_dict_nodes(self):
        work = {"a": "not a dict", "b": signals(n_red=2)}
        assert app_module._pulse_drop_one_signal(work) is True
        assert len(work["b"]["red"]) == 1

    def test_spreads_across_payloads(self):
        work = {"a": signals(n_green=3), "b": signals(n_green=3)}
        for _ in range(4):
            app_module._pulse_drop_one_signal(work)
        assert len(work["a"]["green"]) + len(work["b"]["green"]) == 2


class TestBuildWithinBudget:
    TEMPLATE = ("INSTRUCTIONS AT THE TOP\n{payload}\n"
                "OUTPUT (JSON): {\"proposed_updates\": []}\n"
                "CRITICAL OUTPUT REQUIREMENT: reply with JSON and nothing else.")

    def _build(self, sig):
        return self.TEMPLATE.replace("{payload}", json.dumps(sig, indent=2))

    def test_small_payload_is_untouched(self):
        payloads = {"s": signals(n_green=2)}
        out = app_module._pulse_build_within_budget(
            self._build, payloads, 100000, "Pass X")
        assert "g0" in out and "g1" in out
        assert len(payloads["s"]["green"]) == 2, "must not mutate the caller's data"

    def test_oversized_payload_is_trimmed_to_fit(self):
        out = app_module._pulse_build_within_budget(
            self._build, {"s": signals(n_green=60)}, 4000, "Pass X")
        assert len(out) <= 4000

    def test_instructions_always_survive(self):
        """The whole point: the tail of the template must never be cut."""
        out = app_module._pulse_build_within_budget(
            self._build, {"s": signals(n_green=200)}, 3000, "Pass X")
        assert "CRITICAL OUTPUT REQUIREMENT" in out
        assert "OUTPUT (JSON)" in out
        assert "INSTRUCTIONS AT THE TOP" in out

    def test_embedded_json_stays_parseable(self):
        """Character-slicing would hand the model malformed JSON on top of
        losing content. Dropping whole items must not."""
        out = app_module._pulse_build_within_budget(
            self._build, {"s": signals(n_green=60)}, 4000, "Pass X")
        body = out.split("INSTRUCTIONS AT THE TOP\n", 1)[1]
        body = body.rsplit("\nOUTPUT (JSON)", 1)[0]
        parsed = json.loads(body)
        assert isinstance(parsed["s"]["green"], list)

    def test_trimming_is_logged_with_a_count(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            app_module._pulse_build_within_budget(
                self._build, {"s": signals(n_green=60)}, 4000, "Pass 5")
        assert "Pass 5" in caplog.text
        assert "dropped" in caplog.text.lower()
        assert "Instructions preserved" in caplog.text

    def test_no_log_when_nothing_dropped(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            app_module._pulse_build_within_budget(
                self._build, {"s": signals(n_green=1)}, 100000, "Pass 5")
        assert "dropped" not in caplog.text.lower()

    def test_terminates_when_budget_is_unreachable(self):
        """An impossible budget must return the minimal prompt, not hang."""
        out = app_module._pulse_build_within_budget(
            self._build, {"s": signals(n_green=20)}, 10, "Pass X")
        assert "CRITICAL OUTPUT REQUIREMENT" in out


class TestSystemPromptLen:
    def test_counted_when_briefing_used(self, monkeypatch):
        monkeypatch.setattr(app_module, "load_briefing_book", lambda: "B" * 500)
        assert app_module._pulse_system_prompt_len(True) == 532

    def test_zero_when_not_used(self, monkeypatch):
        monkeypatch.setattr(app_module, "load_briefing_book", lambda: "B" * 500)
        assert app_module._pulse_system_prompt_len(False) == 0

    def test_failure_is_not_fatal(self, monkeypatch):
        def boom():
            raise OSError("no briefing")
        monkeypatch.setattr(app_module, "load_briefing_book", boom)
        assert app_module._pulse_system_prompt_len(True) == 0


class TestSynthesisKeepsCorrections:
    """Pass 4 appended standing corrections AFTER assembly, so a large week
    truncated them straight back off -- silently defeating the mechanism that
    stops the pulse repeating known mistakes."""

    @pytest.fixture
    def stub(self, monkeypatch):
        monkeypatch.setattr(app_module, "load_briefing_book", lambda: "")
        # Above the ~7K synthesis template but far below the ~66K of signal
        # below, so trimming is genuinely exercised and can actually succeed.
        monkeypatch.setattr(app_module, "PULSE_MAX_INPUT_CHARS", 20000)
        sent = {}

        def fake_call(prompt, model=None, use_briefing=True):
            sent.setdefault("prompts", []).append(prompt)
            return "REPORT"

        def fake_json(prompt, model=None, use_briefing=True, label="pass"):
            sent.setdefault("prompts", []).append(prompt)
            return {"green": [], "yellow": [], "red": [], "key_entities": []}

        monkeypatch.setattr(app_module, "_pulse_call_claude", fake_call)
        monkeypatch.setattr(app_module, "_pulse_call_claude_json", fake_json)
        monkeypatch.setattr(app_module, "PULSE_RATE_LIMIT_SECONDS", 0)
        return sent

    def test_corrections_survive_a_huge_signal_set(self, stub, monkeypatch):
        import datetime
        marker = "STANDING CORRECTION: Ariadne has no lead-investor gap."

        class FakeCorrections:
            @staticmethod
            def corrections_block():
                return marker

        monkeypatch.setitem(__import__("sys").modules, "sara_corrections",
                            FakeCorrections)
        big = [("x" * 300) + str(i) for i in range(200)]
        monkeypatch.setattr(app_module, "_pulse_run_chunked_pass",
                            lambda *a, **k: {"green": big, "yellow": [], "red": [],
                                             "key_entities": []})
        app_module.pulse_analyze(
            [], [], [], datetime.datetime(2026, 8, 23), datetime.datetime(2026, 8, 30))
        synth = [p for p in stub["prompts"] if marker in p]
        assert synth, "standing corrections missing from the synthesis prompt"
        # The real assertion: the prompt must be WITHIN budget with the
        # corrections still in it. Pre-2.34.0 it was assembled far over budget
        # and _pulse_truncate_input ate the tail -- the corrections -- inside
        # _pulse_call_claude. Asserting only "marker present" would pass on the
        # old code too, because this test mocks that call.
        assert len(synth[0]) <= app_module.PULSE_MAX_INPUT_CHARS, (
            "synthesis prompt is %d chars, over the %d budget -- truncation "
            "would cut the corrections back off"
            % (len(synth[0]), app_module.PULSE_MAX_INPUT_CHARS))
