"""Math Teacher — the teacher side of the math/education spin's live
class session: broadcasts question(s), tallies live answers, grades
them, supports adaptive per-student difficulty, a streak/scoreboard
layer, teacher live-editing, a per-class identity so more than one
class can run on the same machine at once without their questions/
answers/rosters bleeding into each other, and now persistence — a
class's identity, roster, and question bank survive the process
exiting. Still the exact same Attribute-relay pattern every other
plugin uses; still deliberately short of the harder pieces
(handwriting, whiteboard, accounts, cross-device networking) that get
built later.

Class identity: on start, generates a short CLASS_CODE (see
generate_class_code) and publishes it bare/unnamespaced as `class_code`
so a joining window can discover and display it — that one attribute
is the only thing NOT scoped by itself. Every other attribute this
plugin reads or writes has the class code baked into its name via
class_attr(name) -> f"{name}:{class_code}", so two classes running
concurrently (two separate processes, two separate codes) never see
each other's questions, answers, or roster. A math_student instance
(or the real student-facing UI) has to know the same code to talk to
this specific class at all.

Persistence: a class's identity, roster, and question bank now survive
the process exiting, under %APPDATA%\\Sotrice\\classes\\<class_code>\\ (or
SOTRICE_CLASSES_DIR, if set — the same env-override idiom
sotrice-server's own SOTRICE_PLUGIN_PORT/SOTRICE_WS_PORT already use,
purely so a test run doesn't write into a real machine's actual class
data; defaults are unchanged for everyone who never sets it). A freshly
generated class_code stays PURELY IN MEMORY, same as before, until
ensure_persisted() sees the first real sign of use — a student actually
joining, or a teacher actually sending a teacher_command / adaptive_mode
/ student_tier / scoreboard_enabled write — at which point (and only
then) it's written to disk for the first time, and that code becomes
this class's permanent identity, never reminted on a later relaunch.
This is deliberate: an unopened or never-touched launch (someone starts
math-teacher and immediately closes it) must NOT permanently mint a
class nobody asked for, or the set of "known classes" would grow
without bound from launches that were never real classes at all.

Four files per persisted class:
  meta.json — {"class_name", "created_at"}.
  roster.json — [{"student_id", "name", "joined_at"}, ...], appended to
    (never rewritten wholesale) the first time each student entity is
    ever admitted via ensure_student — the same place this file already
    tracks "who's a known student" for the live tally, not a new
    detection mechanism. "name" is best-effort, filled from a bare
    (unnamespaced) "student_name" this file already watches for exactly
    this purpose (see on_student_name) — a real student's own name is
    published well before its first answer, so this is normally
    accurate, but falls back to a placeholder if it somehow isn't known
    yet rather than blocking the join from being recorded at all.
  questions.json — {"questions": [question, ...]} — this class's OWN
    copy of the question bank, a FLAT list (not grouped by tier the way
    the in-memory rotation reads it — see "Question shape" below for
    why), seeded once from default_question_bank() at first persistence
    (see persist_new_class) and read from disk from then on (see
    class_question_bank below). An OLDER on-disk shape (a bare
    {tier: [(text, options, correct_index), ...]} dict, from before
    question types/ids/chapters existed) is transparently migrated on
    load — see migrate_question_bank() — never just broken.
  materials.json — {"chapters": [chapter, ...], "active_chapter": id |
    None}. See "Chapters" below.

Question shape: {"id" (str, stable, assigned once), "tier" ("easy" |
"medium" | "hard"), "type" ("multiple_choice" | "free_response" |
"matching_pair" | "multi_select"), "text", "hint" (str | None, optional
canned clue — never AI-generated), "points" (int, 1..MAX_POINTS, default
1 — see "Scoring" below), "options" (list[str], multiple_choice AND
multi_select), "correct_index" (int | None, multiple_choice only — None
means ungraded, same meaning a teacher "replace" without correct_index
already had), "accepted_answers" (list[str], free_response only — see
grade_answer()/normalize_answer() for how a submission is checked
against these; plain string/numeric equivalence, never an AI/model
call), "correct_indices" (list[int] | None, multi_select only — None
means ungraded, same "no way to guess a teacher-supplied right answer"
posture as correct_index/accepted_answers), "pairs" (list[{"left",
"right"}], matching_pair only, MIN_PAIRS..MAX_PAIRS long — matching_pair
is always gradable, since the pairs themselves ARE the correct answer;
there's no "ungraded" state for it the way the other three types have),
"distractors" (list[str] | None, matching_pair only — extra right-side
texts that are shuffled in alongside the real pairs' own right-side
texts but are never a correct match for anything; a real pedagogical
feature, not clutter — a matching question with only the correct
options ever shown is trivially easy, since a student can just fill in
whatever's left over without knowing anything; distractors are what
make it actually test whether a student can tell a plausible wrong
answer from the right one, same reason multiple_choice's own wrong
options exist. Capped at MAX_DISTRACTORS, empty/None means no
distractors, same "missing means the plain, unextended case" posture as
everything else optional in this shape). Exactly the fields its own
type uses are ever meaningful; every OTHER type-specific field is
simply None on that question, never omitted (a consumer can always do
question["correct_indices"] without a KeyError, even on a
multiple_choice question). Kept as a flat list with a "tier"
field per question (not the old dict-of-tier-lists) specifically so a
Chapter (below) can reference questions across tiers by id, and so
next_question()'s per-tier rotation is just one filter over one list
rather than a second, parallel structure to keep in sync.

Scoring: every question is worth its own "points" (default 1, so an
older question or an existing multiple_choice/free_response bank is
completely unaffected — see grade_answer()). multiple_choice,
free_response, and matching_pair are always all-or-nothing: a correct
answer earns the question's full points, anything else earns 0,
regardless of how many points the question is worth. multi_select is
the one type where points changes the GRADING RULE, not just the
weight, per Laszlo's own explicit spec: a 1-point multi_select is
all-or-nothing (the submitted selection must exactly equal
correct_indices — matches how LaMa itself grades a 1-point multi-select
import), but a multi_select worth MORE than 1 point instead earns
partial credit proportional to how many of the CORRECT options the
student actually selected — points * (|selected ∩ correct_indices| /
|correct_indices|) — with no penalty for also selecting wrong ones.
That last part is a deliberate, known simplification (a student who
selects every option always gets full credit for whatever's correct
among them) rather than an oversight — nothing about penalizing extra
wrong picks was asked for, and adding it silently would be inventing
product behavior nobody decided on; a future revision can add a penalty
term if a teacher actually wants one. Only a fully-exact selection (for
either points tier) counts as "correct" for streak-counting purposes —
partial credit still resets the streak, same as any other wrong-ish
answer. student_score's "correct"/"total" (see "Entities published"
below) are POINT sums under this scheme, not question counts — for
every pre-existing question type at its default points=1 this is
numerically identical to the old count-based behavior, so nothing that
already reads student_score needs to change to keep working.

Chapters: {"id", "name", "question_ids": [id, ...]} — an authoring-side
ORGANIZING view over the same question bank (which question ids, in
what order, a teacher considers "Chapter 3"). A question can belong to
zero, one, or several chapters — deleting a question removes it from
every chapter's question_ids too (see on_authoring_command's
"delete_question"); deleting a chapter never touches the questions
themselves.

A class-wide `active_chapter` (an id, or None for "the whole bank" — the
original and still-default behavior) additionally narrows what
next_question() actually draws from: when set, questions_for_tier()
filters each tier's pool down to that chapter's own question_ids first,
still choosing by tier within that filtered set exactly as before —
adaptive difficulty and chapter selection are independent axes, not one
replacing the other. Falls back to the WHOLE tier if the chapter has
nothing at all in it for a given tier, so switching to a chapter that's
thin in one tier never leaves next_question() with nothing to draw from
(see questions_for_tier's own doc comment). Set via on_authoring_command's
"set_active_chapter"; deleting the active chapter itself resets this
back to None automatically, same reasoning.

Authoring: a teacher's own dashboard (see MathClassView.tsx's
CourseMaterialPanel) manages the bank/chapters live through a
DEDICATED, teacher-only channel — deliberately NOT the same
`teacher_command` control used for "next"/"replace"/"extend" below.
`authoring_question_bank`/`authoring_chapters`/`authoring_active_chapter`
(class_attr()-scoped) publish the FULL bank/chapters/current active-
chapter selection, correct_index and accepted_answers included;
`authoring_command` (class_attr()-scoped) is where a teacher's UI sends
add/edit/delete/reorder/import mutations (see on_authoring_command). All
four names deliberately start with "authoring_" — server/src/
main.rs's PermissionGate refuses ANY class-scoped attribute with that prefix
for a non-loopback (remote) connection, regardless of class-code
authorization, so a joined STUDENT's connection never sees a correct
answer or an accepted-answers list, and can never smuggle a bank edit
through even though it already knows the class code. This is a Core-
level (Rust) rule keyed purely on the attribute NAME, not this plugin
trusting anyone — matching the "the Core is the mail guy, plugins
negotiate meaning" design; this file still validates every authoring
command's shape/bounds itself, the same posture as everything else here
(see the bottom SECURITY NOTE).

A top-level index.json (a list of {"code", "name"}) tracks every class
ever persisted, appended to by persist_new_class and published bare as
known_classes (see publish_known_classes) so a frontend picker can list
every class that exists, running or not, without a new IPC mechanism —
just the same attribute-publish idiom class_code/class_name already use.
A `delete_class` command (bare/unnamespaced, entity-targeted, see
on_delete_class) is the other side of that: removes the index entry and
the class's own directory, so a day of test classes doesn't pile up in
the picker forever with no way to clear it.

Resuming: a `load_class` command (bare/unnamespaced, entity-targeted at
this instance's own control entity — see on_load_class) reads a
previously-persisted class back off disk and makes THIS instance take
over its identity: class_code/class_name are overwritten (republished),
its questions.json becomes this run's class_question_bank, and every
class_attr()-scoped subscription is re-registered under the RESUMED
code (subscribe_class_scoped()) — sotrice_client's World has no
unsubscribe, so the discarded temporary code's subscriptions are simply
left registered and dead (nothing will ever publish under a code that
was never shown to anyone), which costs nothing real. The live
in-memory roster/scores/tiers for the new session start EMPTY (a resume
is a fresh session with the old identity and question bank, not a
replay of exactly who was online last time) — roster.json's history is
untouched and simply gains new entries as students reconnect and answer
again.

Backward compatible IN SPIRIT only, not by exact attribute name: with
adaptive_mode == "off" and no teacher_command ever sent, the shape of
everything published is unchanged from the original single-question
first slice (one shared "medium" question, one shared tally,
question_source always "builtin") — but every consumer now has to read
these under their class_attr(name) form, not the bare name.

Entities published:
  A single control entity, kind="math_class", class_code (str, bare/
  unnamespaced), class_name (str, bare/unnamespaced), known_classes
  (list[{"code", "name"}], bare/unnamespaced — see the Persistence
  section above):
    All namespaced via class_attr() below:
    question_text, question_options (list[str], [] except for
      multiple_choice/multi_select — multi_select's own options list,
      same as multiple_choice's, minus which ones are correct),
      question_index (int, bumps every new round), question_source
      ("builtin" | "teacher_edited"), question_type ("multiple_choice" |
      "free_response" | "matching_pair" | "multi_select"), question_hint
      (str | None), question_points (int) — all mirror whichever tier is
      DEFAULT_TIER ("medium"), or the sole active tier if medium isn't
      active for some reason. Original shape, unchanged meaning for the
      pre-existing fields; a matching_pair round has no meaningful flat
      "options" mirror at all (its left/right items only ever appear per-
      tier — see question_by_tier below), same "not every type uses
      every field" posture the module docstring's "Question shape"
      section already has.
    answer_tally (list[int]) — mirrors the DEFAULT_TIER tally. For a
      free_response or matching_pair question this is [correct_count,
      incorrect_count] (matching_pair's "correct" meaning an exact,
      fully-matched submission — see grade_answer) instead of one entry
      per option; multi_select is one count per option of how many
      students included it in their own selection (not mutually
      exclusive the way multiple_choice's is, since a student can pick
      more than one) — same list[int] SHAPE in every case (never a type
      a consumer has to branch on to just read length/sum), interpreted
      differently by a UI that already knows the question's type from
      question_by_tier/question_type.
    question_by_tier ({tier: {"text", "type", "hint", "points", "options"
      (multiple_choice/multi_select only, else None), "left_items"/
      "right_items" (matching_pair only, else None — "right_items" is
      LONGER than "left_items" whenever the question has distractors,
      and is ALREADY SHUFFLED, see next_question()/replace_current_
      question(), so its order itself carries no information about the
      correct pairing OR about which entries are real pairs vs.
      distractors — a student's own UI has no way to tell them apart
      except by trying)}}) — one entry per currently active tier (just ["medium"]
      when adaptive_mode is "off", all three otherwise). Correct-answer
      index/accepted-answers/correct-indices/the actual left-right
      pairing are never published here or anywhere else a STUDENT can
      read — see the "Authoring" section above for where the real
      (answer-bearing) copy lives and how it stays teacher-only.
    answer_tally_by_tier ({tier: list[int]}) — same per-tier
      interpretation as answer_tally above.
    adaptive_mode ("off" | "automatic" | "manual", default "off") —
      teacher-configurable. "automatic": this plugin bumps a student's
      tier up/down itself from a rolling correctness window. "manual":
      tiers are broadcast the same way, but only ever change via an
      external student_tier write (a future teacher UI) — this plugin
      never bumps them itself in that mode, so automatic and manual
      never fight over the same entry.
    student_tier ({str(student_id): tier}) — {} while adaptive_mode is
      "off", so a student reading it and finding nothing behaves
      exactly like it doesn't exist.
    student_score ({str(student_id): {"correct": int, "total": int}}) —
      see the module docstring's "Scoring" section: these are POINT
      sums, not question counts (a multi_select worth partial credit can
      contribute a non-integer amount to "correct"), but every question
      at the default points=1 makes this numerically identical to a
      plain count, same as it always was before points existed.
    student_streak ({str(student_id): int}) — running grading state
      per student, independent of adaptive_mode (grading happens
      whenever we know a question's correct answer, tiering or not).
      Streak counts consecutive FULLY-correct answers (full points
      earned, not merely partial credit), reset to 0 on anything less.
      Neither ever reveals what the correct answer WAS.
    scoreboard_enabled (bool, default False) — teacher-facing toggle
      (see MathClassView.tsx's StatisticsPanel). scoreboard
      ([{"student", "correct", "total", "streak"}, ...], sorted by
      correct descending) — only kept up to date while enabled;
      actively cleared to [] the instant it's turned off, never
      populated while off.
    leave_locked (bool, default False) — teacher-facing toggle (same
      StatisticsPanel) for whether a joined student can leave class
      mode on their own. This plugin never enforces anything about it
      itself (see this file's closing SECURITY NOTE on the actual trust
      boundary) — it purely carries the flag a student's own
      KioskShell.tsx reads to decide whether its own "Leave Class"
      button is clickable at all: classroom management, not a security
      mechanism. Reset to False on every on_load_class resume, same as
      scoreboard_enabled.
    student_names ({str(student_id): str}) — best-effort display name
      per tracked student, from the same bare student_name pushes
      roster.json already watches for (see student_name_by_id below) —
      lets the dashboard's roster/groups views show a real name instead
      of a bare "Student <id>" placeholder. Only ever has an entry for a
      student this class's own known_students already contains.
    authoring_question_bank (list[question], see "Question shape"
      above — correct_index/accepted_answers INCLUDED) and
      authoring_chapters (list[chapter], see "Chapters" above) — the
      teacher-only authoring surface; kept current on every bank/
      chapter mutation (see publish_authoring_state). Loopback-only via
      the "authoring_" prefix rule (see "Authoring" above) — never
      readable from a joined student's connection no matter what.

Reads (subscribing to a class_attr()-scoped name, not a specific entity —
any number of math_student instances for THIS class can exist):
    answer — {"question_index": int, "tier": str (optional), ...}, one of
    four shapes depending on the CURRENT tier's actual question type
    (never trusted from the payload itself — a mismatched key for the
    active type, e.g. "option" against a free_response question, is
    dropped like any other malformed shape):
      multiple_choice — "option": int (an index into that tier's options).
      free_response — "text": str.
      multi_select — "selected": list[int] (indices into that tier's
        options, deduplicated; must be non-empty — an "I selected
        nothing" answer isn't submitted at all, same as every other type
        requires SOME answer to be gradeable).
      matching_pair — "pairs": {str(left_index): right_index}, where
        right_index indexes into that tier's OWN SHUFFLED right_items
        (see question_by_tier above, LONGER than left_items whenever the
        question has distractors) — i.e. "I think left item 2 matches
        whatever's currently shown at right_items[right_index]". Must
        name every left index exactly once, with distinct right_index
        values (duplicates make no sense — two left items can't both
        correctly point at the same right slot) — a partial or
        duplicate-valued mapping is dropped rather than partially
        graded, since matching_pair itself is always all-or-nothing (see
        the module docstring's "Scoring" section). A right_index that
        lands on a distractor slot is a completely valid SUBMISSION
        (nothing stops a student guessing one), just never a CORRECT
        one.
    Missing/unknown/inactive "tier" is treated as DEFAULT_TIER, which is
    exactly right for a student that predates tiering, or whenever
    adaptive_mode is "off" (the only active tier then anyway). Only
    counted if question_index matches the current round AND tier is
    currently active; each student is graded at most once per round no
    matter how many times they resubmit.

    adaptive_mode, student_tier, scoreboard_enabled — externally
    settable; a teacher (or future teacher UI) writing these directly
    is adopted immediately. student_tier writes only ever touch a
    student this plugin already knows about, and only to a real tier.

    teacher_command — {"cmd": "next" | "replace" | "extend", ...}: the
    LIVE-EDIT-what's-on-screen-right-now channel, deliberately separate
    from authoring_command below (which edits the class's own saved
    question BANK, not just this round's display) — see "Authoring"
    above for why they're kept apart, and note this one is NOT
    loopback-restricted (a pre-existing gap: knowing the class code is
    still all it takes to send these, same trust level the bottom
    SECURITY NOTE already documents — untouched by this stage).
      "next" — advance to the next round immediately, timer resets.
      "replace" — {"cmd": "replace", "text": str, "tier": str
        (optional, required when more than one tier is active), "type"
        ("multiple_choice" | "free_response" | "matching_pair" |
        "multi_select", default "multiple_choice" — omitting it
        reproduces the exact pre-existing behavior), "hint": str
        (optional), "points": int (optional, 1..MAX_POINTS, default 1 —
        see the module docstring's "Scoring" section), and then whichever
        of "options" + "correct_index" (multiple_choice, both optional),
        "accepted_answers" (free_response, optional), "options" +
        "correct_indices" (multi_select, both optional), or "pairs" +
        "distractors" (matching_pair — "pairs" REQUIRED, matching_pair
        has no ungraded state, see "Question shape"; "distractors"
        optional) the chosen "type" actually uses — see
        validate_question_content() for the exact per-type shape/bounds,
        shared verbatim with authoring_command's add/edit_question below.
        Edits the CURRENT round's question for one tier in place
        (question_index does not change) and clears that tier's tally.
        Omitting "correct_index"/"accepted_answers"/"correct_indices"
        makes that tier's current question ungraded for this round
        (tallied normally, but no correct/streak/score impact) — there
        is no way to guess a teacher-supplied question's right answer,
        so this plugin doesn't pretend to.
      "extend" — {"cmd": "extend", "seconds": number} pushes out the
        round's auto-advance deadline by `seconds`, clamped to
        EXTEND_SECONDS_MIN..MAX.
    Anything that doesn't match one of these shapes, or names a tier
    that isn't currently active, is silently dropped.

    authoring_command — {"cmd": ..., ...}, the question-bank/chapter
    editor's own channel (see "Authoring" above; loopback-only, unlike
    teacher_command). See on_authoring_command's own docstring for the
    full per-cmd shape (add/edit/delete/reorder_questions, add/rename/
    delete/reorder_chapters, set_chapter_questions) — every one shape-
    and bound-checked the same way "replace" already is above, and
    every mutation immediately re-persists questions.json/materials.json
    and republishes authoring_question_bank/authoring_chapters (and,
    for a question that happens to be the one currently on screen,
    nothing — authoring edits the BANK, taking effect from the next
    time that question comes up in rotation; hot-patching what's
    already on screen is what teacher_command's own "replace" is for).

Also reads, bare/unnamespaced (not class_attr()-scoped — same tier as
class_code/class_name/kind themselves):
    student_name — any math_student's own display name (see
      math_student.py), cached best-effort against a future roster
      entry (see the Persistence section above); never required — a
      student who hasn't published one yet just gets a placeholder.
    load_class — see the Persistence section above (on_load_class).
      Entity-targeted at THIS instance's own control entity; a
      load_class write aimed at a different control entity (some other
      running instance) is ignored here, same as teacher_command.
"""

import json
import math
import os
import random
import shutil
import time
import uuid
from pathlib import Path

from sotrice_client import World

# Excludes 0/O/1/I/L -- a code a human has to read off a screen (or a
# projector) and type back in shouldn't depend on telling those apart
# in whatever font it's rendered in.
CLASS_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CLASS_CODE_LENGTH = 6


def generate_class_code() -> str:
    return "".join(random.choice(CLASS_CODE_ALPHABET) for _ in range(CLASS_CODE_LENGTH))


QUESTION_TYPES = ("multiple_choice", "free_response", "matching_pair", "multi_select")


def new_question_id() -> str:
    # Short, random, and — unlike an incrementing counter — needs no
    # persisted "next id" state to stay unique: collisions are
    # astronomically unlikely at the scale one class's bank ever
    # reaches. Chapters likewise (see new_chapter_id).
    return "q" + uuid.uuid4().hex[:8]


def new_chapter_id() -> str:
    return "c" + uuid.uuid4().hex[:8]


# The literal seed content, organized by tier for readability (this is
# what a human reads/edits in source) — default_question_bank() below
# flattens it into the shape everything else in this file actually
# operates on (see the module docstring's "Question shape"). Every seed
# question is multiple_choice; free_response only ever comes from a
# teacher authoring one.
_DEFAULT_QUESTIONS_BY_TIER: dict[str, list[tuple[str, list[str], int]]] = {
    "easy": [
        ("What is 2 + 3?", ["4", "5", "6", "7"], 1),
        ("What is 10 - 4?", ["5", "6", "7", "8"], 1),
        ("What is 3 x 3?", ["6", "9", "12", "15"], 1),
        ("What is 8 / 2?", ["2", "3", "4", "5"], 2),
        ("What is 1 + 1?", ["1", "2", "3", "4"], 1),
    ],
    "medium": [
        ("What is 7 + 5?", ["10", "11", "12", "13"], 2),
        ("What is 9 x 6?", ["45", "54", "56", "63"], 1),
        ("Solve for x: 2x = 14", ["5", "6", "7", "8"], 2),
        ("What is 15% of 200?", ["20", "25", "30", "35"], 2),
        ("What is the square root of 81?", ["7", "8", "9", "10"], 2),
    ],
    "hard": [
        ("What is 12 x 13?", ["144", "156", "168", "132"], 1),
        ("Solve for x: 3x - 7 = 20", ["7", "8", "9", "10"], 2),
        ("What is 20% of 350?", ["60", "65", "70", "75"], 2),
        ("What is the square root of 144?", ["10", "11", "12", "13"], 2),
        ("Simplify: (2^3) x (2^2)", ["16", "32", "64", "128"], 1),
    ],
}


def default_question_bank() -> list[dict]:
    """A fresh flat bank built from _DEFAULT_QUESTIONS_BY_TIER, one new
    random id per question — called once per genuinely NEW class (see
    persist_new_class's call site), never shared/mutated in place across
    classes, so two classes' banks are always independent objects even
    though they start with identical content."""
    return [
        {
            "id": new_question_id(),
            "tier": tier,
            "type": "multiple_choice",
            "text": text,
            "hint": None,
            "points": DEFAULT_POINTS,
            "options": list(options),
            "correct_index": correct_index,
            "accepted_answers": None,
            "correct_indices": None,
            "pairs": None,
        }
        for tier, seeds in _DEFAULT_QUESTIONS_BY_TIER.items()
        for text, options, correct_index in seeds
    ]


def _distractors_for(correct: int, count: int = 3) -> list[int]:
    """Small random perturbations around the correct integer answer,
    deduplicated and never equal to `correct` itself — good enough as
    plausible wrong answers for a templated arithmetic question without
    needing to understand the specific operation that produced them."""
    candidates: set[int] = set()
    attempts = 0
    while len(candidates) < count and attempts < 50:
        attempts += 1
        delta = random.choice([-10, -5, -3, -2, -1, 1, 2, 3, 5, 10])
        candidate = correct + delta
        if candidate != correct and candidate >= 0:
            candidates.add(candidate)
    while len(candidates) < count:
        # Practically unreachable (50 random attempts failing to find 3
        # distinct non-negative neighbors), but a deterministic
        # fallback beats an infinite loop on a hostile/degenerate input.
        candidates.add(correct + len(candidates) + 1)
    return list(candidates)[:count]


def _mc_from_answer(tier: str, text: str, correct: int) -> dict:
    options = _distractors_for(correct) + [correct]
    random.shuffle(options)
    return {
        "id": new_question_id(),
        "tier": tier,
        "type": "multiple_choice",
        "text": text,
        "hint": None,
        "points": DEFAULT_POINTS,
        "options": [str(o) for o in options],
        "correct_index": options.index(correct),
        "accepted_answers": None,
        "correct_indices": None,
        "pairs": None,
    }


def _template_easy() -> tuple[str, int]:
    kind = random.choice(["add", "sub", "mul"])
    if kind == "add":
        a, b = random.randint(1, 12), random.randint(1, 12)
        return f"What is {a} + {b}?", a + b
    if kind == "sub":
        a = random.randint(5, 20)
        b = random.randint(1, a)
        return f"What is {a} - {b}?", a - b
    a, b = random.randint(1, 5), random.randint(1, 5)
    return f"What is {a} x {b}?", a * b


def _template_medium() -> tuple[str, int]:
    kind = random.choice(["mul", "percent", "linear"])
    if kind == "mul":
        a, b = random.randint(2, 12), random.randint(2, 12)
        return f"What is {a} x {b}?", a * b
    if kind == "percent":
        # base a multiple of 20 and pct a multiple of 5 guarantees an
        # exact (never rounded/fractional) integer answer.
        pct = random.choice([10, 20, 25, 50])
        base = random.choice([20, 40, 60, 80, 100, 120, 200])
        return f"What is {pct}% of {base}?", pct * base // 100
    a = random.randint(2, 9)
    x = random.randint(2, 12)
    return f"Solve for x: {a}x = {a * x}", x


def _template_hard() -> tuple[str, int]:
    kind = random.choice(["mul", "percent", "square", "linear"])
    if kind == "mul":
        a, b = random.randint(11, 20), random.randint(2, 20)
        return f"What is {a} x {b}?", a * b
    if kind == "percent":
        pct = random.choice([15, 35, 45, 55, 65, 85])
        base = random.choice([60, 80, 100, 140, 180, 220, 260, 300])
        return f"What is {pct}% of {base}?", pct * base // 100
    if kind == "square":
        n = random.randint(4, 15)
        return f"What is the square root of {n * n}?", n
    a = random.randint(2, 9)
    b = random.randint(1, 20)
    x = random.randint(2, 15)
    return f"Solve for x: {a}x - {b} = {a * x - b}", x


_TEMPLATES_BY_TIER = {"easy": _template_easy, "medium": _template_medium, "hard": _template_hard}
# Generous enough for a teacher who genuinely wants a big batch, tight
# enough that one authoring_command can't be used to flood the bank up
# to MAX_QUESTIONS_PER_CLASS in a single message.
MAX_GENERATE_PER_REQUEST = 20


def generate_templated_question(tier: str) -> dict:
    """Simple randomized arithmetic/algebra from a fixed pattern per
    tier — NOT real AI generation (no API calls, no cost, nothing that
    "understands" math beyond plugging random numbers into a template),
    same spirit as _DEFAULT_QUESTIONS_BY_TIER's own hardcoded seed
    questions, just parameterized so repeated use doesn't hand back the
    same 5 questions every time. Real AI-assisted generation is its own
    later chapter, deliberately not this."""
    text, correct = _TEMPLATES_BY_TIER[tier]()
    return _mc_from_answer(tier, text, correct)


# Order matters: index into this list is how "one tier up"/"one tier
# down" is computed. "medium" is both the default starting tier and
# the one mirrored into the original single-question attributes.
TIERS = ["easy", "medium", "hard"]
DEFAULT_TIER = "medium"
ADAPTIVE_MODES = ("off", "automatic", "manual")

SECONDS_PER_QUESTION = 20.0
TICK_SECONDS = 0.5

# How many of a student's most recent GRADED answers feed the up/down
# tier bump decision in "automatic" mode. Requires the window to be
# entirely one way (all correct, or all incorrect) before moving a
# tier, then clears — the hysteresis is what stops a student
# oscillating between two tiers after every single answer near a
# boundary. Deliberately asymmetric per Laszlo's own call: a student
# should get the benefit of the doubt going up (3-in-a-row correct)
# but get moved down to an easier tier faster (2-in-a-row wrong) so
# they're not stuck on material that's too hard for longer than they
# need to be.
TIER_UP_WINDOW = 3
TIER_DOWN_WINDOW = 2

# Bounds for validating a "replace" teacher_command's (or an
# authoring_command's — see on_authoring_command) free-text fields —
# generous enough for any real question/option a teacher would type,
# tight enough that a hostile payload can't make this plugin publish
# or hold in memory something absurd.
MAX_TEXT_LENGTH = 500
MAX_OPTIONS = 10
MAX_OPTION_LENGTH = 200
# Reuses MAX_OPTIONS/MAX_OPTION_LENGTH's own bounds — an accepted-
# answers list is the free_response equivalent of an options list,
# same realistic size a teacher would ever type.
MAX_ACCEPTED_ANSWERS = MAX_OPTIONS
MAX_ACCEPTED_ANSWER_LENGTH = MAX_OPTION_LENGTH
MAX_HINT_LENGTH = 300
# matching_pair's own bounds — reuses MAX_OPTION_LENGTH for each side's
# text (same realistic size as any other short answer/label in this
# file). MIN_PAIRS = 2 because a "matching" question with fewer than two
# pairs isn't actually a matching question. MAX_DISTRACTORS is deliberately
# smaller than MAX_PAIRS — a handful of plausible wrong options is what
# makes a matching question harder to guess through (see the module
# docstring's "Question shape" section), not a wall of them.
MIN_PAIRS = 2
MAX_PAIRS = 8
MAX_DISTRACTORS = 4
# A question's own weight (see the module docstring's "Scoring"
# section). Generous enough for a teacher who wants a big worksheet
# question to count for more, tight enough that one question can't
# single-handedly dominate a whole scoreboard.
MAX_POINTS = 20
DEFAULT_POINTS = 1
# How many questions/chapters one class's authoring surface can hold —
# generous for any real class's own material, tight enough that a
# flood of add_question/add_chapter commands can't grow either list
# without bound (same "cap the thing a hostile or buggy loop could
# grow forever" posture as MAX_TRACKED_STUDENTS below).
MAX_QUESTIONS_PER_CLASS = 500
MAX_CHAPTERS_PER_CLASS = 100
MAX_CHAPTER_NAME_LENGTH = 100
# A bulk import (see on_authoring_command's "import_questions") is
# expected to carry many more items in one message than a teacher ever
# would by hand through generate_questions — generous enough for a real
# worksheet's worth of imported content (e.g. a LaMa PDF's questions),
# still bounded so one command can't be used to flood the bank on its
# own regardless of MAX_QUESTIONS_PER_CLASS's own remaining room.
MAX_IMPORT_PER_REQUEST = 200

# Bounds for "extend" — keeps a malformed/hostile `seconds` from either
# freezing the session (absurdly large) or expiring it effectively
# instantly (zero/negative).
EXTEND_SECONDS_MIN = 5.0
EXTEND_SECONDS_MAX = 300.0

# Bounds how much a flood of bogus/malicious writes can grow the
# per-student dicts below by — without it, anything that can reach the
# server (see this file's bottom note on the real trust boundary)
# could create unlimited entities and grow these without limit. A real
# class is dozens of students, not hundreds, so this costs nothing for
# legitimate use. Every dict below keyed by student entity id is only
# ever populated through ensure_student, which is the single place
# this cap is enforced. student_name_by_id (bare/global, not per-class)
# reuses the same cap for the same reason.
MAX_TRACKED_STUDENTS = 200


# --- Persistence: plain stdlib json/pathlib, no new dependencies, ---
# --- matching this repo's existing file-I/O style (see spy_loader.py). ---

# Env-overridable the same way sotrice-server's own SOTRICE_PLUGIN_PORT/
# SOTRICE_WS_PORT already are (see server/src/main.rs): "a second,
# throwaway instance (testing a change live) can run alongside" a real
# machine's real class data "without fighting it" — defaults are
# unchanged for everyone who never sets this.
CLASSES_DIR = (
    Path(os.environ["SOTRICE_CLASSES_DIR"])
    if os.environ.get("SOTRICE_CLASSES_DIR")
    else Path(os.environ["APPDATA"]) / "Sotrice" / "classes"
)
INDEX_PATH = CLASSES_DIR / "index.json"


def _read_json(path: Path, default):
    """Best-effort JSON read, same plain open()/json.load() style this
    repo already uses (see spy_loader.py's load_scene) — missing or
    corrupt is treated the same as "nothing here yet", never a crash."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_index() -> list[dict]:
    return _read_json(INDEX_PATH, [])


def append_to_index(code: str, class_display_name: str) -> None:
    """Read-modify-write, no file locking — fine at the realistic scale
    here (a handful of concurrent local teacher processes on one
    machine), not a general-purpose concurrent-writers solution."""
    index = load_index()
    if not any(entry.get("code") == code for entry in index):
        index.append({"code": code, "name": class_display_name})
        _write_json(INDEX_PATH, index)


def remove_from_index(code: str) -> None:
    """The other half of append_to_index — lets a teacher clear out an
    old/unused test class from the picker (see on_delete_class) instead
    of index.json only ever growing for the life of the install."""
    index = load_index()
    filtered = [entry for entry in index if entry.get("code") != code]
    if len(filtered) != len(index):
        _write_json(INDEX_PATH, filtered)


def class_dir_for(code: str) -> Path:
    return CLASSES_DIR / code


def persist_new_class(code: str, class_display_name: str, question_bank: list[dict]) -> None:
    """First-ever persistence of a freshly-generated class code — see
    ensure_persisted()'s call sites for exactly when this fires (the
    first real sign of use, not merely "the process started"), which is
    what keeps an unused/never-touched launch from permanently minting
    a class nobody asked for."""
    class_dir = class_dir_for(code)
    _write_json(class_dir / "meta.json", {"class_name": class_display_name, "created_at": time.time()})
    _write_json(class_dir / "questions.json", {"questions": question_bank})
    _write_json(class_dir / "roster.json", [])
    _write_json(class_dir / "materials.json", {"chapters": [], "active_chapter": None})
    append_to_index(code, class_display_name)


def save_questions(code: str, question_bank: list[dict]) -> None:
    """Rewrites questions.json wholesale — called after every authoring
    mutation (see on_authoring_command); cheap enough at this scale (at
    most MAX_QUESTIONS_PER_CLASS entries) that a real read-modify-write
    diff isn't worth the complexity."""
    _write_json(class_dir_for(code) / "questions.json", {"questions": question_bank})


def save_chapters(code: str, chapters: list[dict], active_chapter: str | None) -> None:
    """Rewrites materials.json wholesale, same posture as save_questions
    — `active_chapter` (see questions_for_tier/on_authoring_command's
    "set_active_chapter") lives in this same file rather than its own,
    since it's meaningless without the chapters list it names an id
    from; every caller passes its own current value, not a stale one,
    so this is never at risk of persisting an active_chapter reference
    to a chapter that isn't ACTUALLY in `chapters` in this same write."""
    _write_json(class_dir_for(code) / "materials.json", {"chapters": chapters, "active_chapter": active_chapter})


def append_roster_entry(code: str, student_id: int, student_display_name: str) -> None:
    roster_path = class_dir_for(code) / "roster.json"
    roster = _read_json(roster_path, [])
    if not isinstance(roster, list):
        roster = []
    roster.append({"student_id": student_id, "name": student_display_name, "joined_at": time.time()})
    _write_json(roster_path, roster)


def _valid_question(q) -> bool:
    if not isinstance(q, dict):
        return False
    if not (isinstance(q.get("id"), str) and q["id"]):
        return False
    if q.get("tier") not in TIERS or q.get("type") not in QUESTION_TYPES:
        return False
    if not isinstance(q.get("text"), str) or not q["text"]:
        return False
    points = q.get("points", DEFAULT_POINTS)
    if isinstance(points, bool) or not isinstance(points, int) or not (1 <= points <= MAX_POINTS):
        return False

    if q.get("type") == "multiple_choice":
        options = q.get("options")
        correct_index = q.get("correct_index")
        return (
            isinstance(options, list)
            and 0 < len(options) <= MAX_OPTIONS
            and all(isinstance(o, str) and o for o in options)
            and (correct_index is None or (isinstance(correct_index, int) and not isinstance(correct_index, bool) and 0 <= correct_index < len(options)))
        )
    if q.get("type") == "free_response":
        accepted = q.get("accepted_answers")
        return accepted is None or (isinstance(accepted, list) and all(isinstance(a, str) and a for a in accepted))
    if q.get("type") == "multi_select":
        options = q.get("options")
        if not (isinstance(options, list) and 0 < len(options) <= MAX_OPTIONS and all(isinstance(o, str) and o for o in options)):
            return False
        correct_indices = q.get("correct_indices")
        if correct_indices is None:
            return True
        return (
            isinstance(correct_indices, list)
            and len(correct_indices) > 0
            and all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(options) for i in correct_indices)
        )
    # matching_pair — always gradable, so there's no "None means ungraded"
    # case the way the other three types have (see the module docstring's
    # "Question shape" section).
    pairs = q.get("pairs")
    if not (
        isinstance(pairs, list)
        and MIN_PAIRS <= len(pairs) <= MAX_PAIRS
        and all(
            isinstance(p, dict) and isinstance(p.get("left"), str) and p["left"] and isinstance(p.get("right"), str) and p["right"]
            for p in pairs
        )
    ):
        return False
    distractors = q.get("distractors")
    return distractors is None or (
        isinstance(distractors, list)
        and len(distractors) <= MAX_DISTRACTORS
        and all(isinstance(d, str) and d for d in distractors)
    )


def migrate_question_bank(raw) -> list[dict]:
    """Reads whatever's actually on disk under questions.json and
    always returns a valid flat bank (see the module docstring's
    "Question shape") — never a crash, never a silently-broken class,
    no matter which of these `raw` turns out to be:
      - the CURRENT shape, {"questions": [question, ...]} — each entry
        still individually validated (see _valid_question), since this
        is a file a human COULD hand-edit; an invalid entry is dropped
        rather than propagated into a plugin that assumes every bank
        entry is well-formed everywhere else.
      - the OLDER pre-question-types shape, a bare {tier: [(text,
        options, correct_index), ...], ...} dict (JSON has no tuple
        type, so this is really lists-of-3-elements on disk) — every
        entry becomes a fresh multiple_choice question with a newly
        minted id (the old shape never had one) and hint=None.
      - anything else (missing file, corrupt JSON, a hand-edited file
        that's neither shape) — treated as "no saved bank at all".
    Callers (load_class_bundle) still heal any tier left with zero
    questions afterward, same as before this existed.
    """
    if isinstance(raw, dict) and isinstance(raw.get("questions"), list):
        # "points" is newer than this on-disk shape itself — default it
        # for any entry saved before it existed, same "missing means the
        # old, unaffected behavior" posture as every other field this
        # migration function has ever had to backfill.
        return [{**q, "points": q.get("points", DEFAULT_POINTS)} for q in raw["questions"] if _valid_question(q)]

    migrated: list[dict] = []
    if isinstance(raw, dict):
        for tier in TIERS:
            entries = raw.get(tier)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not (isinstance(entry, (list, tuple)) and len(entry) == 3):
                    continue
                text, options, correct_index = entry
                if not isinstance(text, str) or not isinstance(options, list):
                    continue
                migrated.append(
                    {
                        "id": new_question_id(),
                        "tier": tier,
                        "type": "multiple_choice",
                        "text": text,
                        "hint": None,
                        "points": DEFAULT_POINTS,
                        "options": list(options),
                        "correct_index": correct_index if isinstance(correct_index, int) else None,
                        "accepted_answers": None,
                        "correct_indices": None,
                        "pairs": None,
                    }
                )
    return migrated


def heal_question_bank(bank: list[dict]) -> list[dict]:
    """Tops up any tier that ended up with zero questions (an empty/
    corrupt file, or every question of that tier having been deleted —
    on_authoring_command's delete_question already refuses to remove
    the LAST question of a tier, so this is a belt-and-suspenders
    backstop for a hand-edited file, not something normal use should
    ever actually trigger) with a freshly-seeded default bank for just
    that tier — never leaves next_question() with nothing to draw
    from."""
    healed = list(bank)
    present_tiers = {q["tier"] for q in healed}
    for tier in TIERS:
        if tier in present_tiers:
            continue
        healed.extend(q for q in default_question_bank() if q["tier"] == tier)
    return healed


def load_class_bundle(code: str) -> dict | None:
    """Reads a previously-persisted class back off disk for on_load_class
    to resume from. None if `code` was never persisted (unknown/typo'd
    code, or a code that was generated but never actually used) —
    on_load_class treats that as "ignore", the same silent-drop posture
    every other malformed/unrecognized input in this file already has."""
    class_dir = class_dir_for(code)
    meta = _read_json(class_dir / "meta.json", None)
    if not isinstance(meta, dict):
        return None
    bank = heal_question_bank(migrate_question_bank(_read_json(class_dir / "questions.json", {})))

    materials = _read_json(class_dir / "materials.json", {})
    chapters_raw = materials.get("chapters") if isinstance(materials, dict) else None
    question_ids = {q["id"] for q in bank}
    chapters: list[dict] = []
    if isinstance(chapters_raw, list):
        for ch in chapters_raw:
            if not (isinstance(ch, dict) and isinstance(ch.get("id"), str) and isinstance(ch.get("name"), str)):
                continue
            ids = ch.get("question_ids")
            # Drop any reference to a question that no longer exists
            # (deleted since, or dropped by _valid_question above) —
            # same "heal rather than propagate a dangling reference"
            # posture as the bank healing right above.
            clean_ids = [qid for qid in ids if isinstance(qid, str) and qid in question_ids] if isinstance(ids, list) else []
            chapters.append({"id": ch["id"], "name": ch["name"], "question_ids": clean_ids})

    # Same "heal rather than propagate a dangling reference" posture as
    # the chapters themselves above — a chapter referenced here that no
    # longer exists (deleted since, or dropped by the isinstance checks
    # above) resolves to "no active chapter" (the whole bank), never a
    # crash or a silently-broken filter next_question() can't recover
    # from.
    chapter_ids = {c["id"] for c in chapters}
    active_chapter = materials.get("active_chapter") if isinstance(materials, dict) else None
    if not isinstance(active_chapter, str) or active_chapter not in chapter_ids:
        active_chapter = None

    class_display_name = meta.get("class_name")
    if not isinstance(class_display_name, str) or not class_display_name:
        class_display_name = f"Class {code}"
    return {"class_name": class_display_name, "questions": bank, "chapters": chapters, "active_chapter": active_chapter}


def normalize_answer(raw: str) -> str:
    """Plain string/numeric equivalence, never an AI/model call (see
    the module docstring) — "12", "12.0", and "12.00" all normalize to
    the same key, and non-numeric text is trimmed + case-folded so
    "Paris"/"paris "/" PARIS" all match too. Applied identically to a
    submitted free_response answer and to every one of a question's
    own accepted_answers before comparing, so grade_answer never has to
    duplicate this logic."""
    trimmed = raw.strip()
    try:
        value = float(trimmed)
    except ValueError:
        return trimmed.casefold()
    if not math.isfinite(value):
        # "inf"/"nan" parse as valid floats but aren't real numeric
        # answers a math question would ever have — fall back to plain
        # text comparison rather than treating them as equivalent to
        # every other non-finite spelling.
        return trimmed.casefold()
    if value == int(value):
        return str(int(value))
    return repr(value)


with World() as world:
    name = world.identify("math-teacher")

    class_code = generate_class_code()
    class_name = f"Class {class_code}"

    def class_attr(attr_name: str) -> str:
        """Every class-scoped attribute name goes through this — see
        the module docstring's "Class identity" section. class_code
        and class_name themselves are the only two exceptions,
        published bare so a joining window can discover them at all."""
        return f"{attr_name}:{class_code}"

    seed_bank = default_question_bank()
    print(
        f"[{name}] math class '{class_name}' started (code {class_code}), "
        f"{len(seed_bank)} questions across {len(TIERS)} tiers",
        flush=True,
    )

    control = world.create_entity()
    world.set_many([
        (control, "kind", "math_class"),
        (control, "class_code", class_code),
        (control, "class_name", class_name),
    ])

    adaptive_mode = "off"
    scoreboard_enabled = False
    leave_locked = False
    round_index = -1
    question_deadline = SECONDS_PER_QUESTION

    # This class's own question bank — the freshly-seeded default until
    # (and unless) ensure_persisted() seeds a real questions.json, or
    # on_load_class replaces it wholesale with a resumed class's own
    # file. See the module docstring's "Question shape" for why this is
    # a flat list with a "tier" field per entry, not grouped by tier.
    class_question_bank: list[dict] = seed_bank
    # This class's chapters — see the module docstring's "Chapters".
    # Empty for a freshly-generated class, same as materials.json's own
    # seed shape.
    class_chapters: list[dict] = []
    # Which chapter (if any) next_question() actually draws from — see
    # questions_for_tier below. None (the default, and the only possible
    # value before this existed) means "the whole bank", exactly
    # today's original behavior; set via on_authoring_command's
    # "set_active_chapter".
    class_active_chapter: str | None = None
    # Whether THIS class's code/name/question-bank have ever been
    # written to disk yet — see ensure_persisted().
    class_persisted = False

    def questions_for_tier(tier: str) -> list[dict]:
        """Every question of `tier` next_question() may currently draw
        from. Filtered down to class_active_chapter's own question_ids
        when one is set — BUT falls back to the whole tier if that
        filtered set would be empty (a chapter with nothing at all in
        this particular tier), same "never leave next_question() with
        nothing to draw from" posture heal_question_bank already has for
        a tier missing from the bank entirely."""
        pool = [q for q in class_question_bank if q["tier"] == tier]
        if class_active_chapter is None:
            return pool
        chapter = next((c for c in class_chapters if c["id"] == class_active_chapter), None)
        if chapter is None:
            return pool
        filtered = [q for q in pool if q["id"] in chapter["question_ids"]]
        return filtered if filtered else pool

    # Populated by next_question(); safe non-empty defaults here so an
    # "answer" replay racing ahead of the first next_question() call
    # (pushes dispatch on their own thread) never indexes into an
    # empty dict. Each tier's value is {"text", "type", "hint", "points",
    # "options" (multiple_choice/multi_select, else None), "correct_index"
    # (multiple_choice, None if ungraded or n/a), "accepted_answers"
    # (free_response, None if ungraded or n/a), "correct_indices"
    # (multi_select, None if ungraded or n/a), "pairs"+"right_order"
    # (matching_pair only, omitted entirely for every other type — see
    # next_question()/replace_current_question()), "source"}.
    current_active_tiers: list[str] = [DEFAULT_TIER]
    _seed_question = questions_for_tier(DEFAULT_TIER)[0]
    current_bank_by_tier: dict[str, dict] = {
        DEFAULT_TIER: {
            "text": _seed_question["text"],
            "type": _seed_question["type"],
            "hint": _seed_question["hint"],
            "points": _seed_question["points"],
            "options": _seed_question["options"],
            "correct_index": _seed_question["correct_index"],
            "accepted_answers": _seed_question["accepted_answers"],
            "correct_indices": _seed_question["correct_indices"],
            "source": "builtin",
        }
    }

    # entity -> {"tier": str, "option": int}, only for the CURRENT round.
    answers_by_student: dict[int, dict] = {}
    # Every student entity this plugin has agreed to track, gated by
    # MAX_TRACKED_STUDENTS in ensure_student — every dict below only
    # ever gets an entry for an id that's in this set.
    known_students: set[int] = set()
    student_tier_by_id: dict[int, str] = {}
    # entity -> {"correct": int, "total": int, "streak": int,
    #            "last_graded_round": int}
    student_stats: dict[int, dict] = {}
    recent_correct_by_id: dict[int, list] = {}
    # entity -> best-known display name, from any math_student's bare
    # "student_name" (see on_student_name) — global across every class
    # on this server (student_name isn't class_attr()-scoped), which is
    # harmless: only ever consulted for an entity this class's OWN
    # known_students already contains. Capped the same way and for the
    # same reason as known_students itself.
    student_name_by_id: dict[int, str] = {}

    def ensure_student(entity: int) -> bool:
        """Registers `entity` as a known/tracked student if it isn't
        already, subject to MAX_TRACKED_STUDENTS. Returns whether
        `entity` is (now) safe to record per-student data for."""
        if entity in known_students:
            return True
        if len(known_students) >= MAX_TRACKED_STUDENTS:
            return False
        known_students.add(entity)
        student_tier_by_id.setdefault(entity, DEFAULT_TIER)
        # A student actually joining is the observable "this class is
        # for real" moment this file already has (this IS the existing
        # live-tally join-detection point, not a new mechanism) — see
        # the module docstring's "Persistence" section.
        ensure_persisted()
        append_roster_entry(
            class_code, entity, student_name_by_id.get(entity, f"Student {entity}")
        )
        return True

    def ensure_persisted():
        """No-ops after the first call for this class — see the module
        docstring's "Persistence" section for exactly what "first" means
        and why it's not simply "at startup"."""
        global class_persisted
        if class_persisted:
            return
        class_persisted = True
        persist_new_class(class_code, class_name, class_question_bank)
        publish_known_classes()
        publish_authoring_state()

    def shift_tier(entity: int, direction: int):
        current = student_tier_by_id.get(entity, DEFAULT_TIER)
        idx = TIERS.index(current) if current in TIERS else TIERS.index(DEFAULT_TIER)
        student_tier_by_id[entity] = TIERS[max(0, min(len(TIERS) - 1, idx + direction))]

    def grade_answer(
        entity: int,
        tier: str,
        option: int | None,
        text: str | None,
        selected: list[int] | None = None,
        pairs_answer: dict[int, int] | None = None,
    ):
        """Grades `entity`'s answer for the CURRENT round against
        `tier`'s current question, at most once per round no matter how
        many times they resubmit — a resubmission still updates the
        live tally (see on_answer) but must not be graded twice, or
        streak/score would drift from "one answer, one grade" into
        something repeated resubmits could game. A question with no
        known correct answer at all (multiple_choice with no
        correct_index, free_response with no accepted_answers, or
        multi_select with no correct_indices — a teacher "replace"
        without one, or an authored question left deliberately ungraded)
        is simply not gradeable this round — tallied, never scored.
        matching_pair has no such state; it's always gradable (see the
        module docstring's "Question shape" section). Exactly one of
        `option`/`text`/`selected`/`pairs_answer` is ever non-None —
        on_answer already cross-checked it against this tier's actual
        type before calling this at all.

        Computes `earned` points out of the question's own `points`
        (see the module docstring's "Scoring" section) — all-or-nothing
        for every type except multi_select worth more than 1 point,
        which gets proportional partial credit. `full_credit` (earned ==
        points) is what streak-counting and "automatic" adaptive-mode
        tiering key off of, not raw correctness — a partially-correct
        multi_select is not a "correct answer" for either purpose.
        """
        bank = current_bank_by_tier[tier]
        points = bank["points"]
        if bank["type"] == "multiple_choice":
            correct_index = bank["correct_index"]
            if correct_index is None or option is None:
                return
            earned = points if option == correct_index else 0
        elif bank["type"] == "free_response":
            accepted = bank["accepted_answers"]
            if not accepted or text is None:
                return
            is_correct = normalize_answer(text) in {normalize_answer(a) for a in accepted}
            earned = points if is_correct else 0
        elif bank["type"] == "matching_pair":
            if pairs_answer is None:
                return
            right_order = bank["right_order"]
            is_correct = all(right_order[right_pos] == left_index for left_index, right_pos in pairs_answer.items())
            earned = points if is_correct else 0
        else:  # multi_select
            correct_indices = bank["correct_indices"]
            if not correct_indices or selected is None:
                return
            correct_set = set(correct_indices)
            if points <= 1:
                # Points-driven grading rule, not just weighting — a
                # 1-point multi_select needs an EXACT match, same as
                # LaMa's own grading (see the module docstring).
                earned = points if set(selected) == correct_set else 0
            else:
                got_right = len(set(selected) & correct_set)
                earned = points * got_right / len(correct_set)
        full_credit = earned == points

        stats = student_stats.setdefault(
            entity, {"correct": 0, "total": 0, "streak": 0, "last_graded_round": -1}
        )
        if stats["last_graded_round"] == round_index:
            return
        stats["last_graded_round"] = round_index
        stats["total"] += points
        stats["correct"] += earned
        if full_credit:
            stats["streak"] += 1
        else:
            stats["streak"] = 0

        if adaptive_mode != "automatic":
            # "manual" (and "off") leave student_tier alone — see the
            # module docstring on why manual and automatic never fight
            # over the same entry.
            return
        window = recent_correct_by_id.setdefault(entity, [])
        window.append(full_credit)
        # Keep only as much history as the larger of the two windows
        # needs; each direction below then looks at just its own
        # trailing slice of it.
        del window[:-max(TIER_UP_WINDOW, TIER_DOWN_WINDOW)]
        if len(window) >= TIER_UP_WINDOW and all(window[-TIER_UP_WINDOW:]):
            shift_tier(entity, +1)
            window.clear()
        elif len(window) >= TIER_DOWN_WINDOW and not any(window[-TIER_DOWN_WINDOW:]):
            shift_tier(entity, -1)
            window.clear()

    def publish_student_state():
        # student_tier published as {} while "off" so a student
        # falling back to the plain question_text/question_options
        # shape has nothing in student_tier to even look at —
        # reproducing the old behavior exactly, not just approximately.
        tier_map = (
            {}
            if adaptive_mode == "off"
            else {str(e): student_tier_by_id.get(e, DEFAULT_TIER) for e in known_students}
        )
        score_map = {
            str(e): {"correct": stats["correct"], "total": stats["total"]}
            for e, stats in student_stats.items()
        }
        streak_map = {str(e): stats["streak"] for e, stats in student_stats.items()}
        # Best-effort display name per known student — see the module
        # docstring's "student_names" entry. Purely cosmetic (every
        # consumer already has a "Student <id>" fallback for anyone
        # missing here), so an entity whose name hasn't arrived yet is
        # simply omitted rather than treated as an error.
        name_map = {str(e): student_name_by_id[e] for e in known_students if e in student_name_by_id}
        world.set_many([
            (control, class_attr("student_tier"), tier_map),
            (control, class_attr("student_score"), score_map),
            (control, class_attr("student_streak"), streak_map),
            (control, class_attr("student_names"), name_map),
        ])
        if scoreboard_enabled:
            publish_scoreboard()

    def publish_scoreboard():
        # Caller's job to only call this while scoreboard_enabled is
        # true — the "turn it off" path instead writes [] directly, so
        # there's no ambiguity between "just computed an empty board"
        # and "board is disabled".
        rows = [
            {
                "student": str(e),
                "correct": stats["correct"],
                "total": stats["total"],
                "streak": stats["streak"],
            }
            for e, stats in student_stats.items()
        ]
        rows.sort(key=lambda row: row["correct"], reverse=True)
        world.set_attribute(control, class_attr("scoreboard"), rows)

    def publish_tally():
        # Same list[int] SHAPE for every question type (see the module
        # docstring's "Entities published" — never something a consumer
        # has to branch on just to read length/sum), different
        # interpretation per type: multiple_choice is one count per
        # option index; multi_select is likewise one count per option,
        # but NOT mutually exclusive (a student's own selection can set
        # more than one); free_response and matching_pair are both
        # [correct_count, incorrect_count] — correctness re-derived here
        # from the question's own answer key rather than cached, since
        # nothing about this aggregate reveals what the right answer WAS
        # the way echoing accepted_answers/pairs itself would.
        tally_by_tier = {}
        for tier in current_active_tiers:
            bank = current_bank_by_tier[tier]
            records = [r for r in answers_by_student.values() if r["tier"] == tier]
            if bank["type"] == "multiple_choice":
                counts = [0] * len(bank["options"])
                for record in records:
                    option = record["option"]
                    if option is not None and 0 <= option < len(counts):
                        counts[option] += 1
                tally_by_tier[tier] = counts
            elif bank["type"] == "multi_select":
                counts = [0] * len(bank["options"])
                for record in records:
                    for i in record["selected"] or []:
                        if 0 <= i < len(counts):
                            counts[i] += 1
                tally_by_tier[tier] = counts
            elif bank["type"] == "matching_pair":
                right_order = bank["right_order"]
                correct = sum(
                    1 for r in records
                    if r["pairs"] is not None and all(right_order[rp] == li for li, rp in r["pairs"].items())
                )
                tally_by_tier[tier] = [correct, len(records) - correct]
            else:  # free_response
                accepted = {normalize_answer(a) for a in (bank["accepted_answers"] or [])}
                correct = sum(1 for r in records if r["text"] is not None and accepted and normalize_answer(r["text"]) in accepted)
                tally_by_tier[tier] = [correct, len(records) - correct]
        mirror_tier = DEFAULT_TIER if DEFAULT_TIER in tally_by_tier else current_active_tiers[0]
        world.set_many([
            (control, class_attr("answer_tally_by_tier"), tally_by_tier),
            (control, class_attr("answer_tally"), tally_by_tier[mirror_tier]),
        ])

    def publish_current_round():
        # Uniform key set across every tier/type (see the module
        # docstring's "Entities published" — "left_items"/"right_items"
        # are None except for matching_pair, same "not every type uses
        # every field, but every field is always present" posture the
        # bank's own shape already has) — right_items is bank["pairs"]
        # read through bank["right_order"]'s SHUFFLED order, never the
        # pairs' own original order, so it never leaks the answer.
        question_by_tier_public = {
            tier: {
                "text": bank["text"],
                "type": bank["type"],
                "hint": bank["hint"],
                "points": bank["points"],
                "options": bank["options"] if bank["type"] in ("multiple_choice", "multi_select") else None,
                "left_items": [p["left"] for p in bank["pairs"]] if bank["type"] == "matching_pair" else None,
                "right_items": (
                    [
                        bank["pairs"][i]["right"] if i < len(bank["pairs"]) else bank["distractors"][i - len(bank["pairs"])]
                        for i in bank["right_order"]
                    ]
                    if bank["type"] == "matching_pair" else None
                ),
            }
            for tier, bank in current_bank_by_tier.items()
        }
        mirror_tier = DEFAULT_TIER if DEFAULT_TIER in current_bank_by_tier else current_active_tiers[0]
        mirror = current_bank_by_tier[mirror_tier]
        world.set_many([
            (control, class_attr("question_by_tier"), question_by_tier_public),
            (control, class_attr("question_text"), mirror["text"]),
            (control, class_attr("question_options"), mirror["options"] if mirror["type"] in ("multiple_choice", "multi_select") else []),
            (control, class_attr("question_type"), mirror["type"]),
            (control, class_attr("question_hint"), mirror["hint"]),
            (control, class_attr("question_points"), mirror["points"]),
            (control, class_attr("question_source"), mirror["source"]),
            (control, class_attr("question_index"), round_index),
            (control, class_attr("adaptive_mode"), adaptive_mode),
        ])

    def publish_known_classes():
        """Publishes the shared index.json (every class ever persisted,
        by ANY math-teacher process on this machine) bare/unnamespaced
        on this instance's own control entity — see the module
        docstring's "Persistence" section. A frontend picker subscribes
        to this the same way it already discovers class_code/
        class_name, no new IPC mechanism needed."""
        world.set_attribute(control, "known_classes", load_index())

    def publish_authoring_state():
        """The teacher-only bank/chapters editor's own view — see the
        module docstring's "Authoring" section on why this is safe to
        publish with correct_index/accepted_answers included (a
        Core-level, name-prefix-keyed rule refuses this to any
        non-loopback connection, regardless of class-code knowledge).
        Called after every authoring mutation, and once at startup/
        resume so a teacher's dashboard has something to show even
        before anything's been touched."""
        world.set_many([
            (control, class_attr("authoring_question_bank"), class_question_bank),
            (control, class_attr("authoring_chapters"), class_chapters),
            (control, class_attr("authoring_active_chapter"), class_active_chapter),
        ])

    def _round_entry(q: dict, source: str) -> dict:
        """Builds a current_bank_by_tier[tier] entry from a question
        dict (either a stored bank question — see the module docstring's
        "Question shape" — or validate_question_content()'s return
        shape, which is a superset of the same field names). Shared by
        next_question() and replace_current_question() so the two never
        drift apart on what a round's own state actually holds,
        including matching_pair's per-round shuffle (see
        question_by_tier's own doc comment on why "right_items" is never
        published in the pairs' original order)."""
        entry = {
            "text": q["text"],
            "type": q["type"],
            "hint": q["hint"],
            "points": q.get("points", DEFAULT_POINTS),
            "options": q.get("options"),
            "correct_index": q.get("correct_index"),
            "accepted_answers": q.get("accepted_answers"),
            "correct_indices": q.get("correct_indices"),
            "source": source,
        }
        if q["type"] == "matching_pair":
            pairs = q["pairs"]
            distractors = q.get("distractors") or []
            # right_order is a shuffled permutation over BOTH real pairs
            # and distractors together — a value < len(pairs) names
            # which pair's own right text sits at that shuffled
            # position, a value >= len(pairs) names a distractor (see
            # this module's "Question shape" section). Distractor
            # "identity" is never anyone's correct answer, so
            # grade_answer's existing `right_order[right_pos] ==
            # left_index` check already handles distractors correctly
            # with no change needed there — a distractor's own value
            # can never equal a real left_index.
            right_order = list(range(len(pairs) + len(distractors)))
            random.shuffle(right_order)
            entry["pairs"] = pairs
            entry["distractors"] = distractors
            entry["right_order"] = right_order
        return entry

    def next_question():
        global round_index, current_active_tiers, current_bank_by_tier, question_deadline
        round_index += 1
        answers_by_student.clear()
        question_deadline = SECONDS_PER_QUESTION
        current_active_tiers = list(TIERS) if adaptive_mode != "off" else [DEFAULT_TIER]
        current_bank_by_tier = {}
        for tier in current_active_tiers:
            bank = questions_for_tier(tier)
            q = bank[round_index % len(bank)]
            current_bank_by_tier[tier] = _round_entry(q, "builtin")
        publish_current_round()
        publish_student_state()
        publish_tally()

    def replace_current_question(tier: str, content: dict):
        # Edits ONE tier's question CURRENTLY up, in place —
        # round_index deliberately does not change, since this is "the
        # teacher fixed a typo/swapped the numbers on what's already
        # shown", not "a new question started". Does NOT touch the
        # saved bank (see the module docstring's "Authoring" section on
        # why the two are kept separate). That tier's existing answers
        # are cleared from the tally, since they were cast against
        # content that, from the students' point of view, no longer
        # exists. `content` is validate_question_content()'s own return
        # shape.
        current_bank_by_tier[tier] = _round_entry(content, "teacher_edited")
        for entity in list(answers_by_student):
            if answers_by_student[entity]["tier"] == tier:
                del answers_by_student[entity]
        publish_current_round()
        publish_tally()

    def on_answer(entity, attribute, value, source):
        # Trust nothing about this payload's shape or contents — it
        # comes from whatever connected and called itself a student
        # (see this file's bottom note on why that's not actually
        # verified yet). Late-joining students also replay every
        # answer ever set, including ones for questions/tiers we've
        # since moved past — dropping those here (rather than in
        # publish_tally) keeps answers_by_student holding only what's
        # actually relevant right now.
        if not isinstance(value, dict) or value.get("question_index") != round_index:
            return
        if entity not in known_students and len(known_students) >= MAX_TRACKED_STUDENTS:
            return  # already at the cap and this isn't an update to an existing student — drop it

        tier = value.get("tier")
        if not isinstance(tier, str) or tier not in current_active_tiers:
            # Missing/unknown/inactive tier — assume DEFAULT_TIER,
            # which is exactly right for a student that predates
            # tiering, or whenever adaptive_mode is "off" (the only
            # active tier then anyway).
            tier = DEFAULT_TIER if DEFAULT_TIER in current_active_tiers else current_active_tiers[0]

        bank = current_bank_by_tier[tier]
        option: int | None = None
        text: str | None = None
        selected: list[int] | None = None
        pairs_answer: dict[int, int] | None = None
        if bank["type"] == "multiple_choice":
            option = value.get("option")
            # bool is a subclass of int in Python -- exclude it
            # explicitly, same as on_teacher_command's "extend" already
            # does for "seconds".
            if not isinstance(option, int) or isinstance(option, bool) or not (0 <= option < len(bank["options"])):
                return
        elif bank["type"] == "free_response":
            text = value.get("text")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH:
                return
            # A student's own free-text answer is never itself stripped
            # of surrounding whitespace before storage — normalize_answer
            # handles that at GRADING time (see grade_answer); keeping
            # the raw text here is what lets the teacher's live tally
            # (see publish_tally) show exactly what was typed, not a
            # silently-modified version of it.
        elif bank["type"] == "multi_select":
            raw_selected = value.get("selected")
            n = len(bank["options"])
            if not isinstance(raw_selected, list) or not raw_selected:
                return
            cleaned: set[int] = set()
            for i in raw_selected:
                if not isinstance(i, int) or isinstance(i, bool) or not (0 <= i < n):
                    return  # any malformed entry drops the whole answer, same posture as everywhere else here
                cleaned.add(i)
            selected = sorted(cleaned)
        else:  # matching_pair
            raw_pairs = value.get("pairs")
            n_left = len(bank["pairs"])
            n_right = n_left + len(bank["distractors"])  # right_items is longer than left_items whenever there are distractors
            if not isinstance(raw_pairs, dict) or len(raw_pairs) != n_left:
                return
            parsed: dict[int, int] = {}
            for raw_left, right_pos in raw_pairs.items():
                try:
                    left_index = int(raw_left)
                except (TypeError, ValueError):
                    return
                if not isinstance(right_pos, int) or isinstance(right_pos, bool) or not (0 <= left_index < n_left) or not (0 <= right_pos < n_right):
                    return
                parsed[left_index] = right_pos
            # Must name every left item exactly once, each pointing at a
            # DISTINCT right position (duplicates make no sense — two
            # left items can't both correctly point at the same right
            # slot) — a partial or duplicate-valued mapping is dropped
            # rather than partially graded (see the module docstring's
            # "Reads" section). A right_pos landing on a distractor slot
            # is a perfectly valid submission here, just never a correct
            # one — grade_answer sorts that out, not this shape check.
            if set(parsed.keys()) != set(range(n_left)) or len(set(parsed.values())) != n_left:
                return
            pairs_answer = parsed

        if not ensure_student(entity):
            return
        answers_by_student[entity] = {
            "tier": tier, "option": option, "text": text, "selected": selected, "pairs": pairs_answer,
        }
        publish_tally()
        grade_answer(entity, tier, option, text, selected, pairs_answer)
        publish_student_state()

    def on_student_name(entity, attribute, value, source):
        # Bare/global (see module docstring) — every math_student on
        # this machine publishes this, regardless of which class it
        # belongs to. Only ever actually consulted (in ensure_student)
        # for an entity this class's own known_students already
        # contains, so aggregating indiscriminately here is harmless.
        if not isinstance(value, str) or not value:
            return
        if entity not in student_name_by_id and len(student_name_by_id) >= MAX_TRACKED_STUDENTS:
            return
        student_name_by_id[entity] = value

    def on_adaptive_mode_external(entity, attribute, value, source):
        # Ignore our own pushes echoing back — only adopt a mode set
        # by someone else (a teacher UI toggling the control).
        global adaptive_mode
        if source == name or entity != control:
            return
        if value not in ADAPTIVE_MODES or value == adaptive_mode:
            return
        ensure_persisted()  # a teacher actively configuring this class is real use
        adaptive_mode = value
        next_question()  # re-broadcast under the new mode now, not up to SECONDS_PER_QUESTION from now

    def on_student_tier_external(entity, attribute, value, source):
        # A future teacher UI's "set this student's tier by hand" —
        # the whole mechanism "manual" mode is meant to be driven
        # through. Only ever updates a student this plugin already
        # knows about, and only to one of the real tiers, so a bad or
        # hostile write here can't inflate the tracked-student set past
        # MAX_TRACKED_STUDENTS or invent a fake tier.
        if source == name or entity != control or not isinstance(value, dict):
            return
        changed = False
        for raw_id, tier in value.items():
            try:
                student_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if student_id in known_students and tier in TIERS and student_tier_by_id.get(student_id) != tier:
                student_tier_by_id[student_id] = tier
                changed = True
        if changed:
            ensure_persisted()
            publish_student_state()

    def on_scoreboard_enabled_external(entity, attribute, value, source):
        # Mirrors city_weather.py's on_city_hour_external idiom: ignore
        # our own writes (the initial default below), adopt anything
        # set by someone else — the eventual teacher UI toggle.
        global scoreboard_enabled
        if source == name or entity != control:
            return
        if not isinstance(value, bool) or value == scoreboard_enabled:
            return
        ensure_persisted()
        scoreboard_enabled = value
        if scoreboard_enabled:
            publish_scoreboard()
        else:
            # Don't just stop updating it — actively clear it, so a
            # student who already read a stale scoreboard before it
            # was turned off can't keep it around by never re-reading.
            world.set_attribute(control, class_attr("scoreboard"), [])

    def on_leave_locked_external(entity, attribute, value, source):
        # Same idiom as on_scoreboard_enabled_external right above:
        # ignore our own writes (there are none — this plugin never sets
        # leave_locked itself), adopt anything set by someone else (the
        # teacher dashboard's own toggle). Unlike scoreboard_enabled,
        # nothing here needs clearing on either transition — this value
        # IS the whole state; a student's own KioskShell.tsx just reads
        # it directly (see the module docstring's "leave_locked" entry).
        global leave_locked
        if source == name or entity != control:
            return
        if not isinstance(value, bool) or value == leave_locked:
            return
        ensure_persisted()
        leave_locked = value

    def validate_question_content(value: dict) -> dict | None:
        """Shared shape/bound validation for a question's CONTENT
        fields — everything except "id" and "tier" — used by
        teacher_command's "replace" and authoring_command's
        add_question/edit_question/import_questions below, so the exact
        same rules apply no matter which way a question's content
        arrives. Returns {"type", "text", "hint", "points", "options",
        "correct_index", "accepted_answers", "correct_indices", "pairs",
        "distractors"} (every key always present, None for whichever the type doesn't
        use — see the module docstring's "Question shape" section) or
        None if anything at all is invalid — every caller's response to
        None is the same "silently drop the whole command" every other
        malformed input here already gets."""
        type_ = value.get("type", "multiple_choice")
        if type_ not in QUESTION_TYPES:
            return None
        text = value.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH:
            return None
        hint = value.get("hint")
        if hint is not None and (not isinstance(hint, str) or len(hint) > MAX_HINT_LENGTH):
            return None
        points = value.get("points", DEFAULT_POINTS)
        if isinstance(points, bool) or not isinstance(points, int) or not (1 <= points <= MAX_POINTS):
            return None

        base = {
            "type": type_, "text": text, "hint": hint, "points": points,
            "options": None, "correct_index": None, "accepted_answers": None,
            "correct_indices": None, "pairs": None, "distractors": None,
        }

        if type_ == "multiple_choice":
            options = value.get("options")
            correct_index = value.get("correct_index")
            if not isinstance(options, list) or not (0 < len(options) <= MAX_OPTIONS):
                return None
            if not all(isinstance(opt, str) and 0 < len(opt) <= MAX_OPTION_LENGTH for opt in options):
                return None
            if correct_index is not None and (
                isinstance(correct_index, bool)
                or not isinstance(correct_index, int)
                or not (0 <= correct_index < len(options))
            ):
                return None  # a nonsensical correct_index is treated as "don't grade this", not silently clamped
            return {**base, "options": list(options), "correct_index": correct_index}

        if type_ == "free_response":
            accepted_answers = value.get("accepted_answers")
            if accepted_answers is not None and not (
                isinstance(accepted_answers, list)
                and 0 < len(accepted_answers) <= MAX_ACCEPTED_ANSWERS
                and all(isinstance(a, str) and 0 < len(a) <= MAX_ACCEPTED_ANSWER_LENGTH for a in accepted_answers)
            ):
                return None
            return {**base, "accepted_answers": accepted_answers}

        if type_ == "multi_select":
            options = value.get("options")
            correct_indices = value.get("correct_indices")
            if not isinstance(options, list) or not (0 < len(options) <= MAX_OPTIONS):
                return None
            if not all(isinstance(opt, str) and 0 < len(opt) <= MAX_OPTION_LENGTH for opt in options):
                return None
            if correct_indices is not None:
                if not isinstance(correct_indices, list) or not correct_indices:
                    return None  # an explicit-but-empty correct set makes no sense — same "don't pretend to grade" refusal as a nonsensical correct_index
                cleaned_indices: set[int] = set()
                for i in correct_indices:
                    if isinstance(i, bool) or not isinstance(i, int) or not (0 <= i < len(options)):
                        return None
                    cleaned_indices.add(i)
                correct_indices = sorted(cleaned_indices)
            return {**base, "options": list(options), "correct_indices": correct_indices}

        # matching_pair — always gradable (no "leave it ungraded" option,
        # see the module docstring), so "pairs" is required here, unlike
        # every other type's answer-key field above. "distractors" is
        # optional — extra right-side texts that are never correct for
        # any left item (see the module docstring's "Question shape").
        pairs = value.get("pairs")
        if not isinstance(pairs, list) or not (MIN_PAIRS <= len(pairs) <= MAX_PAIRS):
            return None
        cleaned_pairs: list[dict] = []
        for pair in pairs:
            if not isinstance(pair, dict):
                return None
            left, right = pair.get("left"), pair.get("right")
            if not isinstance(left, str) or not (0 < len(left) <= MAX_OPTION_LENGTH):
                return None
            if not isinstance(right, str) or not (0 < len(right) <= MAX_OPTION_LENGTH):
                return None
            cleaned_pairs.append({"left": left, "right": right})
        distractors = value.get("distractors")
        if distractors is not None:
            if not isinstance(distractors, list) or len(distractors) > MAX_DISTRACTORS:
                return None
            if not all(isinstance(d, str) and 0 < len(d) <= MAX_OPTION_LENGTH for d in distractors):
                return None
            distractors = list(distractors)
        return {**base, "pairs": cleaned_pairs, "distractors": distractors}

    def on_teacher_command(entity, attribute, value, source):
        # Same untrusted-input posture as on_answer above: stands in
        # for a not-yet-built teacher UI, but until there's auth (see
        # this file's bottom note), anything connected can send this.
        # Shape-check everything; on any mismatch, silently drop
        # rather than raise.
        global elapsed, question_deadline
        if entity != control or not isinstance(value, dict):
            return
        ensure_persisted()  # a teacher actively driving this session is real use
        cmd = value.get("cmd")

        if cmd == "next":
            next_question()
            elapsed = 0.0

        elif cmd == "replace":
            tier = value.get("tier", DEFAULT_TIER if DEFAULT_TIER in current_active_tiers else current_active_tiers[0])
            if not isinstance(tier, str) or tier not in current_active_tiers:
                return
            content = validate_question_content(value)
            if content is None:
                return
            replace_current_question(tier, content)

        elif cmd == "extend":
            seconds = value.get("seconds")
            # bool is a subclass of int in Python — exclude it
            # explicitly so {"cmd": "extend", "seconds": true} doesn't
            # sneak through isinstance(seconds, (int, float)).
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                return
            if not math.isfinite(seconds):
                return  # rejects NaN/Infinity, both valid JSON-module output
            clamped = max(EXTEND_SECONDS_MIN, min(EXTEND_SECONDS_MAX, float(seconds)))
            # Grows the deadline the main loop compares `elapsed`
            # against, rather than pulling `elapsed` itself backward —
            # subtracting from elapsed would be a no-op (clamped at 0)
            # whenever extend is called early in a round's life.
            # Adding to the deadline instead always buys exactly
            # `clamped` more seconds, and repeated extends stack.
            question_deadline += clamped

        # Anything else (missing/unknown "cmd", extra junk keys) falls
        # through and is ignored.

    def on_authoring_command(entity, attribute, value, source):
        """The question-bank/chapter editor's own channel — see the
        module docstring's "Authoring" section for why this is a
        SEPARATE attribute from teacher_command (loopback-only via a
        Core-level name-prefix rule, since these mutations — and the
        authoring_question_bank/authoring_chapters state they publish
        afterward — carry real answers). Every cmd is shape/bound-
        checked the same "silently drop on any mismatch" way as
        on_teacher_command above; nothing here ever raises on bad
        input.

        Question commands (operate on class_question_bank, a flat list
        — see the module docstring's "Question shape"):
          "add_question" — {"tier", + validate_question_content's own
            fields (type/text/hint/points/options|accepted_answers|
            correct_indices|pairs, whichever "type" actually uses)}.
            Appends a freshly-id'd question, capped at
            MAX_QUESTIONS_PER_CLASS.
          "edit_question" — {"id", "tier", + content fields} replaces
            an existing question's fields in place (same id, same
            position in the flat list) — including "tier", so a
            teacher CAN reclassify a question's difficulty; refused if
            that would leave the question's OLD tier with zero
            questions (see questions_for_tier) — delete/recreate a
            tier's last question if that's really the intent, don't
            silently leave live rotation with nothing to draw from.
          "delete_question" — {"id"} removes it from the bank AND from
            every chapter's question_ids — same "refuse if it's the
            last one in its tier" guard as edit_question above.
          "reorder_questions" — {"tier", "ids"} — `ids` must be EXACTLY
            that tier's current question ids, just reordered (no
            add/remove through this cmd); rotation order (next_question's
            round_index % len(...)) follows this order from then on.
          "generate_questions" — {"tier", "count"} appends `count`
            (capped at MAX_GENERATE_PER_REQUEST, and further capped by
            whatever room is left under MAX_QUESTIONS_PER_CLASS) freshly
            randomized templated questions for that tier — see
            generate_templated_question. Simple randomized arithmetic
            from a fixed pattern, deliberately NOT real AI generation
            (no calls, no cost) — that's its own later chapter.
          "import_questions" — {"questions": [content, ...],
            "chapter_id"?, "new_chapter_name"?} bulk-adds many questions
            in ONE command — for an importer (e.g. a future LaMa
            connector) feeding in more than one question at a time,
            which nothing above this could do. Each item needs its own
            "tier" plus validate_question_content's own fields; an
            invalid item is skipped, not a reason to reject the whole
            batch — a real import's whole point is tolerating a few
            malformed entries in an otherwise-good batch, unlike every
            other cmd here which drops the WHOLE command on any
            mismatch. Capped at MAX_IMPORT_PER_REQUEST items and by
            whatever room is left under MAX_QUESTIONS_PER_CLASS.
            Optionally files every successfully-imported question into
            ONE chapter — an existing one ("chapter_id") or a freshly
            created one ("new_chapter_name", tried first if both are
            given); a requested new chapter is never actually created if
            nothing ends up successfully imported.

        Chapter commands (operate on class_chapters):
          "add_chapter" — {"name"}, capped at MAX_CHAPTERS_PER_CLASS.
          "rename_chapter" — {"id", "name"}.
          "delete_chapter" — {"id"} — never touches the questions
            themselves, only this chapter's own membership list. Clears
            class_active_chapter back to None (the whole bank) if it
            named the chapter just deleted — never leaves rotation
            silently filtered against a chapter that no longer exists.
          "reorder_chapters" — {"ids"} — must be exactly the current
            chapter ids, reordered.
          "set_chapter_questions" — {"id", "question_ids"} replaces
            one chapter's ordered membership wholesale — how the UI
            adds, removes, AND reorders a chapter's questions, all in
            one action; every id must already exist in the bank.
          "set_active_chapter" — {"id": str | None} — which chapter (if
            any) next_question() actually draws from; see
            questions_for_tier's own doc comment on the filtering and
            its never-leave-a-tier-empty fallback. None means "the whole
            bank", today's original (and still default) behavior.
            Re-broadcasts the current question immediately under the new
            filter, same as toggling adaptive_mode already does.
        """
        if entity != control or not isinstance(value, dict):
            return
        global class_active_chapter
        ensure_persisted()  # a teacher actively authoring content is real use
        cmd = value.get("cmd")
        question_ids = {q["id"] for q in class_question_bank}
        chapter_ids = {c["id"] for c in class_chapters}

        if cmd == "add_question":
            tier = value.get("tier")
            if tier not in TIERS or len(class_question_bank) >= MAX_QUESTIONS_PER_CLASS:
                return
            content = validate_question_content(value)
            if content is None:
                return
            class_question_bank.append({"id": new_question_id(), "tier": tier, **content})
            save_questions(class_code, class_question_bank)
            publish_authoring_state()

        elif cmd == "edit_question":
            qid = value.get("id")
            new_tier = value.get("tier")
            if not isinstance(qid, str) or qid not in question_ids or new_tier not in TIERS:
                return
            content = validate_question_content(value)
            if content is None:
                return
            index = next(i for i, q in enumerate(class_question_bank) if q["id"] == qid)
            old_tier = class_question_bank[index]["tier"]
            if old_tier != new_tier and len(questions_for_tier(old_tier)) <= 1:
                return  # would leave old_tier with nothing to draw from
            class_question_bank[index] = {"id": qid, "tier": new_tier, **content}
            save_questions(class_code, class_question_bank)
            publish_authoring_state()

        elif cmd == "delete_question":
            qid = value.get("id")
            if not isinstance(qid, str) or qid not in question_ids:
                return
            tier = next(q["tier"] for q in class_question_bank if q["id"] == qid)
            if len(questions_for_tier(tier)) <= 1:
                return  # refuse to leave a tier with zero questions
            class_question_bank[:] = [q for q in class_question_bank if q["id"] != qid]
            for chapter in class_chapters:
                chapter["question_ids"] = [i for i in chapter["question_ids"] if i != qid]
            save_questions(class_code, class_question_bank)
            save_chapters(class_code, class_chapters, class_active_chapter)
            publish_authoring_state()

        elif cmd == "reorder_questions":
            tier = value.get("tier")
            ids = value.get("ids")
            if tier not in TIERS or not isinstance(ids, list):
                return
            current_ids = [q["id"] for q in questions_for_tier(tier)]
            if set(ids) != set(current_ids) or len(ids) != len(current_ids):
                return  # must be a pure reorder -- no add/remove through this cmd
            by_id = {q["id"]: q for q in class_question_bank if q["tier"] == tier}
            reordered = [by_id[i] for i in ids]
            class_question_bank[:] = [q for q in class_question_bank if q["tier"] != tier] + reordered
            save_questions(class_code, class_question_bank)
            publish_authoring_state()

        elif cmd == "generate_questions":
            tier = value.get("tier")
            count = value.get("count")
            if tier not in TIERS or not isinstance(count, int) or isinstance(count, bool) or count < 1:
                return
            room = MAX_QUESTIONS_PER_CLASS - len(class_question_bank)
            n = min(count, MAX_GENERATE_PER_REQUEST, room)
            if n <= 0:
                return
            for _ in range(n):
                class_question_bank.append(generate_templated_question(tier))
            save_questions(class_code, class_question_bank)
            publish_authoring_state()

        elif cmd == "import_questions":
            items = value.get("questions")
            if not isinstance(items, list) or not items:
                return
            room = MAX_QUESTIONS_PER_CLASS - len(class_question_bank)
            if room <= 0:
                return

            imported: list[dict] = []
            for item in items[:MAX_IMPORT_PER_REQUEST]:
                if len(imported) >= room:
                    break
                if not isinstance(item, dict):
                    continue
                tier = item.get("tier", DEFAULT_TIER)
                if tier not in TIERS:
                    continue
                content = validate_question_content(item)
                if content is None:
                    continue
                imported.append({"id": new_question_id(), "tier": tier, **content})

            if not imported:
                return  # nothing in the batch was actually usable
            class_question_bank.extend(imported)
            save_questions(class_code, class_question_bank)

            target_chapter = None
            new_chapter_name = value.get("new_chapter_name")
            if (
                isinstance(new_chapter_name, str)
                and new_chapter_name.strip()
                and len(new_chapter_name) <= MAX_CHAPTER_NAME_LENGTH
                and len(class_chapters) < MAX_CHAPTERS_PER_CLASS
            ):
                target_chapter = {"id": new_chapter_id(), "name": new_chapter_name.strip(), "question_ids": []}
                class_chapters.append(target_chapter)
            else:
                existing_chapter_id = value.get("chapter_id")
                if isinstance(existing_chapter_id, str) and existing_chapter_id in chapter_ids:
                    target_chapter = next(c for c in class_chapters if c["id"] == existing_chapter_id)
            if target_chapter is not None:
                target_chapter["question_ids"].extend(q["id"] for q in imported)
                save_chapters(class_code, class_chapters, class_active_chapter)

            publish_authoring_state()

        elif cmd == "add_chapter":
            chapter_name = value.get("name")
            if not isinstance(chapter_name, str) or not chapter_name.strip() or len(chapter_name) > MAX_CHAPTER_NAME_LENGTH:
                return
            if len(class_chapters) >= MAX_CHAPTERS_PER_CLASS:
                return
            class_chapters.append({"id": new_chapter_id(), "name": chapter_name, "question_ids": []})
            save_chapters(class_code, class_chapters, class_active_chapter)
            publish_authoring_state()

        elif cmd == "rename_chapter":
            chapter_id = value.get("id")
            chapter_name = value.get("name")
            if not isinstance(chapter_id, str) or chapter_id not in chapter_ids:
                return
            if not isinstance(chapter_name, str) or not chapter_name.strip() or len(chapter_name) > MAX_CHAPTER_NAME_LENGTH:
                return
            for chapter in class_chapters:
                if chapter["id"] == chapter_id:
                    chapter["name"] = chapter_name
            save_chapters(class_code, class_chapters, class_active_chapter)
            publish_authoring_state()

        elif cmd == "delete_chapter":
            chapter_id = value.get("id")
            if not isinstance(chapter_id, str) or chapter_id not in chapter_ids:
                return
            class_chapters[:] = [c for c in class_chapters if c["id"] != chapter_id]
            # Never leave rotation silently filtered against a chapter
            # that no longer exists — see questions_for_tier's own
            # None-means-whole-bank fallback.
            if class_active_chapter == chapter_id:
                class_active_chapter = None
            save_chapters(class_code, class_chapters, class_active_chapter)
            publish_authoring_state()

        elif cmd == "reorder_chapters":
            ids = value.get("ids")
            if not isinstance(ids, list) or set(ids) != chapter_ids or len(ids) != len(class_chapters):
                return
            by_id = {c["id"]: c for c in class_chapters}
            class_chapters[:] = [by_id[i] for i in ids]
            save_chapters(class_code, class_chapters, class_active_chapter)
            publish_authoring_state()

        elif cmd == "set_chapter_questions":
            chapter_id = value.get("id")
            new_question_ids = value.get("question_ids")
            if not isinstance(chapter_id, str) or chapter_id not in chapter_ids:
                return
            if not isinstance(new_question_ids, list) or not all(isinstance(i, str) and i in question_ids for i in new_question_ids):
                return
            for chapter in class_chapters:
                if chapter["id"] == chapter_id:
                    chapter["question_ids"] = list(new_question_ids)
            save_chapters(class_code, class_chapters, class_active_chapter)
            publish_authoring_state()

        elif cmd == "set_active_chapter":
            chapter_id = value.get("id")
            if chapter_id is not None and (not isinstance(chapter_id, str) or chapter_id not in chapter_ids):
                return
            if chapter_id == class_active_chapter:
                return
            class_active_chapter = chapter_id
            save_chapters(class_code, class_chapters, class_active_chapter)
            publish_authoring_state()
            next_question()  # reflect the new filter right away, same as on_adaptive_mode_external

        # Anything else (missing/unknown "cmd", extra junk keys) falls
        # through and is ignored, same as on_teacher_command above.

    def subscribe_class_scoped():
        """Every class_attr()-scoped subscription this plugin needs —
        factored out so on_load_class can re-run it under a RESUMED
        class's code, not just once at startup. sotrice_client's World
        has no unsubscribe, so re-running this after class_code changes
        ADDS subscriptions under the new namespaced names without ever
        removing the old (now-permanently-dead, since nothing will ever
        publish under a discarded temporary code again) ones — see the
        module docstring's "Resuming" section."""
        world.subscribe(class_attr("answer"), on_answer, replay=True)
        world.subscribe(class_attr("adaptive_mode"), on_adaptive_mode_external, replay=False)
        world.subscribe(class_attr("student_tier"), on_student_tier_external, replay=False)
        world.subscribe(class_attr("scoreboard_enabled"), on_scoreboard_enabled_external, replay=False)
        world.subscribe(class_attr("leave_locked"), on_leave_locked_external, replay=False)
        # replay=False — a command is a one-shot action ("advance now",
        # "here's an edit"), not persistent state to replay to a
        # late-joining subscriber the way "answer" and question_* are.
        world.subscribe(class_attr("teacher_command"), on_teacher_command, replay=False)
        # Loopback-only in practice (see the module docstring's
        # "Authoring" section) — server/src/main.rs's PermissionGate refuses
        # this name for any non-loopback connection regardless of
        # class-code knowledge, same as the authoring_* attributes this
        # handler publishes. replay=False for the same reason
        # teacher_command is: a mutation, not persistent state.
        world.subscribe(class_attr("authoring_command"), on_authoring_command, replay=False)

    def on_load_class(entity, attribute, value, source):
        """See the module docstring's "Resuming" section. `value` is a
        previously-persisted class's code; entity-targeted at THIS
        instance's own control entity, same idiom as teacher_command."""
        global class_code, class_name, class_question_bank, class_chapters, class_active_chapter, class_persisted
        global adaptive_mode, scoreboard_enabled, leave_locked, elapsed
        if entity != control or not isinstance(value, str):
            return
        code = value.strip().upper()
        if len(code) != CLASS_CODE_LENGTH or any(ch not in CLASS_CODE_ALPHABET for ch in code):
            return
        bundle = load_class_bundle(code)
        if bundle is None:
            print(f"[{name}] load_class: no persisted class found for code {code!r}, ignoring", flush=True)
            return

        # A resumed session starts with an empty LIVE roster/scores —
        # roster.json's history is untouched and simply gains new
        # entries as students reconnect and answer again (see the
        # module docstring).
        known_students.clear()
        student_tier_by_id.clear()
        student_stats.clear()
        recent_correct_by_id.clear()
        answers_by_student.clear()

        class_code = code
        class_name = bundle["class_name"]
        class_question_bank = bundle["questions"]
        class_chapters = bundle["chapters"]
        class_active_chapter = bundle["active_chapter"]
        class_persisted = True  # already on disk — never re-seed/re-append-to-index for this code
        adaptive_mode = "off"
        scoreboard_enabled = False
        leave_locked = False

        world.set_many([
            (control, "class_code", class_code),
            (control, "class_name", class_name),
        ])
        world.set_attribute(control, class_attr("scoreboard"), [])
        subscribe_class_scoped()
        publish_known_classes()
        publish_authoring_state()
        print(f"[{name}] resumed class '{class_name}' (code {class_code}) from disk", flush=True)
        next_question()
        elapsed = 0.0

    def on_delete_class(entity, attribute, value, source):
        """Removes a class the picker lists but nobody's currently in —
        the other side of load_class/resume (see the module docstring's
        "Persistence" section). Bare/unnamespaced and entity-targeted
        the same way, and deliberately refuses to delete THIS instance's
        own active code: the frontend only offers the delete action on
        a non-running row in the first place (see ClassPicker), but a
        stale/racing message shouldn't be able to nuke a live session's
        own persisted data out from under it."""
        if entity != control or not isinstance(value, str):
            return
        code = value.strip().upper()
        if len(code) != CLASS_CODE_LENGTH or any(ch not in CLASS_CODE_ALPHABET for ch in code):
            return
        if code == class_code:
            print(f"[{name}] delete_class: refusing to delete {code!r}, it's this instance's own active class", flush=True)
            return
        remove_from_index(code)
        shutil.rmtree(class_dir_for(code), ignore_errors=True)
        publish_known_classes()
        print(f"[{name}] deleted class {code!r}", flush=True)

    # elapsed must exist before any subscription is live: a push can be
    # dispatched from the background dispatch thread the instant
    # subscribe() returns, and on_teacher_command's `global elapsed`
    # would otherwise reference a name that doesn't exist yet.
    elapsed = 0.0

    world.set_attribute(control, class_attr("scoreboard"), [])
    publish_known_classes()
    publish_authoring_state()

    subscribe_class_scoped()
    # Bare/unnamespaced — not part of any one class's namespace (see
    # the module docstring on why load_class can't be class_attr()
    # scoped, and why student_name is deliberately global too).
    world.subscribe("student_name", on_student_name, replay=True)
    world.subscribe("load_class", on_load_class, replay=False)
    world.subscribe("delete_class", on_delete_class, replay=False)

    next_question()
    try:
        while True:
            time.sleep(TICK_SECONDS)
            elapsed += TICK_SECONDS
            if elapsed >= question_deadline:
                elapsed = 0.0
                next_question()
    except KeyboardInterrupt:
        print(f"[{name}] stopped")


# SECURITY NOTE — updated once real cross-device networking landed
# (server/src/main.rs's PermissionGate); read this version, not an old copy:
#
# Every input this file actually receives from the outside — "answer",
# "adaptive_mode", "student_tier", "scoreboard_enabled", "leave_locked",
# "teacher_command", "authoring_command" (all class_attr()-scoped), and
# "student_name"/"load_class" (bare) — is validated and bounded above:
# shape-checked,
# range-checked against the actual active question for a given tier,
# capped at MAX_TRACKED_STUDENTS via ensure_student (the single gate
# every per-student dict is populated through), capped at
# MAX_QUESTIONS_PER_CLASS/MAX_CHAPTERS_PER_CLASS for authoring_command,
# constrained to the fixed ADAPTIVE_MODES/TIERS/QUESTION_TYPES enums
# rather than accepting arbitrary strings, and length/type/range-checked
# for teacher_command/authoring_command specifically before ever
# touching current_bank_by_tier, class_question_bank/class_chapters, or
# elapsed. That covers what a malformed or hostile payload could do to
# THIS plugin's own logic, independent of who's allowed to send it at
# all (below). load_class is likewise shape- and alphabet/length-
# checked before ever touching disk, and an unknown code is silently
# ignored rather than treated as "create a new class with this code" —
# a code only ever becomes real through ensure_persisted().
#
# WHO can reach this plugin at all is now a real, Core-level (Rust)
# concern, not something this file has to assume the worst about on its
# own: server/src/main.rs's PermissionGate distinguishes a loopback connection
# (this machine's own UI, or a locally-spawned plugin process — always
# fully trusted) from a non-loopback one (a real joined student, over
# real WiFi) and refuses that second kind class-scoped ATTRIBUTE NAMES
# starting with "authoring_" outright, regardless of whether it already
# knows the class code — see the module docstring's "Authoring" section.
# That's why authoring_question_bank/authoring_chapters can safely
# publish real answers, and authoring_command can safely accept bank-
# rewriting commands, while every OTHER class_attr()-scoped name
# (teacher_command included) stays reachable by anyone who knows the
# class code, same trust level the class_code namespacing itself always
# had: it separates concurrent classes' DATA, it does not gate who can
# read or write within one. A joined student sending teacher_command
# (skip the question, edit what's on screen) is therefore still
# possible today — a real, known, NOT-yet-closed gap, deliberately left
# for a later stage's "class-mode controls" to address holistically
# rather than patched narrowly here. The on-disk persistence has the
# same shape: anything that can reach %APPDATA%\Sotrice\classes\ (or
# read a class_code off a projector) can read or resume that class —
# no new exposure beyond what class_code sharing already implied.
