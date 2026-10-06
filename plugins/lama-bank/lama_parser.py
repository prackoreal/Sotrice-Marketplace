"""Parses a LaMA-generated (lama.schule) worksheet PDF into questions
shaped for math_teacher.py's `import_questions` authoring_command — see
math_teacher.py's own module docstring for the target "Question shape"
and `on_authoring_command`'s "import_questions" for the exact payload
this produces.

Not a Sotrice plugin — a standalone offline tool. A teacher (or, later,
an authoring-UI "import from PDF" button) runs this against a LaMA PDF
and gets back questions ready to hand to `import_questions`, plus a
report of what it skipped and why. `pip install pdfplumber` is this
file's one real dependency (word-position-aware PDF text extraction —
plain `pdftotext` collapses multi-column content like a matching table
or a set of side-by-side multiple-choice terms into unreadable noise,
since it has no notion of which words belong to which visual column).

REAL, CONFIRMED LIMITS OF WHAT THIS CAN DO — found by directly
inspecting real LaMA output (text, font COLOR, and embedded images) on
2026-09-19, not assumed from the format's field names alone. The one
genuinely useful discovery: a SOLVED LaMA export (the kind with answers
already filled in, confirmed to be what's actually on disk) color-codes
its own content — BLACK is the problem statement, BLUE is a metadata
label ("Titel:", ...), and RED is LaMA's own worked solution/correct-
answer marking. Used throughout below as the primary signal, with the
original position-based heuristic kept as a fallback for whatever this
color convention doesn't cover.

- A LaMA "Mutiple Choice" (their own spelling) question never marks
  which option(s) are correct ANYWHERE in the PDF — no red text, no
  checkbox glyph (checked: every embedded image on an options page
  turned out to be a hairline fraction-bar/exponent rendering artifact,
  not a checkbox). LaMA assumes a human grades these by eye. So:
  imported as multi_select with `correct_indices: None` (ungraded,
  exactly the state math_teacher.py already has for "a teacher hasn't
  decided a right answer yet") — this still saves retyping the whole
  question and every option, the teacher only needs to tick the correct
  box(es) afterward.
- A LaMA Zuordnungsformat (matching) table CAN have more right-side
  options than left-side terms — real distractors, never correct for
  anything, and confirmed (per Laszlo's own direct call) as a real
  pedagogical feature to preserve, not drop. math_teacher.py's
  matching_pair type now has a real `distractors` field for exactly
  this (see its own "Question shape" docstring) — every right-side
  option letter that a solved table's RED answer-letters never point at
  is imported as a distractor, not discarded.
- LaMA's BIFIE-sourced "Oberstufe" exam content (multi-part questions
  graded by a Lösungsschlüssel POINT-ALLOCATION RUBRIC against a long
  Lösungserwartung essay/derivation, e.g. "give two reasons why...") is
  not representable as ANY question type this system has — it needs a
  human grader reading prose, not a string/option comparison. Skipped
  entirely, reported, never force-fit into free_response.
- Lückentext (fill-in-the-blank) is skipped too, after real
  investigation (not a lazy dismissal): the one example checked is a
  compound "tick the right phrase for each of several blanks in one
  sentence" format — several small embedded multiple-choice picks per
  exercise, not a single value to fill in — which both re-introduces the
  "no recoverable correct answer" problem above AND has no single-slot
  structure clean enough to segment generally.

Given how much of a "Rechenregeln Potenzen"-style term collapses under
plain text extraction (superscripts/subscripts/fraction bars all
flatten to same-baseline characters — "a^x / a^y" and "a^x · a^y" both
end up as barely-distinguishable runs of "a", "x", "y"), THIS PARSER
NEVER MARKS ANYTHING AS READY TO PUBLISH DIRECTLY. Every question it
produces is meant to land in front of a teacher for review/cleanup
before it's ever shown to a real student — same posture any reasonable
person would expect from a PDF importer, not a special caveat unique to
this one. The one thing worth real confidence in is a matching
question's PAIRING (which left term goes with which right term) and a
free_response answer that extracted as a clean bare number — everything
else (the exact rendered text of a mathematical expression) may need
manual LaTeX/notation cleanup even when the underlying logic is right.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import pdfplumber

# Prompt-in (a plain string), text-out (Gemini's raw reply, or None on
# any failure/timeout) -- deliberately just that, no World/ai_request
# shape leaking in here. This module stays a standalone offline tool
# (see its own docstring) with zero Sotrice/Core dependency; whoever
# calls parse_lama_pdf (lama_bank.py) owns the actual ai_request/
# ai_response round trip and hands in a plain callable, same as any
# other dependency-injection boundary.
AiFallback = Callable[[str], Optional[str]]

# A LaMA page repeats "Lösungen" as a watermark two different ways: a
# normal-size (12pt) red "Lösungen" running across the very top/bottom
# margin, and a single HUGE (60-70pt) red/pink character per occurrence
# of a giant diagonal "Lösungen" printed across the page body — the
# latter looks, at word-extraction level, like an ordinary short/single-
# character "word" scattered at seemingly random positions, which would
# otherwise corrupt row/column detection for whatever real content it
# happens to land near. Both are dropped by shape (exact text match for
# the first, a font-size cutoff no real 8-14pt exercise content ever
# reaches for the second) — not by which letter they happen to be,
# since single real letters (x, y, a, n, m...) are extremely common,
# genuine math-variable content here and can't be filtered by identity.
_WATERMARK_WORD = "Lösungen"
_WATERMARK_MAX_REAL_SIZE = 20.0  # largest real body/label text seen is 14.3pt; watermark letters are 60pt+

# LaMA's own solved-PDF convention, confirmed by inspecting real
# character colors directly (not assumed from the field names): BLACK
# is the problem statement, BLUE is a metadata label ("Titel:", ...),
# and RED is LaMA's own worked solution/correct-answer text — used here
# as the primary signal for "which matching-table letter is the marked
# correct answer" and "which part of a free_response exercise is the
# actual final answer, not the problem statement."
def _is_red(color) -> bool:
    if not color or len(color) < 3:
        return False
    r, g, b = color[0], color[1], color[2]
    return r > 0.9 and g < 0.3 and b < 0.3

# The page's own running header ("PWU - 6[1] - Umformung eines Terms")
# and the per-exercise point-counter ("___/1") are likewise decoration,
# not exercise content — recognized by shape, not by hardcoding exact
# strings that would only match this one subject's competency codes.
_POINT_COUNTER_RE = re.compile(r"^_+/\d+$")

METADATA_FIELDS = ("Titel", "Grundkompetenz(en)", "Aufgabenformat", "Klasse", "Quelle")

# Only these Aufgabenformat values (as LaMA itself spells them, typo and
# all — "Mutiple" is really how their tool spells it, confirmed against
# real output) are handled; anything else (Lückentext, or a value never
# seen before) is skipped with a reason rather than guessed at.
_SUPPORTED_FORMATS = ("Offenes Antwortformat", "Mutiple Choice", "Zuordnungsformat")

_NUMERIC_ANSWER_RE = re.compile(r"^-?\d+([.,]\d+)?$")

# Mirrors math_teacher.py's own MAX_DISTRACTORS — duplicated as a plain
# constant rather than imported, since importing math_teacher.py itself
# would run its whole `with World() as world:` body and try to connect
# to a live Sotrice server just to read one number. Capped here too so
# an over-generous real matching table (more distractors than our
# schema allows) still imports its usable distractors instead of the
# whole question being rejected later by validate_question_content.
_MAX_DISTRACTORS = 4


@dataclass
class Word:
    text: str
    x0: float
    top: float
    bottom: float
    red: bool  # see _is_red() above — True for LaMA's own solution/correct-answer-colored text


@dataclass
class RawExercise:
    words: list[Word]  # the exercise's own content, NOT including its metadata block
    meta: dict[str, str]


@dataclass
class ParsedQuestion:
    question: dict[str, Any]  # ready for import_questions' own "questions" list item shape
    review_reason: str
    source: dict[str, str]  # Titel/Grundkompetenz(en)/Klasse/Quelle, for the teacher's own reference


@dataclass
class SkippedExercise:
    reason: str
    source: dict[str, str]


@dataclass
class ParseResult:
    imported: list[ParsedQuestion] = field(default_factory=list)
    skipped: list[SkippedExercise] = field(default_factory=list)


def _is_watermark_word(w) -> bool:
    text = w["text"]
    if text == _WATERMARK_WORD or text == "gen":  # "gen" = the tail end of a watermark instance split across a page-wrap
        return True
    if w.get("size", 0) > _WATERMARK_MAX_REAL_SIZE:
        return True
    if _POINT_COUNTER_RE.match(text):
        return True
    return False


def _load_words(pdf_path: str) -> list[Word]:
    """Every real word on every page, in reading order, watermark/point-
    counter noise already stripped — the single word stream every other
    function in this file segments and slices further. Carries each
    word's color forward (see _is_red()) since several parsers below key
    off it directly rather than word position alone."""
    words: list[Word] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for w in page.extract_words(extra_attrs=["non_stroking_color", "size"]):
                if _is_watermark_word(w):
                    continue
                words.append(Word(
                    text=w["text"], x0=w["x0"], top=w["top"], bottom=w["bottom"],
                    red=_is_red(w.get("non_stroking_color")),
                ))
    return words


def _segment_exercises(words: list[Word]) -> list[RawExercise]:
    """Splits the whole document's word stream into one RawExercise per
    Titel/Grundkompetenz(en)/Aufgabenformat/Klasse/Quelle block, each
    holding the CONTENT that precedes its own metadata (LaMA always
    prints an exercise's metadata block immediately after the exercise
    itself, confirmed against every real example checked) plus the
    parsed metadata dict. Content before the first metadata block or
    after the last one (running headers, a title page) is discarded —
    it's never a real exercise on its own."""
    exercises: list[RawExercise] = []
    content_start = 0
    i = 0
    n = len(words)
    while i < n:
        if words[i].text != "Titel:":
            i += 1
            continue
        # Found a metadata block's start. Parse all 5 fields — each is
        # "Label:" followed by its value words, up to the next label.
        # Every real value is SHORT (a competency code, a class number,
        # a source name, a title — never more than a handful of words),
        # so each field's value search is bounded to a small lookahead
        # window rather than scanning until ANY marker appears anywhere
        # later in the document — an unbounded scan is a real bug this
        # module had at first: when nothing marker-like appeared right
        # after a field (most often "Quelle", the last field, with no
        # field of its own to bound it), it silently swallowed the
        # ENTIRE next exercise's body as if it were this field's value.
        _MAX_FIELD_VALUE_WORDS = 12
        meta: dict[str, str] = {}
        j = i
        for label in METADATA_FIELDS:
            marker = f"{label}:"
            if not (j < n and words[j].text == marker):
                meta[label] = ""
                continue
            value_start = j + 1
            j = value_start
            limit = min(n, value_start + _MAX_FIELD_VALUE_WORDS)
            while j < limit and words[j].text not in (f"{f}:" for f in METADATA_FIELDS):
                j += 1
            meta[label] = " ".join(w.text for w in words[value_start:j]).strip()
        exercises.append(RawExercise(words=words[content_start:i], meta=meta))
        content_start = j
        i = j
    return exercises


def _cluster_rows(words: list[Word], gap: float = 6.0) -> list[list[Word]]:
    """Groups words into visual rows by vertical proximity — NOT a fixed
    grid, since two different exercises' rows sit at different heights.
    `gap` is deliberately small (single-line text only); a stacked
    fraction/exponent spans a bigger gap and ends up split across
    multiple "rows" here, which is fine for our purposes since every
    caller only ever needs "is this word roughly on the same line as
    that one," not perfect multi-line term reconstruction."""
    if not words:
        return []
    ordered = sorted(words, key=lambda w: (w.top, w.x0))
    rows: list[list[Word]] = [[ordered[0]]]
    for w in ordered[1:]:
        if w.top - rows[-1][-1].top > gap:
            rows.append([w])
        else:
            rows[-1].append(w)
    return rows


def _row_text(row: list[Word]) -> str:
    return " ".join(w.text for w in sorted(row, key=lambda w: w.x0))


def _split_x_clusters(xs: list[float]) -> tuple[float, float] | None:
    """Splits a set of x-positions into two column groups by finding the
    single biggest gap in sorted order — used wherever a table has
    exactly two label columns (a matching table's "your answer" blank
    vs its own option-letter column) whose exact pixel position isn't
    known ahead of time and shouldn't be hardcoded. Returns
    (left_cluster_max, right_cluster_min) as the split boundary, or None
    if there's only one real cluster (nothing to split — e.g. an
    UNSOLVED matching table with no answer-blank column at all)."""
    if len(set(round(x) for x in xs)) < 2:
        return None
    unique = sorted(set(xs))
    best_gap, best_at = 0.0, None
    for a, b in zip(unique, unique[1:]):
        if b - a > best_gap:
            best_gap, best_at = b - a, (a, b)
    if best_at is None or best_gap < 15.0:  # too close together to be two real columns
        return None
    return best_at


_SINGLE_LETTER_RE = re.compile(r"^[A-F]$")


def _parse_matching_pair(ex: RawExercise) -> tuple[Optional[tuple[list[dict], list[str]]], str]:
    """Reconstructs a Zuordnungsformat table's pairs (AND real
    distractors — see this module's own docstring) from word geometry.
    Real LaMA layout, confirmed by direct inspection: the right side
    lists every option A, B, C... top to bottom (its own enumeration,
    unrelated to correctness, always plain black); a SOLVED table
    additionally shows, next to each left-side term, which right-side
    letter is that term's correct match — and that "your answer" letter
    is colored RED, distinctly from the black enumeration letters (see
    _is_red()). Color is used as the primary split signal since it's
    semantically direct; x-position clustering is kept as a fallback for
    whatever export style doesn't color-code this. Returns
    ((pairs, distractors), reason), or (None, reason) if the geometry
    doesn't look like a solved matching table at all (so the caller
    skips it rather than guess)."""
    letters = [w for w in ex.words if _SINGLE_LETTER_RE.match(w.text)]
    if len(letters) < 4:
        return None, "too few A-F letter tokens found to be a solved matching table"

    answer_letters = sorted((w for w in letters if w.red), key=lambda w: w.top)
    option_letters = sorted((w for w in letters if not w.red), key=lambda w: w.top)
    left_max = right_min = None
    if not answer_letters or not option_letters:
        # No color signal available (or every letter came out one color) —
        # fall back to the original position-clustering heuristic.
        split = _split_x_clusters([w.x0 for w in letters])
        if split is None:
            return None, "matching table has only one letter column (looks unsolved — no answer key visible) and no red/black color split either"
        left_max, right_min = split
        answer_letters = sorted((w for w in letters if w.x0 <= left_max), key=lambda w: w.top)
        option_letters = sorted((w for w in letters if w.x0 >= right_min), key=lambda w: w.top)
    if not answer_letters or not option_letters:
        return None, "letter clustering didn't produce both an answer column and an option column"
    if left_max is None:
        # Color-based split found both groups — still need an x boundary
        # to bound each side's term-text slice (see slice_for below).
        left_max = max(w.x0 for w in answer_letters) + 1.0
        right_min = min(w.x0 for w in option_letters) - 1.0

    def slice_for(anchor_list: list[Word], idx: int, bound_x_low: float | None, bound_x_high: float | None) -> str:
        y0 = anchor_list[idx].top - 3.0
        y1 = anchor_list[idx + 1].top - 3.0 if idx + 1 < len(anchor_list) else max(w.bottom for w in ex.words) + 1
        chunk = [
            w for w in ex.words
            if y0 <= w.top < y1 and not _SINGLE_LETTER_RE.match(w.text)
            and (bound_x_low is None or w.x0 >= bound_x_low)
            and (bound_x_high is None or w.x0 <= bound_x_high)
        ]
        return _row_text(chunk)

    option_text_by_letter = {
        option_letters[i].text: slice_for(option_letters, i, right_min, None)
        for i in range(len(option_letters))
    }

    pairs: list[dict] = []
    used_letters: set[str] = set()
    for i, letter_word in enumerate(answer_letters):
        left_text = slice_for(answer_letters, i, None, left_max)
        correct_letter = letter_word.text
        right_text = option_text_by_letter.get(correct_letter, "")
        if left_text and right_text:
            pairs.append({"left": left_text, "right": right_text})
            used_letters.add(correct_letter)

    if len(pairs) < 2:
        return None, "fewer than 2 usable left/right term pairs reconstructed"

    distractors = [text for letter, text in option_text_by_letter.items() if letter not in used_letters and text]
    reason = "matching pairs reconstructed from table geometry (color-coded answer key), verify the (garbled-notation-prone) term text"
    if distractors:
        reason += f"; {len(distractors)} unused right-side option(s) imported as distractors"
    return (pairs, distractors), reason


def _parse_multi_select(ex: RawExercise) -> tuple[Optional[list[str]], str]:
    """Splits a "Mutiple Choice" exercise's content into an intro
    sentence (discarded — the question TEXT itself is Titel, same as
    every other type here) and a list of option terms, one per visually
    separated block. No correct-answer marking is recoverable for this
    Aufgabenformat AT ALL (see this module's own docstring) — this
    function only ever returns option TEXT, never a correct_indices
    guess."""
    rows = _cluster_rows(ex.words, gap=6.0)
    if not rows:
        return None, "no content rows found"
    # Group rows into blocks separated by a bigger vertical gap — that
    # gap is what visually distinguishes one option from the next (a
    # single option's own stacked fraction/exponent still uses the
    # smaller row-gap and stays inside one block).
    blocks: list[list[list[Word]]] = [[rows[0]]]
    for prev, row in zip(rows, rows[1:]):
        gap = row[0].top - prev[-1].top
        if gap > 15.0:
            blocks.append([row])
        else:
            blocks[-1].append(row)
    if len(blocks) < 3:  # intro sentence + at least 2 options
        return None, "fewer than 2 option blocks found"
    # The instruction sentence ("Kreuze die beiden Terme an, die...")
    # sometimes wraps across MORE than one visually-separated block —
    # taking only blocks[0] as "the intro" then leaked its own later
    # line(s) in as fake options. A real option is always a short math
    # expression that never ends in sentence punctuation, while a
    # wrapped intro's OWN LAST line does — so the intro is every LEADING
    # block up to and including the first one that ends that way (a
    # mid-sentence wrapped line won't end in punctuation itself, so this
    # has to scan forward for the terminator, not stop at the first
    # block that lacks one).
    block_texts = [" ".join(_row_text(row) for row in block).strip() for block in blocks]
    intro_len = 1
    for idx, text in enumerate(block_texts):
        if text.endswith(("!", "?", ".")):
            intro_len = idx + 1
            break
    options = [o for o in block_texts[intro_len:] if o]
    if len(options) < 2:
        return None, "fewer than 2 non-empty options after the intro sentence"
    return options, "no correct answer is recoverable from a LaMA Mutiple Choice export — imported ungraded, tick the correct option(s) before use"


def _parse_free_response(ex: RawExercise) -> tuple[Optional[str], str]:
    """The accepted answer for a SOLVED Offenes Antwortformat exercise is
    LaMA's own RED-colored text (see this module's docstring) — the
    problem statement itself stays black. Preferred over the older
    "last short line" guess since it directly distinguishes the final
    answer from an intermediate step in a multi-step derivation, not
    just from the problem text. Falls back to the position-based guess
    when no red text is present at all (an unsolved-style export, or a
    LaMA theme that doesn't color-code this)."""
    red_words = [w for w in ex.words if w.red]
    if red_words:
        candidate = _row_text(sorted(red_words, key=lambda w: (w.top, w.x0))).strip()
        if candidate and len(candidate) <= 80:
            if _NUMERIC_ANSWER_RE.match(candidate.replace(" ", "")):
                return candidate, "clean numeric answer, high confidence (LaMA's own color-coded solution)"
            return candidate, "answer extracted from LaMA's own color-coded solution text — still verify math notation (superscripts/fraction bars often mangle in plain extraction)"
        # Red text present but too long/empty to trust as a single short
        # answer (probably a multi-step derivation shown in red, not just
        # the final value) — fall through to the plain last-line guess.

    rows = _cluster_rows(ex.words, gap=6.0)
    if not rows:
        return None, "no content rows found"
    candidate = _row_text(rows[-1]).strip()
    if not candidate or len(candidate) > 60:
        return None, "no short final-answer line found (likely a multi-line derivation, not a single computable answer)"
    if _NUMERIC_ANSWER_RE.match(candidate.replace(" ", "")):
        return candidate, "clean numeric answer, high confidence"
    return candidate, "answer extracted from math notation that plain text extraction often mangles (lost superscripts/fraction bars) — verify before use"


_COMPETENCY_CODE_RE = re.compile(r"^[A-ZÄÖÜ]{2,6}$")


def _strip_competency_code_noise(ex: RawExercise) -> RawExercise:
    """Every page repeats its own Grundkompetenz code (e.g. "PWU") as a
    running header ("PWU - 6 - Umformung eines Terms") and again as a
    small label next to the per-exercise point-counter — both pure page
    decoration, already captured properly via the metadata block, but
    landing close enough to real content vertically that row/block
    clustering elsewhere in this file can't tell them apart from it.
    Stripped here by finding this exercise's OWN Grundkompetenz code
    tokens (split out of its "Grundkompetenz(en)" field — handles both
    a bare code like "PWU" and a comma list like "AN 1.1, AN 3.3") and
    dropping any content word that's an exact, case-sensitive match for
    one of them — safe because a real math term is never literally just
    the bare letters of a competency code standing alone."""
    codes = {
        tok for tok in re.split(r"[\s,]+", ex.meta.get("Grundkompetenz(en)", ""))
        if _COMPETENCY_CODE_RE.match(tok)
    }
    if not codes:
        return ex
    return RawExercise(words=[w for w in ex.words if w.text not in codes], meta=ex.meta)


_AI_REVIEW_PREFIX = "AI-assisted extraction (Gemini) -- geometry-based parsing failed here; REQUIRES teacher review before use"


def _exercise_plain_text(ex: RawExercise) -> str:
    """Plain reading-order text of one exercise's own content (metadata
    already stripped out by _segment_exercises) -- good enough for an
    LLM to read even though it loses exactly the column/color structure
    the geometry-based parsers above rely on. That's the whole point:
    this is only ever tried once THAT structure-dependent parsing has
    already failed, so a structure-blind plain-text view is a genuinely
    different (and sometimes recoverable) signal, not a strictly worse
    version of the same one."""
    return "\n".join(_row_text(row) for row in _cluster_rows(ex.words, gap=6.0))


def _ai_json_reply(ai_fallback: AiFallback, prompt: str) -> Optional[dict]:
    """Calls ai_fallback, tolerates the model wrapping its JSON in a
    markdown code fence (a real, common Gemini habit, not a hypothetical)
    -- returns the parsed dict, or None on any failure (network, timeout,
    invalid JSON, wrong shape). Never raises; a fallback that fails is
    just another reason to skip, same as the geometry parser failing."""
    reply = ai_fallback(prompt)
    if not reply:
        return None
    text = reply.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _ai_extract_free_response(ex: RawExercise, ai_fallback: AiFallback) -> tuple[Optional[str], str]:
    prompt = (
        "This is one exercise from a solved German-language math worksheet "
        "(Aufgabenformat: Offenes Antwortformat -- a free-text numeric/short "
        "answer). Find the final answer actually given/marked in the text "
        "below (not an intermediate step). Reply with ONLY a JSON object: "
        '{"answer": "<the short final answer, as plain text>"} -- or '
        '{"answer": null} if no clear final answer is present.\n\n'
        f"Exercise text:\n{_exercise_plain_text(ex)}"
    )
    parsed = _ai_json_reply(ai_fallback, prompt)
    if not parsed or not parsed.get("answer"):
        return None, "AI fallback also found no recoverable final answer"
    return str(parsed["answer"]).strip(), _AI_REVIEW_PREFIX


def _ai_extract_multi_select(ex: RawExercise, ai_fallback: AiFallback) -> tuple[Optional[list[str]], str]:
    prompt = (
        "This is one exercise from a German-language math worksheet "
        "(Aufgabenformat: Mutiple Choice). List every answer OPTION text "
        "(not the instruction sentence itself). No option is marked correct "
        "in this format -- don't guess one. Reply with ONLY a JSON object: "
        '{"options": ["...", "...", ...]} with at least 2 options, or '
        '{"options": null} if you can\'t confidently separate options from '
        "the instruction text.\n\n"
        f"Exercise text:\n{_exercise_plain_text(ex)}"
    )
    parsed = _ai_json_reply(ai_fallback, prompt)
    options = parsed.get("options") if parsed else None
    if not isinstance(options, list) or len(options) < 2 or not all(isinstance(o, str) and o.strip() for o in options):
        return None, "AI fallback also couldn't separate at least 2 real options from the instruction text"
    return [o.strip() for o in options], _AI_REVIEW_PREFIX


def _ai_extract_matching_pair(ex: RawExercise, ai_fallback: AiFallback) -> tuple[Optional[tuple[list[dict], list[str]]], str]:
    prompt = (
        "This is one exercise from a solved German-language math worksheet "
        "(Aufgabenformat: Zuordnungsformat -- a matching table). Find every "
        "left-side term and its correctly-matched right-side term (the "
        "marked/solved answer), PLUS any right-side option that is a real "
        "distractor (never the correct match for any left-side term -- "
        "these are a deliberate feature of the format, not noise). Reply "
        "with ONLY a JSON object: {\"pairs\": [{\"left\": \"...\", "
        '"right": "..."}, ...], "distractors": ["...", ...]} (distractors '
        'may be an empty list), or {"pairs": null} if you can\'t confidently '
        "tell which right-side term matches which left-side term.\n\n"
        f"Exercise text:\n{_exercise_plain_text(ex)}"
    )
    parsed = _ai_json_reply(ai_fallback, prompt)
    pairs = parsed.get("pairs") if parsed else None
    if not isinstance(pairs, list) or len(pairs) < 2:
        return None, "AI fallback also couldn't confidently pair up this matching table"
    if not all(isinstance(p, dict) and p.get("left") and p.get("right") for p in pairs):
        return None, "AI fallback returned pairs missing a left or right term"
    clean_pairs = [{"left": str(p["left"]).strip(), "right": str(p["right"]).strip()} for p in pairs]
    distractors = parsed.get("distractors") if isinstance(parsed.get("distractors"), list) else []
    clean_distractors = [str(d).strip() for d in distractors if isinstance(d, str) and d.strip()]
    return (clean_pairs, clean_distractors), _AI_REVIEW_PREFIX


def parse_lama_pdf(
    pdf_path: str,
    default_tier: str = "medium",
    ai_fallback: Optional[AiFallback] = None,
    on_progress: Optional[Callable[[int, int], bool]] = None,
) -> ParseResult:
    """`ai_fallback`, if given, is tried ONLY after the geometry/color-
    based parser below already failed for a SUPPORTED Aufgabenformat --
    never for one of the genuinely unsupported formats (Lückentext, a
    BIFIE Oberstufe rubric question, ...), since AI recovering an answer
    there would need a whole new target question TYPE this system
    doesn't have yet, not just better extraction into an existing one.
    This keeps the already-reliable geometry parser as the one and only
    PRIMARY path (never bypassed, never second-guessed when it already
    succeeded) and AI strictly as a second chance for a real, common
    failure mode: the heuristic's positional/color assumptions not
    matching this particular PDF's exact layout.

    `on_progress`, if given, is called (exercises_done, exercises_total)
    after every single exercise in THIS file — the real per-file exercise
    count, known once `_segment_exercises` has run, is what makes a large
    single PDF (many exercises, each possibly slow due to an AI-fallback
    round trip) show real incremental progress instead of a single
    file-level "done" flip at the very end (see lama_bank.py's own
    scan_watch_dir, whose per-FILE granularity alone is too coarse for
    exactly that case). Returning False from it is this parser's
    cancellation mechanism — checked at the same natural per-exercise
    checkpoint, so a cancel request lands within one exercise's worth of
    latency even mid-file, not just between files."""
    words = _load_words(pdf_path)
    exercises = [_strip_competency_code_noise(ex) for ex in _segment_exercises(words)]
    total_ex = len(exercises)
    result = ParseResult()

    def _tick(i: int) -> bool:
        return on_progress is None or on_progress(i, total_ex)

    for i, ex in enumerate(exercises, start=1):
        fmt = ex.meta.get("Aufgabenformat", "").strip()
        source = {k: ex.meta.get(k, "") for k in METADATA_FIELDS}
        if fmt not in _SUPPORTED_FORMATS:
            result.skipped.append(SkippedExercise(
                reason=f"Aufgabenformat {fmt!r} has no supported target question type (only free-text, multiple-choice, and matching are importable)",
                source=source,
            ))
            if not _tick(i):
                break
            continue

        title = ex.meta.get("Titel", "").strip() or "Imported question"

        if fmt == "Offenes Antwortformat":
            answer, reason = _parse_free_response(ex)
            if answer is None and ai_fallback is not None:
                answer, reason = _ai_extract_free_response(ex, ai_fallback)
            if answer is None:
                result.skipped.append(SkippedExercise(reason=reason, source=source))
                if not _tick(i):
                    break
                continue
            question = {
                "tier": default_tier, "type": "free_response", "text": title,
                "accepted_answers": [answer], "points": 1,
            }
            result.imported.append(ParsedQuestion(question=question, review_reason=reason, source=source))

        elif fmt == "Mutiple Choice":
            options, reason = _parse_multi_select(ex)
            if options is None and ai_fallback is not None:
                options, reason = _ai_extract_multi_select(ex, ai_fallback)
            if options is None:
                result.skipped.append(SkippedExercise(reason=reason, source=source))
                if not _tick(i):
                    break
                continue
            question = {
                "tier": default_tier, "type": "multi_select", "text": title,
                "options": options, "correct_indices": None, "points": 1,
            }
            result.imported.append(ParsedQuestion(question=question, review_reason=reason, source=source))

        else:  # Zuordnungsformat
            parsed, reason = _parse_matching_pair(ex)
            if parsed is None and ai_fallback is not None:
                parsed, reason = _ai_extract_matching_pair(ex, ai_fallback)
            if parsed is None:
                result.skipped.append(SkippedExercise(reason=reason, source=source))
                if not _tick(i):
                    break
                continue
            pairs, distractors = parsed
            if len(distractors) > _MAX_DISTRACTORS:
                distractors = distractors[:_MAX_DISTRACTORS]
                reason += f" (truncated to {_MAX_DISTRACTORS} distractors, our schema's own cap)"
            question = {
                "tier": default_tier, "type": "matching_pair", "text": title,
                "pairs": pairs, "distractors": distractors or None, "points": 1,
            }
            result.imported.append(ParsedQuestion(question=question, review_reason=reason, source=source))

        if not _tick(i):
            break

    return result


def to_import_questions_payload(result: ParseResult, chapter_name: Optional[str] = None) -> dict:
    """Shapes a ParseResult into exactly the body authoring_command's
    "import_questions" expects (see math_teacher.py) — hand this
    straight to a World.set_attribute(control, class_attr("authoring_command"), ...)
    call, or to a future "import from PDF" button doing the same."""
    payload: dict[str, Any] = {"questions": [pq.question for pq in result.imported]}
    if chapter_name:
        payload["new_chapter_name"] = chapter_name
    return payload


def _main() -> None:
    if len(sys.argv) < 2:
        print("usage: python lama_parser.py <path-to-lama-pdf> [output.json]", file=sys.stderr)
        raise SystemExit(2)
    pdf_path = sys.argv[1]
    result = parse_lama_pdf(pdf_path)

    print(f"Parsed {Path(pdf_path).name}: {len(result.imported)} imported (all need review before use), {len(result.skipped)} skipped\n")

    by_type: dict[str, int] = {}
    for pq in result.imported:
        by_type[pq.question["type"]] = by_type.get(pq.question["type"], 0) + 1
    for t, count in sorted(by_type.items()):
        print(f"  imported as {t}: {count}")

    by_reason: dict[str, int] = {}
    for sk in result.skipped:
        by_reason[sk.reason] = by_reason.get(sk.reason, 0) + 1
    if by_reason:
        print("\nskipped, by reason:")
        for reason, count in sorted(by_reason.items(), key=lambda kv: -kv[1]):
            print(f"  {count:3d}  {reason}")

    if len(sys.argv) >= 3:
        out_path = sys.argv[2]
        payload = to_import_questions_payload(result)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"\nWrote {len(payload['questions'])} questions to {out_path} (import_questions-ready)")


if __name__ == "__main__":
    _main()
