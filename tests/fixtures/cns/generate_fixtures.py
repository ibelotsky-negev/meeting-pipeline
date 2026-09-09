"""Deterministic generator for tests/fixtures/cns/*.txt.

WHY the trap fractions matter
------------------------------
Both real earnings-call transcript formats put the phrase "question-and-answer"
(or "Q&A") inside the operator's OPENING boilerplate, long before the actual
handoff to the analyst Q&A session. Verified against live FMP transcripts on
2026-09-09: AbbVie's ONLY occurrence of "question-and-answer" is at character
176 of 56,663 (0.3% into the document); Biogen has "question-and-answer" at
character 354 (0.6%) and "Q&A" at character 1,606 (2.7%). A naive
"find the phrase" boundary detector would label the ENTIRE call as Q&A in
both cases.

find_qa_boundary() defends against this by rejecting any candidate handoff
outside a position band (default 15%-85% of the document) and taking the
earliest surviving candidate. These four fixtures exist to PROVE that defense
works: format_a_abbv.txt and format_b_biib.txt each carry the trap phrase
below the band floor (so it is rejected) while the real handoff sits
comfortably inside the band, in the 0.30-0.50 range that matches the twelve
real transcripts sampled for this feature (their boundaries fell between
0.234 and 0.596). A fixture whose trap does not sit below the band floor
would not reproduce the bug this detection exists to avoid -- the test would
pass for the wrong reason.

Run directly to (re)write the fixtures:
    python tests/fixtures/cns/generate_fixtures.py

This script is a maintenance tool, not a test -- pytest never collects it
(the "generate_" prefix does not match the "test_*.py" collection pattern).
Running it twice must produce byte-identical output (git status must show no
diff on the second run): there is no randomness and no timestamp anywhere in
any fixture.
"""
import os
import sys

FIXTURES_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(FIXTURES_DIR, "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import cns_screen  # noqa: E402

FILLER_UNIT = (
    "Revenue in the quarter grew across the portfolio and we remain "
    "confident in the full-year outlook we provided in April. "
)

# Additional QA-side prose appended AFTER the real handoff in formats A and B.
# This grows the Q&A share of the document without touching a single
# character of the prepared-remarks content that carries the trap phrase --
# per the rebalance rule, filler goes on the Q&A side, never the trap side.
QA_FILLER_UNIT_A = (
    "Terence Flynn. Understood, thanks for the added context on capital "
    "allocation. Roopal Thakkar. Sure -- we continue to evaluate business "
    "development opportunities across the neuroscience landscape every "
    "quarter and remain disciplined on capital deployment. "
)
QA_FILLER_UNIT_B = (
    "Christopher A. Viehbacher: We continue to invest across the neuroscience "
    "portfolio and look for disciplined tuck-in opportunities that complement "
    "our core franchises.\n"
    "Chris Schott: That's helpful, thanks for the additional detail.\n"
)


def build_format_a_abbv() -> str:
    """Single unbroken line, 'Analyst (Name).' labels. The trap phrase sits in
    the operator's opening boilerplate; the real handoff is 'We will now open
    the call for questions.'. Nine repeats of QA_FILLER_UNIT_A after the
    handoff pull the real fraction from the original 0.795 down to ~0.40,
    comfortably inside 0.30-0.50."""
    opening = (
        "Operator. Good morning, and thank you for standing by. Welcome to the AbbVie "
        "Second Quarter 26 Earnings Conference Call. All participants will be able to "
        "listen only until the question-and-answer portion of this call. You may ask a "
        "question by pressing star 1 on your phone. "
    )
    neuro = (
        "We continue to invest across neuroscience, and our Parkinson's disease "
        "portfolio is performing. "
    )
    handoff_and_qa = (
        "We will now open the call for questions. In the interest of hearing from as "
        "many analysts as possible, we ask that you limit your questions to 1 or 2. "
        "Operator, we will take the first question. Analyst (Terence Flynn). Great. "
        "Congrats on the progress. Can you speak to your appetite for external "
        "neuroscience assets given the pipeline gap after 2029? Roopal Thakkar. "
        "Thanks, Terence. We have significant capacity for business development, "
        "particularly in neuroscience."
    )
    text = (
        opening
        + FILLER_UNIT * 6
        + neuro
        + FILLER_UNIT * 6
        + handoff_and_qa
        + QA_FILLER_UNIT_A * 9
    )
    assert "\n" not in text, "format A must be a single unbroken line"
    return text


def build_format_b_biib() -> str:
    """Newline-delimited 'Speaker Name: text' turns. Both trap phrases
    ('question-and-answer' AND 'Q&A') sit in the opening two turns; the real
    handoff is 'could we open us up for questions'. Six repeats of
    QA_FILLER_UNIT_B appended after Chris Schott's question pull the real
    fraction from the original 0.824 down to ~0.40, comfortably inside
    0.30-0.50."""
    lines = [
        "Operator: Please stand by. We are about to begin. Good morning. My name is "
        "Jess. After the speakers' remarks, there will be a question-and-answer "
        "session. To ask a question, please press star one.",
        "Tim Power: Thanks, Jess. Welcome to Biogen's second quarter 2026 Earnings "
        "Call. During this call we will make forward-looking statements. Alisha Alaimo "
        "will also be available for the Q&A section of the call. " + FILLER_UNIT,
        "Christopher A. Viehbacher: Thank you, Tim. " + FILLER_UNIT + FILLER_UNIT
        + "Our Alzheimer's franchise and the broader neurology portfolio remain the "
        "core of the growth story. " + FILLER_UNIT,
        "Tim Power: Thanks, Robin. Jess, could we open us up for questions, please?",
        "Operator: Certainly. Our first question comes from Chris Schott.",
        "Chris Schott: Thanks. On business development in neuroscience, how should we "
        "think about your capacity for a tuck-in acquisition?",
    ]
    return "\n".join(lines) + "\n" + (QA_FILLER_UNIT_B * 6)


def build_format_b_roche() -> str:
    """'Name : text' spacing (space before colon), blank-line-separated turns.
    Real handoff is "we'll open the Q&A session". Left at its original 0.690
    fraction (no filler at all) -- there is no trap phrase to reject here, and
    0.690 already sits comfortably inside the band."""
    return "\n\n".join([
        "Operator : Ladies and gentlemen, welcome to Roche's Half Year Results "
        "Webinar 2026.",
        "Thomas Schinecker : Thank you very much, and good morning. ",
        "Teresa Graham : Thanks, Thomas. "
        "In neurology, trontinemab continues to progress. ",
        "Bruno Eschli : And with that, I think we are done with the presentation, and "
        "we'll open the Q&A session. First questions would go to Graham Parry from Citi.",
        "Graham Glyn Parry : So there's a question on the neuro portfolio.",
    ])


def build_no_boundary() -> str:
    """No handoff pattern anywhere in the document -- zone must come back
    UNKNOWN. Single unbroken line, same as format A, so it can never
    accidentally acquire a newline-adjacent match either."""
    text = (
        "Operator. Welcome to the call. " + FILLER_UNIT * 12
        + "Our neuroscience pipeline advanced this quarter. " + FILLER_UNIT * 6
        + "That concludes our prepared remarks. Thank you all for joining."
    )
    assert "\n" not in text, "no_boundary.txt must be a single unbroken line"
    return text


def main() -> None:
    fixtures = {
        "format_a_abbv.txt": build_format_a_abbv(),
        "format_b_biib.txt": build_format_b_biib(),
        "format_b_roche.txt": build_format_b_roche(),
        "no_boundary.txt": build_no_boundary(),
    }

    # ---- format_a_abbv.txt: trap below the band floor, real handoff tightened ----
    a = fixtures["format_a_abbv.txt"]
    assert a.count("\n") == 0, "format_a_abbv.txt must contain zero newlines"
    trap_a = a.lower().find("question-and-answer") / len(a)
    boundary_a, _via_a = cns_screen.find_qa_boundary(a)
    assert boundary_a is not None, "format A must have a detectable boundary"
    real_a = boundary_a / len(a)
    assert trap_a < 0.15, f"format A trap fraction {trap_a:.3f} must sit below the band floor"
    assert 0.30 <= real_a <= 0.50, f"format A real fraction {real_a:.3f} must sit in 0.30-0.50"

    # ---- format_b_biib.txt: both traps below the band floor, real handoff tightened ----
    b = fixtures["format_b_biib.txt"]
    trap_b = b.lower().find("question-and-answer") / len(b)
    boundary_b, _via_b = cns_screen.find_qa_boundary(b)
    assert boundary_b is not None, "format B must have a detectable boundary"
    real_b = boundary_b / len(b)
    assert trap_b < 0.15, f"format B trap fraction {trap_b:.3f} must sit below the band floor"
    assert 0.30 <= real_b <= 0.50, f"format B real fraction {real_b:.3f} must sit in 0.30-0.50"

    # ---- format_b_roche.txt: no trap, real handoff inside the wider band ----
    r = fixtures["format_b_roche.txt"]
    boundary_r, _via_r = cns_screen.find_qa_boundary(r)
    assert boundary_r is not None, "roche must have a detectable boundary"
    real_r = boundary_r / len(r)
    assert 0.15 <= real_r <= 0.85, f"roche real fraction {real_r:.3f} must sit in the band"

    # ---- no_boundary.txt: no pattern must match, by design ----
    n = fixtures["no_boundary.txt"]
    assert n.count("\n") == 0, "no_boundary.txt must contain zero newlines"
    boundary_n, via_n = cns_screen.find_qa_boundary(n)
    assert boundary_n is None and via_n is None, "no_boundary.txt must match no handoff pattern"

    print(f"format_a_abbv.txt: len={len(a)} trap={trap_a:.3f} real={real_a:.3f}")
    print(f"format_b_biib.txt: len={len(b)} trap={trap_b:.3f} real={real_b:.3f}")
    print(f"format_b_roche.txt: len={len(r)} real={real_r:.3f}")
    print(f"no_boundary.txt: len={len(n)} (no boundary, by design)")

    for name, content in fixtures.items():
        path = os.path.join(FIXTURES_DIR, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)


if __name__ == "__main__":
    main()
