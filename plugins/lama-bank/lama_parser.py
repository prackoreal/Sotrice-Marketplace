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

- A LaMA "Mutiple Choice" (their own spelling) exercise is a table, one
  row per option. In a SOLVED export the correct rows carry a checked box
  in a narrow right-hand column -- but not as text, and not as a vector
  shape pdfminer reports: the table borders are 1px embedded images and
  the box only exists in the RENDERED page. (An earlier version of this
  docstring claimed the marking was unrecoverable, after checking only the
  embedded images.) read_choice_table() reads it the way a person does:
  rows from the table borders, ticks by rendering each checkbox cell. When
  that can't be read with confidence (no table, a table split across
  pages, nothing/everything ticked, a count that contradicts "die
  beiden"), the question imports with correct_indices None and
  lama_bank.py asks the AI for a draft key instead (see needs_ai_key).
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
    size: float = 12.0  # font size in pt — what tells a superscript/subscript from base-line text, see _layout_text()
    page: int = 0  # 0-based page index -- where to find this word again when rendering an exercise as an image
    x1: float = 0.0  # right edge — lets _layout_text() tell a root index sitting over a radical from an ordinary exponent after it


@dataclass
class RawExercise:
    words: list[Word]  # the exercise's own content, NOT including its metadata block
    meta: dict[str, str]


# Where a parsed question's ANSWER KEY (not its text) came from -- what
# decides whether it needs an AI-drafted key (see needs_ai_key) and how the
# catalog labels it. Deliberately separate from review_reason, which is
# free-form prose for a human: the catalog used to render ANY non-empty
# review_reason as a red "Needs review", and since every question has one
# (even a clean numeric answer read straight from LaMa's own solution
# colour), every row was red.
ANSWER_PDF = "pdf"  # read from LaMa's own color-coded solution/answer marking -- trusted as-is
ANSWER_GUESS = "pdf_guess"  # free_response only: no color marking, the last short line was GUESSED to be the answer
ANSWER_AI_EXTRACT = "ai_extract"  # the whole structure (pairs/answer) was recovered by the AI fallback, not geometry
ANSWER_NONE = "none"  # no answer key at all (every LaMa "Mutiple Choice") -- imports ungraded unless an AI key is added later


@dataclass
class ParsedQuestion:
    question: dict[str, Any]  # ready for import_questions' own "questions" list item shape
    review_reason: str
    source: dict[str, str]  # Titel/Grundkompetenz(en)/Klasse/Quelle, for the teacher's own reference
    answer_source: str = ANSWER_PDF  # see ANSWER_* above
    # What an LLM needs to DRAFT an answer key for this question, only
    # filled in when answer_source says one is wanted (see needs_ai_key):
    # {"question": layout text of the problem statement, "options":
    # per-option layout text (multi_select) | None, "current_answer": the
    # guessed answer (ANSWER_GUESS) | None}. Layout text, not the plain
    # question text -- see _layout_text on why they differ.
    ai_context: Optional[dict[str, Any]] = None


@dataclass
class SkippedExercise:
    reason: str
    source: dict[str, str]


# Bump whenever parse_lama_pdf's OUTPUT for the same PDF changes (new
# fields, better extraction, a fixed bug) -- lama_bank.py's on-disk parse
# cache is keyed by it, so a stale cached parse never outlives the code
# that would now produce something different.
PARSER_CACHE_VERSION = 7


@dataclass
class ParseResult:
    imported: list[ParsedQuestion] = field(default_factory=list)
    skipped: list[SkippedExercise] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "imported": [
                {
                    "question": pq.question, "review_reason": pq.review_reason, "source": pq.source,
                    "answer_source": pq.answer_source, "ai_context": pq.ai_context,
                }
                for pq in self.imported
            ],
            "skipped": [{"reason": sk.reason, "source": sk.source} for sk in self.skipped],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ParseResult":
        return cls(
            imported=[
                ParsedQuestion(
                    question=e["question"], review_reason=e["review_reason"], source=e["source"],
                    answer_source=e["answer_source"], ai_context=e["ai_context"],
                )
                for e in data["imported"]
            ],
            skipped=[SkippedExercise(reason=e["reason"], source=e["source"]) for e in data["skipped"]],
        )


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
        for page_index, page in enumerate(pdf.pages):
            for w in page.extract_words(extra_attrs=["non_stroking_color", "size"]):
                if _is_watermark_word(w):
                    continue
                words.append(Word(
                    text=w["text"], x0=w["x0"], top=w["top"], bottom=w["bottom"],
                    red=_is_red(w.get("non_stroking_color")),
                    size=float(w.get("size") or 12.0),
                    x1=float(w.get("x1") or w["x0"]),
                    page=page_index,
                ))
    return words


_MAX_TITLE_TAIL_WORDS = 12


def _drop_previous_title_tail(content: list[Word]) -> list[Word]:
    """A metadata block's title can wrap past the bit the field window
    reads ("... Matura Haupt- termin 16/17)"), and when that block ends a
    page, the wrapped tail is left at the top of the NEXT page -- where it
    lands at the front of the following exercise's content. Those few
    stray words made roughly half of a real 314-exercise PDF look like it
    spanned two pages, which silently cost every one of them its choice
    table and its picture. Real two-page exercises have a whole page of
    content, not a title fragment, so a short run on an earlier page
    followed by a longer one is dropped."""
    if not content:
        return content
    first = content[0].page
    lead = 0
    while lead < len(content) and content[lead].page == first:
        lead += 1
    if lead < len(content) and lead <= _MAX_TITLE_TAIL_WORDS and len(content) - lead > lead:
        return content[lead:]
    return content


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
        exercises.append(RawExercise(words=_drop_previous_title_tail(words[content_start:i]), meta=meta))
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
    reason = "math notation in these terms may be garbled by the PDF's text extraction"
    if distractors:
        reason += f"; {len(distractors)} unused right-side option(s) imported as distractors"
    return (pairs, distractors), reason


def _multi_select_blocks(ex: RawExercise) -> Optional[tuple[list[str], int]]:
    split = _multi_select_block_words(ex)
    return None if split is None else (split[0], split[1])


def _multi_select_block_words(ex: RawExercise) -> Optional[tuple[list[str], int, list[list[Word]]]]:
    """Shared block-splitting used by both _parse_multi_select (the
    options) and _question_text (the discarded intro sentence) — one
    visually separated block per option, with the instruction sentence
    possibly wrapping across more than one of them. Returns
    (block_texts, intro_len), or None if there's no usable block
    structure at all (fewer than an intro + 2 options)."""
    rows = _cluster_rows(ex.words, gap=6.0)
    if not rows:
        return None
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
        return None
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
    return block_texts, intro_len, [[w for row in block for w in row] for block in blocks]


def _parse_multi_select(ex: RawExercise) -> tuple[Optional[list[str]], str]:
    """Splits a "Mutiple Choice" exercise's content into an intro
    sentence (see _question_text — used as the imported question's real
    text, not discarded) and a list of option terms, one per visually
    separated block. No correct-answer marking is recoverable for this
    Aufgabenformat AT ALL (see this module's own docstring) — this
    function only ever returns option TEXT, never a correct_indices
    guess."""
    split = _multi_select_blocks(ex)
    if split is None:
        return None, "fewer than 2 option blocks found"
    block_texts, intro_len = split
    options = [o for o in block_texts[intro_len:] if o]
    if len(options) < 2:
        return None, "fewer than 2 non-empty options after the intro sentence"
    return options, ""  # no answer key in this format is the NORMAL case -- carried by answer_source, not prose


def _question_text(ex: RawExercise, fmt: str) -> str:
    """The exercise's own problem statement, used as the imported
    question's "text" field — Titel (ex.meta's "Titel" field) is only a
    short catalog label (e.g. "Potenzgesetze 3"), never the actual
    question being asked, so using it as "text" made every imported
    question unreadable regardless of type. Independent of however the
    answer/options themselves were extracted (geometry or AI fallback)
    — this only ever needs the INTRO part of the exercise, a strictly
    easier job than finding the final answer or every option. Falls
    back to the full exercise text (never to Titel) if the type-specific
    intro heuristic below doesn't apply cleanly, so a messy PDF still
    gets SOME real question text rather than none."""
    rows = _cluster_rows(ex.words, gap=6.0)
    if not rows:
        return ""
    if fmt == "Mutiple Choice":
        split = _multi_select_blocks(ex)
        if split is not None:
            block_texts, intro_len = split
            intro = " ".join(block_texts[:intro_len]).strip()
            if intro:
                return intro
        return " ".join(_row_text(r) for r in rows).strip()
    if fmt == "Zuordnungsformat":
        out = []
        for row in rows:
            if any(_SINGLE_LETTER_RE.match(w.text) for w in row):
                break
            out.append(_row_text(row))
        intro = " ".join(t for t in out if t).strip()
        return intro or " ".join(_row_text(r) for r in rows).strip()
    # Offenes Antwortformat — everything except the row(s) holding the
    # final answer (LaMA's own red-colored solution text, or — when no
    # color signal is present — the last content row, mirroring
    # _parse_free_response's own two-tier answer heuristic exactly, so
    # the text excluded here always matches what actually became the
    # "accepted_answers" entry instead of guessing independently).
    red_row_idx = {i for i, row in enumerate(rows) if any(w.red for w in row)}
    kept = [r for i, r in enumerate(rows) if i not in red_row_idx] if red_row_idx else rows[:-1]
    intro = " ".join(_row_text(r) for r in kept).strip()
    return intro or " ".join(_row_text(r) for r in rows).strip()


def _parse_free_response(ex: RawExercise) -> tuple[Optional[str], str, str]:
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
                return candidate, "", ANSWER_PDF
            # Red text that isn't a plain number is flattened math ("√x 5 2",
            # "m 4x"): a student can never type that, so grading against it
            # would mark every correct answer wrong. It is only a GUESS --
            # ungraded, handed to the AI keying / the teacher's review.
            return candidate, "math notation in this answer is garbled by the PDF's text extraction (superscripts/fraction bars), so it can't be used as an answer key", ANSWER_GUESS
        # Red text present but too long/empty to trust as a single short
        # answer (probably a multi-step derivation shown in red, not just
        # the final value) — fall through to the plain last-line guess.

    rows = _cluster_rows(ex.words, gap=6.0)
    if not rows:
        return None, "no content rows found", ANSWER_NONE
    candidate = _row_text(rows[-1]).strip()
    if not candidate or len(candidate) > 60:
        return None, "no short final-answer line found (likely a multi-line derivation, not a single computable answer)", ANSWER_NONE
    # No color marking anywhere, so "the last short line" is a GUESS --
    # even when it happens to look like a clean number (it may just as
    # well be the last number in the problem statement).
    if _NUMERIC_ANSWER_RE.match(candidate.replace(" ", "")):
        return candidate, "answer guessed from the last line (no color-coded solution in this PDF) — plausible number, not verified", ANSWER_GUESS
    return candidate, "answer guessed from math notation that plain text extraction often mangles (lost superscripts/fraction bars) — verify before use", ANSWER_GUESS


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


def _region(words: list[Word]) -> Optional[dict[str, Any]]:
    """Where an exercise sits on its page ({"page", "bbox": [x0, top, x1,
    bottom]} in PDF points), so it can be rendered back to an image for
    the AI (render_region_png). None for an exercise spanning pages --
    those just go to the AI as text."""
    if not words or len({w.page for w in words}) != 1:
        return None
    return {
        "page": words[0].page,
        "bbox": [
            round(min(w.x0 for w in words) - 6, 1), round(min(w.top for w in words) - 6, 1),
            round(max(w.x1 for w in words) + 6, 1), round(max(w.bottom for w in words) + 6, 1),
        ],
    }


# One Gemini image "tile" is 768px on a side (~258 tokens); an exercise that
# fits one or two tiles costs next to nothing, a full-page scan does not.
_IMAGE_MAX_WIDTH_PX = 768
_IMAGE_MAX_HEIGHT_PX = 1536


def render_region_png(pdf: Any, region: dict[str, Any]) -> Optional[bytes]:
    """The exercise as a small grayscale PNG (cropped to just its own
    region, at most 768x1536 px = 1-2 Gemini tiles), or None if it can't
    be rendered. `pdf` is an already-open pdfplumber PDF -- the caller
    opens each source file once per scan, not once per exercise. This is
    what lets the model read real fractions/exponents/roots instead of
    the flattened text."""
    try:
        import io
        page = pdf.pages[region["page"]]
        x0, top, x1, bottom = region["bbox"]
        x0, top = max(0.0, x0), max(0.0, top)
        x1, bottom = min(float(page.width), x1), min(float(page.height), bottom)
        if x1 - x0 < 10 or bottom - top < 10:
            return None
        scale = min(_IMAGE_MAX_WIDTH_PX / (x1 - x0), _IMAGE_MAX_HEIGHT_PX / (bottom - top), 2.0)
        img = page.crop((x0, top, x1, bottom)).to_image(resolution=max(40, int(72 * scale))).original.convert("L")
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        return buf.getvalue()
    except Exception:  # noqa: BLE001 - a render failure only costs this exercise its picture
        return None


def _center(w: Word) -> float:
    return (w.top + w.bottom) / 2


def _layout_text(words: list[Word]) -> str:
    """Reading-order text of one exercise WITH its math layout recovered
    from font size + position -- the structure plain row text throws away
    ("x 6 z y 15 − 9 · z 2 y x 6" is really x^6 z^y ... once you know the
    6 and the y are smaller and raised). Only ever fed to an LLM (see
    build_key_prompt), never shown to a teacher as the question text:
    notation is a best-effort hint, not a typesetter.

    Notation used in the output (and explained to the model in the
    prompt): `^{...}` a superscript run, `_{...}` a subscript run, `(a/b)`
    two small tokens stacked at the same x (a fraction inside an
    exponent), `√[n]` a root index sitting over a radical, and
    `{top} over {bottom}` two short base-size rows stacked over each
    other (a displayed fraction -- the fraction bar itself is not text,
    so this is inferred from stacking alone)."""
    if not words:
        return ""
    weight: dict[int, int] = {}
    for w in words:
        weight[round(w.size)] = weight.get(round(w.size), 0) + len(w.text)
    base = float(max(weight, key=lambda k: weight[k]))
    cutoff = 0.85 * base
    big = [w for w in words if w.size >= cutoff]
    small = [w for w in words if w.size < cutoff]
    rows = _cluster_rows(big, gap=6.0)
    if not rows:
        rows = _cluster_rows(words, gap=6.0)
        small = []
    refs = [sorted(_center(w) for w in row)[len(row) // 2] for row in rows]
    per_row: list[list[Word]] = [list(row) for row in rows]
    orphans: list[Word] = []
    for w in small:
        i = min(range(len(rows)), key=lambda k: abs(_center(w) - refs[k]))
        if abs(_center(w) - refs[i]) <= 1.4 * base:
            per_row[i].append(w)
        else:
            orphans.append(w)
    if orphans:
        # A script with no base-size row near it -- keep it, just as its
        # own line rather than silently dropped.
        extra = _cluster_rows(orphans, gap=6.0)
        for row in extra:
            per_row.append(row)
            refs.append(sorted(_center(w) for w in row)[len(row) // 2])

    def render_row(row: list[Word], ref: float) -> str:
        tokens = sorted(row, key=lambda w: w.x0)
        bigs = [t for t in tokens if t.size >= cutoff]
        index_for: dict[int, list[Word]] = {}  # id(big token) -> root-index words over it
        consumed: set[int] = set()
        for t in tokens:
            if t.size >= cutoff:
                continue
            for b in bigs:
                if b.text.startswith("√") and b.x0 - 1.0 <= t.x0 <= b.x1 and _center(t) < _center(b):
                    index_for.setdefault(id(b), []).append(t)
                    consumed.add(id(t))
                    break
        out: list[str] = []
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if id(t) in consumed:
                i += 1
                continue
            if t.size >= cutoff:
                text = t.text
                idx = index_for.get(id(t))
                if idx:
                    text = "√[" + " ".join(w.text for w in sorted(idx, key=lambda w: (w.x0, w.top))) + "]" + text[1:]
                out.append(text)
                i += 1
                continue
            run = [t]
            j = i + 1
            while j < len(tokens):
                nxt = tokens[j]
                if id(nxt) in consumed:
                    j += 1
                    continue
                if nxt.size >= cutoff or nxt.x0 - max(r.x1 for r in run) > 0.6 * base:
                    break
                run.append(nxt)
                j += 1
            columns: list[list[Word]] = []
            for w in sorted(run, key=lambda w: w.x0):
                if columns and abs(columns[-1][0].x0 - w.x0) <= 2.0:
                    columns[-1].append(w)
                else:
                    columns.append([w])
            parts = [
                col[0].text if len(col) == 1 else "(" + "/".join(c.text for c in sorted(col, key=lambda c: c.top)) + ")"
                for col in columns
            ]
            run_center = (min(w.top for w in run) + max(w.bottom for w in run)) / 2
            mark = "_" if run_center - ref > 0.08 * base else "^"
            out.append(f"{mark}{{{' '.join(parts)}}}")
            i = j
        return " ".join(out)

    order = sorted(range(len(per_row)), key=lambda k: refs[k])
    rendered = [(render_row(per_row[k], refs[k]), per_row[k], refs[k]) for k in order]
    lines: list[str] = []
    k = 0
    while k < len(rendered):
        text, row, ref = rendered[k]
        if k + 1 < len(rendered):
            ntext, nrow, nref = rendered[k + 1]
            x_lo, x_hi = min(w.x0 for w in row), max(w.x1 for w in row)
            nx_lo, nx_hi = min(w.x0 for w in nrow), max(w.x1 for w in nrow)
            overlap = min(x_hi, nx_hi) - max(x_lo, nx_lo)
            narrow = min(x_hi - x_lo, nx_hi - nx_lo)
            if len(row) <= 5 and len(nrow) <= 5 and narrow > 0 and overlap >= 0.5 * narrow and (nref - ref) < 1.8 * base:
                lines.append(f"{{{text}}} over {{{ntext}}}")
                k += 2
                continue
        lines.append(text)
        k += 1
    return "\n".join(lines)


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


def _multi_select_context(ex: RawExercise, options: list[str], geometry_options: bool) -> dict[str, Any]:
    """The AI-facing view of a multi_select exercise (see ParsedQuestion's
    ai_context). When the options came from the geometry parser, each
    option's OWN words get their own layout text, aligned 1:1 with
    `options` -- so "option index 2" means the same thing to the model as
    to the question it will later be graded against. If the AI fallback
    produced the options instead (no block structure to key off), the
    whole exercise's layout text is all there is to offer."""
    split = _multi_select_block_words(ex) if geometry_options else None
    if split is not None:
        block_texts, intro_len, block_words = split
        kept = [i for i in range(intro_len, len(block_texts)) if block_texts[i]]
        if len(kept) == len(options):
            return {
                "question": _layout_text([w for i in range(intro_len) for w in block_words[i]]),
                "options": [_layout_text(block_words[i]) for i in kept],
                "current_answer": None,
                "region": _region(ex.words),
            }
    return {"question": _layout_text(ex.words), "options": None, "current_answer": None, "region": _region(ex.words)}


_CHOICE_INSTRUCTION_RE = re.compile(r"put a cross|cross next|place a cross|kreuze|ankreuz|^\s*(for which|which of)", re.I | re.M)
_INTERPRETIVE_RE = re.compile(
    r"\b(interpret|describe|explain|justify|give a reason|state what|write down what|what (this|the) (value|result|expression)"
    r"|interpretier|beschreib|erkl[aä]r|begr[uü]nd)",
    re.I,
)
_SELF_CORRECTION_RE = re.compile(r"\bwait\b|let me re-?(evaluate|check|think)|\bhmm\b|not indexed|options? (are|is) likely", re.I)  # a rationale that argues with itself is not a confident key
_MAX_TRUSTED_OPTIONS = 6  # LaMa multiple choice has 4-5 rows; more means the splitter swallowed diagram labels or the intro


def _fix_mojibake(text: str) -> str:
    """Undo UTF-8 read as Latin-1 ("213.69Â°" -> "213.69°") -- a real
    artifact seen in model replies, and an answer key with it would never
    match what a student types."""
    if "Ã" not in text and "Â" not in text:
        return text
    try:
        return text.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _key_is_gradeable(pq: ParsedQuestion) -> bool:
    """False for exercises where an AI key would only be a confident-looking
    guess, so the question stays calmly "waiting for an answer key" instead:
      - multiple choice whose option list can't be trusted to line up with
        the picture (no per-option split, more rows than LaMa ever prints,
        or the instruction line itself sitting among the options) -- spot
        checks showed the model keys those anyway, against garbage indexes;
      - free response that asks to interpret/describe/explain: the answer is
        a sentence, and exact-match grading of a sentence would mark every
        correct student wrong (and a 223%-vs-323% slip in one is invisible)."""
    ctx = pq.ai_context or {}
    q = pq.question
    if q["type"] == "multi_select":
        opts = ctx.get("options")
        if not opts or len(opts) > _MAX_TRUSTED_OPTIONS or any(_CHOICE_INSTRUCTION_RE.search(o or "") for o in opts):
            return False
    elif q["type"] == "free_response":
        if _INTERPRETIVE_RE.search(ctx.get("question") or q.get("text") or ""):
            return False
    return True


def needs_ai_key(pq: ParsedQuestion) -> bool:
    """True for a question whose answer key is missing or only guessed,
    i.e. one an AI-drafted key (see build_key_prompt) is wanted for. A key
    read straight off LaMa's own solution colour (ANSWER_PDF) is never
    second-guessed, and matching_pair is always already graded."""
    return pq.answer_source in (ANSWER_NONE, ANSWER_GUESS) and pq.ai_context is not None and _key_is_gradeable(pq)


KEY_PROMPT_VERSION = 3

_KEY_COUNT_CUES = (
    (re.compile(r"\b(beiden|zwei|two|both)\b", re.IGNORECASE), 2),
    (re.compile(r"\b(drei|three)\b", re.IGNORECASE), 3),
    (re.compile(r"\b(vier|four)\b", re.IGNORECASE), 4),
)
# parse_key_reply's reason for an exercise the model's reply never mentioned
# -- also what EVERY exercise of a batch gets when the reply wasn't usable
# JSON at all, which a caller treats as a failed batch (retry next scan)
# rather than a per-question verdict worth caching.
NO_ENTRY_REASON = "the AI reply had no entry for this exercise"
# A LaMa "Mutiple Choice" exercise is a real table: one row per option, and
# in a SOLVED export the correct rows carry a checked box in a narrow right-
# hand column. None of that is text, and none of it is a vector shape
# pdfminer reports (page.rects/lines/curves are empty on these pages): the
# table borders are 1px-wide EMBEDDED IMAGES and the box only shows when the
# page is rendered. So it's read the way a person reads it -- find the rows
# from the borders, take each row's words, render the checkbox cell and see
# which have ink in them. This also fixes option splitting for these
# exercises: the old gap-based block heuristic could swallow a wrapped
# instruction sentence as "option A", shifting every index by one. (This
# module's docstring used to say the marking was unrecoverable -- that came
# from looking only at the embedded images, not at the rendered page.)
_BOX_INSET_PT = 4.0  # stay clear of the cell's own border lines
_BOX_HALF_HEIGHT_PT = 6.0
_BOX_INK_THRESHOLD = 0.012  # fraction of dark pixels in the cell window that counts as "a mark is there"
_HAIRLINE_MAX_PT = 3.0


@dataclass
class ChoiceTable:
    intro: list[Word]  # words above the table -- the actual question statement
    rows: list[list[Word]]  # one word list per option row, left-to-right content only
    ink: list[float]  # dark-pixel fraction in each row's checkbox cell
    checked: Optional[list[int]] = None  # filled in by read_choice_table when the marks are unambiguous


def read_choice_table(pdf: Any, ex: RawExercise, question_text_for_cue: str = "") -> Optional[ChoiceTable]:
    """The exercise's option table, or None if it doesn't look like exactly
    one table with a narrow checkbox column (then the caller falls back to
    the gap-based splitting). `checked` is only set when the ticks are
    unambiguous: at least one, never all, and consistent with a count the
    question states ("die beiden" = exactly two)."""
    if not ex.words or len({w.page for w in ex.words}) != 1:
        return None
    try:
        page = pdf.pages[ex.words[0].page]
        top, bottom = min(w.top for w in ex.words) - 4, max(w.bottom for w in ex.words) + 4
        hair = [
            im for im in page.images
            if im["top"] < bottom and im["bottom"] > top
        ]
        horiz = [im for im in hair if (im["bottom"] - im["top"]) <= _HAIRLINE_MAX_PT and (im["x1"] - im["x0"]) > 12]
        vert = [im for im in hair if (im["x1"] - im["x0"]) <= _HAIRLINE_MAX_PT and (im["bottom"] - im["top"]) > 12]
        if len(horiz) < 3 or len(vert) < 3:
            return None
        # The table's frame comes from its VERTICAL borders; only horizontal
        # lines that span exactly that frame are row separators (the many
        # short hairlines are fraction bars inside the options, and a page
        # with a second table -- a matching exercise's two grids -- has no
        # single frame that fits, so it's rejected below).
        xs: list[float] = []
        for x in sorted(im["x0"] for im in vert):
            if not xs or x - xs[-1] > 1.5:
                xs.append(x)
        if len(xs) < 3:
            return None
        frame_lo, frame_hi = xs[0], xs[-1]
        lines = [im for im in horiz if abs(im["x0"] - frame_lo) <= 3 and abs(im["x1"] - frame_hi) <= 4]
        ys: list[float] = []
        for y in sorted(im["top"] for im in lines):
            if not ys or y - ys[-1] > 1.5:
                ys.append(y)
        if len(ys) < 3:
            return None
        x_left, x_right = xs[-2], xs[-1]
        if not (10 <= x_right - x_left <= 40):
            return None
        bounds = [(y0, y1) for y0, y1 in zip(ys, ys[1:]) if y1 - y0 >= 14]
        if len(bounds) < 2:
            return None
        intro = [w for w in ex.words if _center(w) < ys[0]]
        rows: list[list[Word]] = []
        mids: list[float] = []
        for y0, y1 in bounds:
            row = [w for w in ex.words if y0 <= _center(w) < y1 and (w.x0 + w.x1) / 2 < x_left]
            if not row:
                return None  # an option row with no text (a pure-image option) -- can't be represented
            rows.append(row)
            mids.append((y0 + y1) / 2)
        # ONE render of the whole checkbox column, sliced per row -- rendering
        # each cell separately costs a pdfium page render apiece, which made a
        # 300-exercise PDF take half an hour to parse.
        res = 216
        px_per_pt = res / 72.0
        y_top = bounds[0][0]
        column = page.crop((x_left + _BOX_INSET_PT, y_top, x_right - _BOX_INSET_PT, bounds[-1][1])).to_image(resolution=res).original.convert("L")
        width, height = column.size
        gray = column.tobytes()
        ink = []
        for mid in mids:
            r0 = max(0, int((mid - _BOX_HALF_HEIGHT_PT - y_top) * px_per_pt))
            r1 = min(height, int((mid + _BOX_HALF_HEIGHT_PT - y_top) * px_per_pt))
            cell = gray[r0 * width:r1 * width]
            ink.append(sum(1 for px in cell if px < 110) / max(1, len(cell)))
    except Exception:  # noqa: BLE001 - any rendering trouble just means "can't read it as a table"
        return None
    table = ChoiceTable(intro=intro, rows=rows, ink=ink)
    checked = [i for i, f in enumerate(ink) if f > _BOX_INK_THRESHOLD]
    # Marks must be clearly present or clearly absent: a cell in the grey
    # zone (a border sliver, a smudge) means this read isn't trustworthy.
    ambiguous = any(_BOX_INK_THRESHOLD * 0.25 < f <= _BOX_INK_THRESHOLD for f in ink)
    expected = next((n for rx, n in _KEY_COUNT_CUES if rx.search(question_text_for_cue)), None)
    if checked and len(checked) < len(rows) and not ambiguous and (expected is None or len(checked) == expected):
        table.checked = checked
    return table


_MAX_AI_ANSWER_CHARS = 200  # mirrors math_teacher.py's MAX_ACCEPTED_ANSWER_LENGTH
_MAX_AI_RATIONALE_CHARS = 300


def build_key_prompt(items: list[dict[str, Any]]) -> str:
    """One prompt for a BATCH of exercises (every item: {"id", "type":
    "multi_select"|"free_response", "text", "options"|None, "context":
    ParsedQuestion.ai_context}). Strict JSON out -- see parse_key_reply,
    which validates every field against the question it belongs to
    rather than trusting the model."""
    payload = []
    for it in items:
        ctx = it["context"]
        entry: dict[str, Any] = {"id": it["id"], "type": it["type"], "question": ctx["question"] or it["text"]}
        if it["type"] == "multi_select":
            entry["options"] = ctx["options"] if ctx.get("options") else it["options"]
        if ctx.get("current_answer"):
            entry["unverified_guess"] = ctx["current_answer"]
        if it.get("image_index") is not None:
            entry["image"] = it["image_index"]
        payload.append(entry)
    return (
        "You are drafting the ANSWER KEY for exercises from a school math worksheet (Austrian LaMa/BIFIE format, "
        "German or English, mostly algebra: powers, roots, term rewriting, some functions/geometry). A teacher will "
        "check your work, but wrong answers waste their time, so correctness matters more than coverage.\n\n"
        "The exercises were extracted from a PDF, so formulas arrive flattened into text with layout hints:\n"
        "  ^{...} = superscript (an exponent); _{...} = subscript; (a/b) = two stacked tokens, i.e. a fraction "
        "(usually inside an exponent); √[n] = an n-th root (the index sat over the radical); {A} over {B} = a fraction "
        "with numerator A and denominator B (the bar itself is not text). Some tokens are still garbled "
        "((cid:NN) are bracket glyphs; a lone digit or letter may belong to a neighbouring term). Diagrams and graphs "
        "are NOT in the text -- an exercise that depends on one cannot be answered unless it has an image.\n"
        "If an exercise has an \"image\" number N, the Nth attached picture (counting from 1, in order) is the "
        "authoritative rendering of that exercise: read the formulas from the picture and treat the text only as a rough hint.\n\n"
        "Per exercise:\n"
        '  - type "multi_select": choose EVERY correct option. "answer" = a JSON list of 0-based indexes into that '
        'exercise\'s "options" array (the array order is the order the options appear on the page). Follow the '
        'instruction in the question ("die beiden" = exactly two). Verify each option by actually simplifying it.\n'
        '  - type "free_response": solve it. "answer" = the final result as short plain text (a number, or a '
        'simplified expression written like x^(3/2)). If "unverified_guess" is present it is only an untrusted '
        "guess of the answer scraped from the PDF -- solve independently.\n"
        '  - If you cannot reconstruct the exercise reliably, it needs a diagram, or it is ambiguous: "answer": null, '
        '"confident": false. Never guess.\n\n'
        "Reply with ONLY one JSON object, no markdown, no commentary, exactly this shape:\n"
        '{"answers": [{"id": "<the exercise id>", "answer": <list of ints | string | null>, "confident": <true|false>, '
        '"rationale": "<one short sentence: why this is the answer>"}]}\n'
        "Include one entry per exercise, same ids.\n\n"
        "Exercises:\n" + json.dumps(payload, ensure_ascii=False)
    )


@dataclass
class KeyOutcome:
    """The validated result for ONE exercise of a key batch. status:
    "ok" (answer is usable -- `correct_indices` for multi_select,
    `answer` for free_response), "unsure" (the model itself declined --
    cacheable, asking again won't change its mind), or "invalid" (the
    model answered but it fails validation against the question --
    `reason` says how)."""
    status: str
    correct_indices: Optional[list[int]] = None
    answer: Optional[str] = None
    rationale: str = ""
    reason: str = ""


def _extract_json_object(reply: str) -> Optional[Any]:
    text = reply.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def parse_key_reply(reply: Optional[str], items: list[dict[str, Any]]) -> dict[str, KeyOutcome]:
    """Validates a build_key_prompt reply against the exact items it was
    asked about. Returns one KeyOutcome per item id -- never raises, never
    omits an id: anything the model skipped, mangled or got out of range
    is an explicit "invalid" with a reason, so a caller can tell "the
    model said it isn't sure" from "the reply was unusable"."""
    outcomes: dict[str, KeyOutcome] = {}
    parsed = _extract_json_object(reply) if reply else None
    entries = parsed.get("answers") if isinstance(parsed, dict) else parsed
    by_id: dict[str, dict] = {}
    if isinstance(entries, list):
        for e in entries:
            if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"] not in by_id:
                by_id[e["id"]] = e

    for it in items:
        e = by_id.get(it["id"])
        if e is None:
            outcomes[it["id"]] = KeyOutcome("invalid", reason=NO_ENTRY_REASON)
            continue
        rationale = e.get("rationale")
        wavering = isinstance(rationale, str) and bool(_SELF_CORRECTION_RE.search(rationale))
        rationale = " ".join(rationale.split())[:_MAX_AI_RATIONALE_CHARS] if isinstance(rationale, str) else ""
        answer = e.get("answer")
        if answer is None or e.get("confident") is False or wavering:
            outcomes[it["id"]] = KeyOutcome("unsure", rationale=rationale, reason="the AI wasn't confident enough to answer")
            continue
        if it["type"] == "multi_select":
            n_options = len(it["options"])
            if not isinstance(answer, list) or not answer or not all(isinstance(i, int) and not isinstance(i, bool) for i in answer):
                outcomes[it["id"]] = KeyOutcome("invalid", reason="multi_select answer must be a non-empty list of option indexes")
                continue
            indices = sorted(set(answer))
            if indices[0] < 0 or indices[-1] >= n_options:
                outcomes[it["id"]] = KeyOutcome("invalid", reason=f"option index out of range (0..{n_options - 1})")
                continue
            if len(indices) == n_options:
                outcomes[it["id"]] = KeyOutcome("invalid", reason="the AI marked every option correct")
                continue
            expected = next((n for rx, n in _KEY_COUNT_CUES if rx.search(it["text"])), None)
            if expected is not None and len(indices) != expected:
                outcomes[it["id"]] = KeyOutcome("invalid", reason=f"the question asks for {expected} options but the AI picked {len(indices)}")
                continue
            outcomes[it["id"]] = KeyOutcome("ok", correct_indices=indices, rationale=rationale)
        else:  # free_response
            if isinstance(answer, bool) or not isinstance(answer, (str, int, float)):
                outcomes[it["id"]] = KeyOutcome("invalid", reason="free_response answer must be a string or number")
                continue
            text = _fix_mojibake(str(answer).strip())
            if not text or len(text) > _MAX_AI_ANSWER_CHARS or "\n" in text:
                outcomes[it["id"]] = KeyOutcome("invalid", reason="free_response answer is empty or too long to be a short final answer")
                continue
            outcomes[it["id"]] = KeyOutcome("ok", answer=text, rationale=rationale)
    return outcomes


# What a catalog entry's answer key looks like to a teacher right now --
# lama_bank.py publishes this as `answer_state`, and it is the ONLY thing
# the UI colors by (never review_reason, which is prose). None of these is
# an error, so none is rendered as one:
STATE_PDF = "pdf"  # key read from LaMa's own solution marking
STATE_VERIFIED = "verified"  # a guessed key the AI independently agreed with
STATE_AI = "ai"  # key drafted by the AI -- graded from the start, but flagged "check it"
STATE_GUESS = "guess"  # unverified guess from the PDF (AI off/unsure/declined) -- NOT graded: shown as "teacher review"
STATE_MISSING = "missing"  # no key; imports ungraded ("waiting for an answer key")

_AI_EXTRACT_NOTE = "Recovered by the AI from the PDF text -- geometry-based parsing failed on this exercise"


def initial_answer_state(pq: ParsedQuestion) -> str:
    """The answer state a freshly parsed question has BEFORE any AI
    keying pass -- and the one it keeps if that pass is unavailable, so
    "AI off" degrades to exactly today's import behavior."""
    if pq.answer_source == ANSWER_PDF:
        return STATE_PDF
    if pq.answer_source == ANSWER_GUESS:
        return STATE_GUESS
    if pq.answer_source == ANSWER_AI_EXTRACT:
        # Whole structure came from the AI fallback: gradable, but a
        # teacher should look -- same marker an AI-drafted key gets.
        pq.question["ai_answer"] = {"rationale": _AI_EXTRACT_NOTE, "model": None}
        return STATE_AI
    return STATE_MISSING


def key_cache_id(pq: ParsedQuestion) -> str:
    """Stable identity of a question's key REQUEST -- same exercise text
    in a different PDF (or the same PDF re-uploaded) hashes the same, so
    a cached AI answer is reused instead of re-paid. Folds in
    KEY_PROMPT_VERSION so a prompt change invalidates every old entry."""
    import hashlib
    q = pq.question
    blob = json.dumps(
        [KEY_PROMPT_VERSION, q["type"], q["text"], q.get("options"), pq.ai_context],
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _normalize_answer(a: str) -> str:
    return re.sub(r"\s+", "", a).replace(",", ".").lower()


def answers_agree(a: str, b: str) -> bool:
    """Whether two free-response answer strings say the same thing --
    numerically when both are plain numbers (1.0 == 1 == "1,0"), else by
    whitespace/case-insensitive text equality. Deliberately conservative:
    a False here only means "treat the AI's answer as a disagreement the
    teacher should look at", never silently overwrites anything."""
    na, nb = _normalize_answer(a), _normalize_answer(b)
    try:
        return float(na) == float(nb)
    except ValueError:
        return na == nb


def apply_key_outcome(pq: ParsedQuestion, outcome: KeyOutcome, model: Optional[str]) -> str:
    """Applies one validated AI outcome to a question IN PLACE and returns
    its resulting answer state. The AI's key becomes the question's real,
    gradable answer key -- so a student answering right after import is
    graded -- plus an `ai_answer` marker (math_teacher.py's "Question
    shape") so the teacher sees "AI answer -- check it" until they accept
    or edit it. A free_response whose guessed key the AI independently
    agrees with is upgraded to STATE_VERIFIED with no marker (two
    independent reads agree; nothing for the teacher to check)."""
    q = pq.question
    if outcome.status != "ok":
        return initial_answer_state(pq)
    if q["type"] == "multi_select":
        q["correct_indices"] = list(outcome.correct_indices or [])
        q["ai_answer"] = {"rationale": outcome.rationale or None, "model": model}
        return STATE_AI
    # free_response
    guess = (pq.ai_context or {}).get("current_answer") if pq.answer_source == ANSWER_GUESS else None
    if guess is not None and outcome.answer is not None and answers_agree(guess, outcome.answer):
        q["accepted_answers"] = [guess]
        return STATE_VERIFIED
    q["accepted_answers"] = [outcome.answer]
    q["ai_answer"] = {"rationale": outcome.rationale or None, "model": model}
    return STATE_AI


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
    render_pdf = pdfplumber.open(pdf_path)  # for reading checkbox marks; closed at the end

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
        text = _question_text(ex, fmt) or title

        if fmt == "Offenes Antwortformat":
            answer, reason, answer_source = _parse_free_response(ex)
            if answer is None and ai_fallback is not None:
                answer, reason = _ai_extract_free_response(ex, ai_fallback)
                answer_source = ANSWER_AI_EXTRACT if answer is not None else ANSWER_NONE
            if answer is None:
                result.skipped.append(SkippedExercise(reason=reason, source=source))
                if not _tick(i):
                    break
                continue
            question = {
                "tier": default_tier, "type": "free_response", "text": text,
                "accepted_answers": [answer], "points": 1,
            }
            context = None
            if answer_source == ANSWER_GUESS:
                # A scraped "last line" is never graded against students: a
                # wrong "incorrect" is worse than no grade. It only becomes the
                # key if the AI independently agrees (apply_key_outcome) or the
                # teacher accepts it; until then the question imports ungraded
                # and the guess rides along as context for the AI.
                question["accepted_answers"] = None
                context = {
                    "question": _layout_text([w for w in ex.words if not w.red]),
                    "options": None,
                    "current_answer": answer,
                    "region": _region([w for w in ex.words if not w.red]),
                }
            result.imported.append(ParsedQuestion(
                question=question, review_reason=reason, source=source, answer_source=answer_source, ai_context=context,
            ))

        elif fmt == "Mutiple Choice":
            table = read_choice_table(render_pdf, ex, _question_text(ex, fmt))
            if table is not None:
                options = [" ".join(_row_text(r) for r in _cluster_rows(row)) for row in table.rows]
                text = " ".join(_row_text(r) for r in _cluster_rows(table.intro)).strip() or text
                reason = ""
                geometry_options = True
            else:
                options, reason = _parse_multi_select(ex)
                geometry_options = options is not None
            if options is None and ai_fallback is not None:
                options, reason = _ai_extract_multi_select(ex, ai_fallback)
            if options is None:
                result.skipped.append(SkippedExercise(reason=reason, source=source))
                if not _tick(i):
                    break
                continue
            checked = table.checked if table is not None else None
            question = {
                "tier": default_tier, "type": "multi_select", "text": text,
                "options": options, "correct_indices": checked, "points": 1,
            }
            if checked is not None:
                # Read straight off the solved export's own checked boxes:
                # as trustworthy as a color-coded solution, no AI involved.
                result.imported.append(ParsedQuestion(question=question, review_reason="", source=source, answer_source=ANSWER_PDF))
            else:
                if table is not None:
                    context = {
                        "question": _layout_text(table.intro),
                        "options": [_layout_text(row) for row in table.rows],
                        "current_answer": None,
                        "region": _region(ex.words),
                    }
                else:
                    context = _multi_select_context(ex, options, geometry_options)
                result.imported.append(ParsedQuestion(
                    question=question, review_reason=reason, source=source, answer_source=ANSWER_NONE, ai_context=context,
                ))

        else:  # Zuordnungsformat
            parsed, reason = _parse_matching_pair(ex)
            answer_source = ANSWER_PDF
            if parsed is None and ai_fallback is not None:
                parsed, reason = _ai_extract_matching_pair(ex, ai_fallback)
                answer_source = ANSWER_AI_EXTRACT
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
                "tier": default_tier, "type": "matching_pair", "text": text,
                "pairs": pairs, "distractors": distractors or None, "points": 1,
            }
            result.imported.append(ParsedQuestion(question=question, review_reason=reason, source=source, answer_source=answer_source))

        if not _tick(i):
            break

    render_pdf.close()
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
