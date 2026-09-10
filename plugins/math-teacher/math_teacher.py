"""Math Teacher — first slice of the math/education spin: proves the
live teacher-broadcasts-question, many-students-answer-live loop
through the exact same Attribute-relay pattern every other plugin
uses, before any of the harder pieces (handwriting, whiteboard,
adaptive difficulty, accounts, class-mode) get built on top of it.

Deliberately minimal: multiple-choice only, one small hardcoded
question bank cycled automatically (there's no real teacher UI to
author/advance questions yet), no grading/correctness tracking yet —
just proving "one broadcaster, many live respondents, a live tally"
actually works end to end, the same way city-weather's clock proved
"one broadcaster, many reactors" before anything more sophisticated
was built on top of it.

Entities published:
  A single control entity, kind="math_teacher_control":
    question_text, question_options (list[str]), question_index (int,
      bumps every new question) — what's being asked right now.
    answer_tally (list[int], same length as question_options) — live
      count of how many students currently have each option selected
      for the CURRENT question_index. Recomputed on every relevant
      answer, reset to zeros the moment the question changes.

Reads (subscribing to a name, not a specific entity — any number of
math_student instances can exist):
    answer — {"question_index": int, "option": int} written by each
    student. Only counted toward the tally if its question_index
    matches the currently active question; a stale answer to a
    question that's since moved on is silently ignored rather than
    corrupting the current tally.
"""

import time

from sotrice_client import World

QUESTION_BANK = [
    ("What is 7 + 5?", ["10", "11", "12", "13"]),
    ("What is 9 x 6?", ["45", "54", "56", "63"]),
    ("Solve for x: 2x = 14", ["5", "6", "7", "8"]),
    ("What is 15% of 200?", ["20", "25", "30", "35"]),
    ("What is the square root of 81?", ["7", "8", "9", "10"]),
]

SECONDS_PER_QUESTION = 20.0
TICK_SECONDS = 0.5

# Bounds how much a flood of bogus/malicious "answer" writes can grow
# this dict by — without it, anything that can reach the server (see
# this file's bottom note on the real trust boundary) could create
# unlimited entities each posting one answer and grow this without
# limit. A real class is dozens of students, not hundreds, so this
# costs nothing for legitimate use.
MAX_TRACKED_STUDENTS = 200


with World() as world:
    name = world.identify("math-teacher")
    print(f"[{name}] math class started, {len(QUESTION_BANK)} questions in the bank", flush=True)

    control = world.create_entity()
    question_index = -1
    answers_by_student: dict[int, int] = {}

    def publish_tally():
        options = QUESTION_BANK[question_index % len(QUESTION_BANK)][1]
        counts = [0] * len(options)
        for option in answers_by_student.values():
            if 0 <= option < len(counts):
                counts[option] += 1
        world.set_attribute(control, "answer_tally", counts)

    def next_question():
        global question_index
        question_index += 1
        answers_by_student.clear()
        text, options = QUESTION_BANK[question_index % len(QUESTION_BANK)]
        world.set_many([
            (control, "question_text", text),
            (control, "question_options", options),
            (control, "question_index", question_index),
        ])
        publish_tally()

    def on_answer(entity, attribute, value, source):
        # Trust nothing about this payload's shape or contents — it
        # comes from whatever connected and called itself a student
        # (see this file's bottom note on why that's not actually
        # verified yet). Late-joining students also replay every
        # answer ever set, including ones for questions we've since
        # moved past — dropping those here (rather than in
        # publish_tally) keeps answers_by_student holding only what's
        # actually relevant right now.
        if not isinstance(value, dict) or value.get("question_index") != question_index:
            return
        option = value.get("option")
        if not isinstance(option, int):
            return
        if entity not in answers_by_student and len(answers_by_student) >= MAX_TRACKED_STUDENTS:
            return  # already at the cap and this isn't an update to an existing one — drop it
        answers_by_student[entity] = option
        publish_tally()

    world.subscribe("answer", on_answer, replay=True)

    next_question()
    elapsed = 0.0
    try:
        while True:
            time.sleep(TICK_SECONDS)
            elapsed += TICK_SECONDS
            if elapsed >= SECONDS_PER_QUESTION:
                elapsed = 0.0
                next_question()
    except KeyboardInterrupt:
        print(f"[{name}] stopped")


# SECURITY NOTE — read before this ever leaves localhost:
#
# Every input this file actually receives from the outside (the
# "answer" attribute) is validated and bounded above: shape-checked,
# range-checked in publish_tally, capped at MAX_TRACKED_STUDENTS. That
# covers what a malformed or hostile payload could do to THIS plugin's
# own logic.
#
# It does NOT cover the real question: right now, sotrice-server has
# no concept of identity or permission at all — any process that can
# reach its port can call identify("math-student") (or any other
# type), or just start writing attributes directly with no identify()
# call at all, including question_text/question_options/answer_tally
# themselves (nothing stops a connected client from impersonating the
# teacher). That's an accepted, deliberate tradeoff for a single-user
# local simulation sandbox — every existing plugin already relies on
# "any plugin can read/write any attribute" as a FEATURE, not a bug.
#
# It stops being acceptable the moment a real student's own device
# connects over a real network instead of a locally co-located trusted
# process — which is the explicit plan for this spin (see the
# class-mode/accounts design). Authentication and per-connection
# authorization need to be designed into the Core/server before that
# happens, not bolted onto this file — this file has nothing to add on
# top of a properly authenticated connection, and nothing it does can
# substitute for one.
