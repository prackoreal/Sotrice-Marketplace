"""Math Student — the other half of the math class (see
math_teacher.py). Each running instance is one simulated student:
watches its assigned tier's current question and submits an answer
after a short random "thinking" delay, standing in for a real student
clicking an option until there's a real UI for it — swapping that
random pick for an actual click later doesn't change the protocol at
all, only where the option index comes from.

Class identity: this student doesn't do anything until it knows which
class's code to use for every class_attr()-scoped name (see
math_teacher.py's own docstring on class_attr) — otherwise it would
have no way to tell two concurrently-running classes' questions apart.
Two ways to learn it, in order:
  1. sys.argv[1], if given — the convenient path for manually running
     this file directly during testing (`python math_student.py
     ABC123`), which never goes through the Toolbox/StartPlugin at all.
  2. Otherwise, waits for an external `join_code` write on its OWN
     entity — the same post-connect-control-attribute idiom every
     other cross-plugin config in this codebase uses, for whenever
     this IS started via the Toolbox (which has no way to pass a
     launch-time argument) and something else (a join-flow UI) sets
     the code right after.
This file is a simulated bot standing in for testing/demo load, not
the real human-facing student experience — that's the actual Simulation
View UI, which collects a class code from a person directly and never
needs this launch-time-vs-post-connect distinction at all.

Entities published:
  Its own entity, kind="math_student":
    student_name — a short auto-generated label, purely for display
      once a real UI shows a roster; not read by math_teacher.py.
    answer — {"question_index": int, "option": int, "tier": str},
      rewritten every time this student answers a new question. "tier"
      is whichever tier's question this student was actually looking
      at (see on_student_tier below) — always sent, so an old teacher
      that ignores unknown fields is unaffected, and a tiering-aware
      teacher can grade against the right question. Published under
      class_attr("answer") once a class code is known.

Reads (subscribing to class_attr()-scoped names, not a specific entity —
there's exactly one control entity per class in practice, but this
never assumes that; the FIRST value seen for each name is what's used,
matching how every other plugin here treats a "there should only be
one" attribute):
    question_by_tier — {tier: {"text", "options"}}. Preferred source of
      the current question once present; falls back to the plain
      question_options below if it never arrives (an older teacher).
    question_options, question_index — the original shape, still read
      as the fallback question content (mirrors DEFAULT_TIER) and as
      the round counter either way.
    student_tier — {<student_id str>: tier}, published by the teacher.
      This student only ever looks up its OWN entry (keyed by str(me))
      to decide which tier's question is "mine" this round. Empty (or
      missing this student's entry) means "no tiering" — falls back to
      the plain question_options/question_index shape, exactly the
      original first-slice behavior.

    student_streak — {<student_id str>: int}, student_score
      ({<student_id str>: {"correct", "total"}}) — same idea: this
      student only ever looks up its own entry. The teacher never
      tells it whether an answer was right or wrong beyond these
      counters. Just stored/printed here to prove the data flows; no
      UI binds to it yet.

    scoreboard_enabled, scoreboard — read and stashed locally so a
      future UI has something to bind to, printed when
      scoreboard_enabled is true and the board changes. The teacher
      never populates `scoreboard` at all while it's disabled, so
      there's nothing to accidentally see even if this file didn't
      bother checking the flag itself — but it checks anyway, since
      "don't show it when it's supposed to be hidden" shouldn't depend
      solely on the other end of the wire behaving.
"""

import random
import string
import sys
import time

from sotrice_client import World

MIN_THINK_SECONDS = 2.0
MAX_THINK_SECONDS = 8.0
DEFAULT_TIER = "medium"


def random_name() -> str:
    return "Student-" + "".join(random.choices(string.ascii_uppercase, k=3))


with World() as world:
    name = world.identify("math-student")

    me = world.create_entity()
    world.set_many([
        (me, "kind", "math_student"),
        (me, "student_name", random_name()),
    ])

    class_code: str | None = sys.argv[1] if len(sys.argv) > 1 else None

    def class_attr(attr_name: str) -> str:
        assert class_code is not None
        return f"{attr_name}:{class_code}"

    # Fallback (pre-tiering) question content.
    fallback_options: list = []
    current_index = None
    answered_index = None

    # Tiering (empty/absent means "not tiering" — use the fallback
    # above, reproducing the original first-slice behavior exactly).
    question_by_tier: dict = {}
    my_tier: str | None = None

    my_streak = None
    my_score = None
    scoreboard_enabled = False
    current_scoreboard: list = []

    def on_question_options(entity, attribute, value, source):
        global fallback_options
        if isinstance(value, list):
            fallback_options = value

    def on_question_index(entity, attribute, value, source):
        global current_index
        if isinstance(value, int):
            current_index = value

    def on_question_by_tier(entity, attribute, value, source):
        global question_by_tier
        if isinstance(value, dict):
            question_by_tier = value

    def on_student_tier(entity, attribute, value, source):
        global my_tier
        if not isinstance(value, dict):
            return
        tier = value.get(str(me))
        my_tier = tier if isinstance(tier, str) else None

    def on_student_streak(entity, attribute, value, source):
        # Only this student's own entry is ever looked at — the
        # teacher doesn't hide other students' streaks in this
        # attribute (that's what scoreboard_enabled/scoreboard gate),
        # but this file has no reason to read anyone else's.
        global my_streak
        if not isinstance(value, dict):
            return
        streak = value.get(str(me))
        if isinstance(streak, int) and streak != my_streak:
            my_streak = streak
            print(f"[{name}] current streak: {my_streak}", flush=True)

    def on_student_score(entity, attribute, value, source):
        global my_score
        if not isinstance(value, dict):
            return
        score = value.get(str(me))
        if isinstance(score, dict) and score != my_score:
            my_score = score

    def on_scoreboard_enabled(entity, attribute, value, source):
        global scoreboard_enabled
        if isinstance(value, bool):
            scoreboard_enabled = value

    def on_scoreboard(entity, attribute, value, source):
        # The teacher only ever populates this while scoreboard_enabled
        # is true (and clears it to [] the moment it's turned off), but
        # this still gates its own printing on the locally-tracked flag
        # too — belt and suspenders against printing a stale board this
        # student happened to read a split second before the teacher's
        # "turn it off" write lands.
        global current_scoreboard
        if not isinstance(value, list):
            return
        current_scoreboard = value
        if scoreboard_enabled:
            print(f"[{name}] scoreboard: {current_scoreboard}", flush=True)

    def subscribe_to_class():
        world.subscribe(class_attr("question_options"), on_question_options, replay=True)
        world.subscribe(class_attr("question_index"), on_question_index, replay=True)
        world.subscribe(class_attr("question_by_tier"), on_question_by_tier, replay=True)
        world.subscribe(class_attr("student_tier"), on_student_tier, replay=True)
        world.subscribe(class_attr("student_streak"), on_student_streak, replay=True)
        world.subscribe(class_attr("student_score"), on_student_score, replay=True)
        world.subscribe(class_attr("scoreboard_enabled"), on_scoreboard_enabled, replay=True)
        world.subscribe(class_attr("scoreboard"), on_scoreboard, replay=True)

    if class_code is not None:
        print(f"[{name}] joining class {class_code} (given on the command line)", flush=True)
        subscribe_to_class()
    else:
        def on_join_code(entity, attribute, value, source):
            global class_code
            if entity != me or class_code is not None or not isinstance(value, str) or not value:
                return
            class_code = value
            print(f"[{name}] joining class {class_code}", flush=True)
            subscribe_to_class()

        print(f"[{name}] waiting for a join_code...", flush=True)
        world.subscribe("join_code", on_join_code, replay=True)

    def current_question_options() -> list:
        if my_tier and my_tier in question_by_tier:
            options = question_by_tier[my_tier].get("options")
            if isinstance(options, list):
                return options
        return fallback_options

    try:
        while True:
            time.sleep(0.5)
            if class_code is None:
                continue
            options = current_question_options()
            if current_index is not None and current_index != answered_index and options:
                # Snapshot before the think-delay: question_index/
                # options/my_tier can all advance to the NEXT question
                # while this student is "thinking" about the current
                # one, and the answer submitted below should reflect
                # what they were actually looking at, not whatever's
                # current by the time they finish.
                answering_index = current_index
                answering_options = options
                answering_tier = my_tier if my_tier else DEFAULT_TIER
                time.sleep(random.uniform(MIN_THINK_SECONDS, MAX_THINK_SECONDS))
                option = random.randrange(len(answering_options))
                world.set_attribute(
                    me,
                    class_attr("answer"),
                    {"question_index": answering_index, "option": option, "tier": answering_tier},
                )
                answered_index = answering_index
                print(
                    f"[{name}] answered question {answering_index} with option {option} (tier={answering_tier})",
                    flush=True,
                )
    except KeyboardInterrupt:
        print(f"[{name}] left the class")
