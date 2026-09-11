"""Math Teacher — the teacher side of the math/education spin's live
class session: broadcasts question(s), tallies live answers, grades
them, supports adaptive per-student difficulty, a streak/scoreboard
layer, teacher live-editing, and now a per-class identity so more than
one class can run on the same machine at once without their questions/
answers/rosters bleeding into each other. Still the exact same
Attribute-relay pattern every other plugin uses; still deliberately
short of the harder pieces (handwriting, whiteboard, accounts,
cross-device networking) that get built later.

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

Backward compatible IN SPIRIT only, not by exact attribute name: with
adaptive_mode == "off" and no teacher_command ever sent, the shape of
everything published is unchanged from the original single-question
first slice (one shared "medium" question, one shared tally,
question_source always "builtin") — but every consumer now has to read
these under their class_attr(name) form, not the bare name.

Entities published:
  A single control entity, kind="math_class", class_code (str, bare/
  unnamespaced), class_name (str, bare/unnamespaced):
    All namespaced via class_attr() below:
    question_text, question_options (list[str]), question_index (int,
      bumps every new round), question_source ("builtin" |
      "teacher_edited") — all mirror whichever tier is DEFAULT_TIER
      ("medium"), or the sole active tier if medium isn't active for
      some reason. Original shape, unchanged meaning.
    answer_tally (list[int]) — mirrors the DEFAULT_TIER tally.
    question_by_tier ({tier: {"text", "options"}}) — one entry per
      currently active tier (just ["medium"] when adaptive_mode is
      "off", all three otherwise). Correct-answer index is never
      published here or anywhere else a student can read.
    answer_tally_by_tier ({tier: list[int]}).
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
    student_score ({str(student_id): {"correct": int, "total": int}}),
    student_streak ({str(student_id): int}) — running grading state
      per student, independent of adaptive_mode (grading happens
      whenever we know a question's correct answer, tiering or not).
      Streak counts consecutive correct answers, reset to 0 on a wrong
      one. Neither ever reveals what the correct answer WAS.
    scoreboard_enabled (bool, default False) — teacher-facing toggle,
      no UI yet. scoreboard ([{"student", "correct", "total",
      "streak"}, ...], sorted by correct descending) — only kept up to
      date while enabled; actively cleared to [] the instant it's
      turned off, never populated while off.

Reads (subscribing to a class_attr()-scoped name, not a specific
entity — any number of math_student instances for THIS class can
exist):
    answer — {"question_index": int, "option": int, "tier": str
    (optional)}. Missing/unknown/inactive "tier" is treated as
    DEFAULT_TIER, which is exactly right for a student that predates
    tiering, or whenever adaptive_mode is "off" (the only active tier
    then anyway). Only counted if question_index matches the current
    round AND tier is currently active; each student is graded at most
    once per round no matter how many times they resubmit.

    adaptive_mode, student_tier, scoreboard_enabled — externally
    settable; a teacher (or future teacher UI) writing these directly
    is adopted immediately. student_tier writes only ever touch a
    student this plugin already knows about, and only to a real tier.

    teacher_command — {"cmd": "next" | "replace" | "extend", ...}:
      "next" — advance to the next round immediately, timer resets.
      "replace" — {"cmd": "replace", "text": str, "options": [str,
        ...], "tier": str (optional, required when more than one tier
        is active), "correct_index": int (optional)} edits the
        CURRENT round's question for one tier in place (question_index
        does not change) and clears that tier's tally. Omitting
        "correct_index" makes that tier's current question ungraded
        for this round (tallied normally, but no correct/streak/score
        impact) — there is no way to guess a teacher-supplied
        question's right answer, so this plugin doesn't pretend to.
      "extend" — {"cmd": "extend", "seconds": number} pushes out the
        round's auto-advance deadline by `seconds`, clamped to
        EXTEND_SECONDS_MIN..MAX.
    Anything that doesn't match one of these shapes, or names a tier
    that isn't currently active, is silently dropped.
"""

import math
import random
import time

from sotrice_client import World

# Excludes 0/O/1/I/L -- a code a human has to read off a screen (or a
# projector) and type back in shouldn't depend on telling those apart
# in whatever font it's rendered in.
CLASS_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CLASS_CODE_LENGTH = 6


def generate_class_code() -> str:
    return "".join(random.choice(CLASS_CODE_ALPHABET) for _ in range(CLASS_CODE_LENGTH))


# Each tier is a list of (text, options, correct_option_index). The
# correct index is never published on any attribute a student can
# read (question_text/question_options/question_by_tier/answer_tally*
# all omit it) — only used internally to grade an "answer" against.
QUESTION_BANK_BY_TIER: dict[str, list[tuple[str, list[str], int]]] = {
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
# boundary.
ROLLING_WINDOW = 3

# Bounds for validating a "replace" teacher_command's free-text fields
# — generous enough for any real question/option a teacher would type,
# tight enough that a hostile payload can't make this plugin publish
# or hold in memory something absurd.
MAX_TEXT_LENGTH = 500
MAX_OPTIONS = 10
MAX_OPTION_LENGTH = 200

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
# this cap is enforced.
MAX_TRACKED_STUDENTS = 200


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

    print(
        f"[{name}] math class '{class_name}' started (code {class_code}), "
        f"{sum(len(v) for v in QUESTION_BANK_BY_TIER.values())} questions across {len(TIERS)} tiers",
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
    round_index = -1
    question_deadline = SECONDS_PER_QUESTION

    # Populated by next_question(); safe non-empty defaults here so an
    # "answer" replay racing ahead of the first next_question() call
    # (pushes dispatch on their own thread) never indexes into an
    # empty dict. Each tier's value is
    # {"text", "options", "correct_index" (None if ungraded), "source"}.
    current_active_tiers: list[str] = [DEFAULT_TIER]
    current_bank_by_tier: dict[str, dict] = {
        DEFAULT_TIER: {
            "text": QUESTION_BANK_BY_TIER[DEFAULT_TIER][0][0],
            "options": QUESTION_BANK_BY_TIER[DEFAULT_TIER][0][1],
            "correct_index": QUESTION_BANK_BY_TIER[DEFAULT_TIER][0][2],
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
        return True

    def shift_tier(entity: int, direction: int):
        current = student_tier_by_id.get(entity, DEFAULT_TIER)
        idx = TIERS.index(current) if current in TIERS else TIERS.index(DEFAULT_TIER)
        student_tier_by_id[entity] = TIERS[max(0, min(len(TIERS) - 1, idx + direction))]

    def grade_answer(entity: int, tier: str, option: int):
        """Grades `entity`'s answer for the CURRENT round against
        `tier`'s correct index, at most once per round no matter how
        many times they resubmit — a resubmission still updates the
        live tally (see on_answer) but must not be graded twice, or
        streak/score would drift from "one answer, one grade" into
        something repeated resubmits could game. A tier whose current
        question has no known correct answer (a teacher "replace"
        without correct_index) is simply not gradeable this round —
        tallied, never scored.
        """
        correct_index = current_bank_by_tier[tier]["correct_index"]
        if correct_index is None:
            return
        stats = student_stats.setdefault(
            entity, {"correct": 0, "total": 0, "streak": 0, "last_graded_round": -1}
        )
        if stats["last_graded_round"] == round_index:
            return
        stats["last_graded_round"] = round_index
        stats["total"] += 1
        is_correct = option == correct_index
        if is_correct:
            stats["correct"] += 1
            stats["streak"] += 1
        else:
            stats["streak"] = 0

        if adaptive_mode != "automatic":
            # "manual" (and "off") leave student_tier alone — see the
            # module docstring on why manual and automatic never fight
            # over the same entry.
            return
        window = recent_correct_by_id.setdefault(entity, [])
        window.append(is_correct)
        del window[:-ROLLING_WINDOW]  # keep only the most recent ROLLING_WINDOW entries
        if len(window) == ROLLING_WINDOW:
            if all(window):
                shift_tier(entity, +1)
                window.clear()
            elif not any(window):
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
        world.set_many([
            (control, class_attr("student_tier"), tier_map),
            (control, class_attr("student_score"), score_map),
            (control, class_attr("student_streak"), streak_map),
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
        tally_by_tier = {
            tier: [0] * len(current_bank_by_tier[tier]["options"]) for tier in current_active_tiers
        }
        for record in answers_by_student.values():
            counts = tally_by_tier.get(record["tier"])
            option = record["option"]
            if counts is not None and 0 <= option < len(counts):
                counts[option] += 1
        mirror_tier = DEFAULT_TIER if DEFAULT_TIER in tally_by_tier else current_active_tiers[0]
        world.set_many([
            (control, class_attr("answer_tally_by_tier"), tally_by_tier),
            (control, class_attr("answer_tally"), tally_by_tier[mirror_tier]),
        ])

    def publish_current_round():
        question_by_tier_public = {
            tier: {"text": bank["text"], "options": bank["options"]}
            for tier, bank in current_bank_by_tier.items()
        }
        mirror_tier = DEFAULT_TIER if DEFAULT_TIER in current_bank_by_tier else current_active_tiers[0]
        mirror = current_bank_by_tier[mirror_tier]
        world.set_many([
            (control, class_attr("question_by_tier"), question_by_tier_public),
            (control, class_attr("question_text"), mirror["text"]),
            (control, class_attr("question_options"), mirror["options"]),
            (control, class_attr("question_source"), mirror["source"]),
            (control, class_attr("question_index"), round_index),
            (control, class_attr("adaptive_mode"), adaptive_mode),
        ])

    def next_question():
        global round_index, current_active_tiers, current_bank_by_tier, question_deadline
        round_index += 1
        answers_by_student.clear()
        question_deadline = SECONDS_PER_QUESTION
        current_active_tiers = list(TIERS) if adaptive_mode != "off" else [DEFAULT_TIER]
        current_bank_by_tier = {}
        for tier in current_active_tiers:
            bank = QUESTION_BANK_BY_TIER[tier]
            text, options, correct_index = bank[round_index % len(bank)]
            current_bank_by_tier[tier] = {
                "text": text,
                "options": options,
                "correct_index": correct_index,
                "source": "builtin",
            }
        publish_current_round()
        publish_student_state()
        publish_tally()

    def replace_current_question(tier, text, options, correct_index):
        # Edits ONE tier's question CURRENTLY up, in place —
        # round_index deliberately does not change, since this is "the
        # teacher fixed a typo/swapped the numbers on what's already
        # shown", not "a new question started". That tier's existing
        # answers are cleared from the tally, since they were cast
        # against options that, from the students' point of view, no
        # longer exist.
        current_bank_by_tier[tier] = {
            "text": text,
            "options": options,
            "correct_index": correct_index,
            "source": "teacher_edited",
        }
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
        option = value.get("option")
        if not isinstance(option, int):
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

        tier_options = current_bank_by_tier[tier]["options"]
        if not (0 <= option < len(tier_options)):
            return

        if not ensure_student(entity):
            return
        answers_by_student[entity] = {"tier": tier, "option": option}
        publish_tally()
        grade_answer(entity, tier, option)
        publish_student_state()

    def on_adaptive_mode_external(entity, attribute, value, source):
        # Ignore our own pushes echoing back — only adopt a mode set
        # by someone else (a teacher UI toggling the control).
        global adaptive_mode
        if source == name or entity != control:
            return
        if value not in ADAPTIVE_MODES or value == adaptive_mode:
            return
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
        scoreboard_enabled = value
        if scoreboard_enabled:
            publish_scoreboard()
        else:
            # Don't just stop updating it — actively clear it, so a
            # student who already read a stale scoreboard before it
            # was turned off can't keep it around by never re-reading.
            world.set_attribute(control, class_attr("scoreboard"), [])

    def on_teacher_command(entity, attribute, value, source):
        # Same untrusted-input posture as on_answer above: stands in
        # for a not-yet-built teacher UI, but until there's auth (see
        # this file's bottom note), anything connected can send this.
        # Shape-check everything; on any mismatch, silently drop
        # rather than raise.
        global elapsed, question_deadline
        if entity != control or not isinstance(value, dict):
            return
        cmd = value.get("cmd")

        if cmd == "next":
            next_question()
            elapsed = 0.0

        elif cmd == "replace":
            text = value.get("text")
            options = value.get("options")
            tier = value.get("tier", DEFAULT_TIER if DEFAULT_TIER in current_active_tiers else current_active_tiers[0])
            correct_index = value.get("correct_index")

            if not isinstance(tier, str) or tier not in current_active_tiers:
                return
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH:
                return
            if not isinstance(options, list) or not (0 < len(options) <= MAX_OPTIONS):
                return
            if not all(
                isinstance(opt, str) and 0 < len(opt) <= MAX_OPTION_LENGTH
                for opt in options
            ):
                return
            if correct_index is not None and (
                isinstance(correct_index, bool)
                or not isinstance(correct_index, int)
                or not (0 <= correct_index < len(options))
            ):
                return  # a nonsensical correct_index is treated as "don't grade this", not silently clamped
            replace_current_question(tier, text, list(options), correct_index)

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

    # elapsed must exist before any subscription is live: a push can be
    # dispatched from the background dispatch thread the instant
    # subscribe() returns, and on_teacher_command's `global elapsed`
    # would otherwise reference a name that doesn't exist yet.
    elapsed = 0.0

    world.set_attribute(control, class_attr("scoreboard"), [])

    world.subscribe(class_attr("answer"), on_answer, replay=True)
    world.subscribe(class_attr("adaptive_mode"), on_adaptive_mode_external, replay=False)
    world.subscribe(class_attr("student_tier"), on_student_tier_external, replay=False)
    world.subscribe(class_attr("scoreboard_enabled"), on_scoreboard_enabled_external, replay=False)
    # replay=False — a command is a one-shot action ("advance now",
    # "here's an edit"), not persistent state to replay to a
    # late-joining subscriber the way "answer" and question_* are.
    world.subscribe(class_attr("teacher_command"), on_teacher_command, replay=False)

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


# SECURITY NOTE — read before this ever leaves localhost:
#
# Every input this file actually receives from the outside — "answer",
# "adaptive_mode", "student_tier", "scoreboard_enabled", and
# "teacher_command" (all class_attr()-scoped) — is validated and
# bounded above: shape-checked, range-checked against the actual active
# question for a given tier, capped at MAX_TRACKED_STUDENTS via
# ensure_student (the single gate every per-student dict is populated
# through), constrained to the fixed ADAPTIVE_MODES/TIERS enums rather
# than accepting arbitrary strings, and length/type/range-checked for
# teacher_command specifically before ever touching current_bank_by_tier
# or elapsed. That covers what a malformed or hostile payload could do
# to THIS plugin's own logic.
#
# The class_code namespacing (see the module docstring) keeps two
# concurrent classes' DATA separate on the same local server, but it is
# NOT an access-control mechanism — knowing (or guessing) a 6-character
# code is all it takes to read or write that class's attributes, same
# trust level as everything else here. That's fine for what this is:
# every existing plugin already relies on "any plugin can read/write
# any attribute" as a FEATURE, not a bug, and sotrice-server has no
# concept of identity or permission at all yet — any process that can
# reach its port can call identify("math-student") (or any other type),
# or just start writing attributes directly with no identify() call.
#
# It stops being acceptable the moment a real student's own device
# connects over a real network instead of a locally co-located trusted
# process. Authentication and per-connection authorization need to be
# designed into the Core/server before that happens, not bolted onto
# this file — this file has nothing to add on top of a properly
# authenticated connection, and nothing it does can substitute for one.
