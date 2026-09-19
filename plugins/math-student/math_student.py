"""Math Student — the other half of the math class (see
math_teacher.py). Each running instance is one simulated student:
watches its assigned tier's current question and submits an answer
after a short random "thinking" delay, standing in for a real student
clicking an option until there's a real UI for it — swapping that
random pick for an actual click later doesn't change the protocol at
all, only where the option index comes from.

Class identity: every attribute math_teacher.py reads or writes is
class_attr()-scoped (`f"{name}:{class_code}"`) except class_code itself
(see math_teacher.py's own docstring) — so this file waits for a
teacher's bare class_code push before it can subscribe to or write
anything else.

Which class: `subscribe(..., replay=True)` fires once for EVERY
Entity that already has class_code set (see sotrice_client.py's own
docstring on replay) — so with several math-teacher instances running
at once, this bot sees ALL of their class codes, not just one. It
collects whatever arrives in a short settle window after the first one
and then picks UNIFORMLY AT RANDOM among them, so several locally-
started bots spread across the currently-running classes instead of
all piling into whichever teacher happened to publish first (the
original behavior here — keeping only the first value seen — meant
every bot started in the same window landed in the same class, since
the server's replay order is effectively fixed within one run). Only
classes already running by the end of that window are candidates; one
started later is invisible to a bot that already picked. Good enough
for local testing, which is this simulated bot's only real job; a real
student picks a SPECIFIC class through the actual join-code UI instead
(see MathStudentView.tsx), which already speaks this same scoped
protocol and isn't affected by any of this.

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
      class_attr("answer") once class_code is known (see above).

Reads (subscribing to class_attr()-scoped names once class_code is
known — see above; the FIRST value seen for each name is what's used,
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
import time

from sotrice_client import World

MIN_THINK_SECONDS = 2.0
MAX_THINK_SECONDS = 8.0
DEFAULT_TIER = "medium"
# How long to keep collecting class_code replay pushes after the first
# one before picking — long enough for a handful of concurrently-running
# teachers' replay pushes to all land (they're ordinary pushes, subject
# to the same dispatch-thread queueing as anything else), short enough
# a single-class run barely notices the delay.
CLASS_DISCOVERY_SECONDS = 0.5


def random_name() -> str:
    return "Student-" + "".join(random.choices(string.ascii_uppercase, k=3))


with World() as world:
    name = world.identify("math-student")
    print(f"[{name}] joined the class", flush=True)

    me = world.create_entity()
    world.set_many([
        (me, "kind", "math_student"),
        (me, "student_name", random_name()),
    ])

    # Waits for a teacher's bare class_code push (see the module
    # docstring) — collects every distinct one seen (replay fires once
    # per currently-running teacher entity, see sotrice_client.py) and,
    # once at least one has shown up, gives stragglers a short window to
    # arrive too before picking uniformly at random among all of them.
    known_class_codes: list[str] = []

    def on_class_code(entity, attribute, value, source):
        if isinstance(value, str) and value not in known_class_codes:
            known_class_codes.append(value)

    world.subscribe("class_code", on_class_code, replay=True)
    while not known_class_codes:
        time.sleep(0.1)
    time.sleep(CLASS_DISCOVERY_SECONDS)
    class_code = random.choice(known_class_codes)

    def class_attr(attr_name: str) -> str:
        """Mirrors math_teacher.py's own class_attr() exactly."""
        return f"{attr_name}:{class_code}"

    print(f"[{name}] found class (code {class_code})", flush=True)

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

    world.subscribe(class_attr("question_options"), on_question_options, replay=True)
    world.subscribe(class_attr("question_index"), on_question_index, replay=True)
    world.subscribe(class_attr("question_by_tier"), on_question_by_tier, replay=True)
    world.subscribe(class_attr("student_tier"), on_student_tier, replay=True)
    world.subscribe(class_attr("student_streak"), on_student_streak, replay=True)
    world.subscribe(class_attr("student_score"), on_student_score, replay=True)
    world.subscribe(class_attr("scoreboard_enabled"), on_scoreboard_enabled, replay=True)
    world.subscribe(class_attr("scoreboard"), on_scoreboard, replay=True)

    def current_question_type() -> str:
        # Defaults to multiple_choice, matching every pre-existing
        # teacher this file already worked against (an older
        # question_by_tier entry, or the plain fallback shape, never
        # carried a "type" at all -- both mean the same thing this
        # always assumed).
        if my_tier and my_tier in question_by_tier:
            type_ = question_by_tier[my_tier].get("type")
            if isinstance(type_, str):
                return type_
        return "multiple_choice"

    def current_question_options() -> list:
        if my_tier and my_tier in question_by_tier:
            options = question_by_tier[my_tier].get("options")
            if isinstance(options, list):
                return options
        return fallback_options

    def guess_free_response_answer() -> str:
        # This bot never learns the real answer (math_teacher.py never
        # publishes accepted_answers anywhere a student can read -- see
        # its own module docstring's "Authoring" section), so it can't
        # actually try to get a free_response question right the way it
        # picks a plausible-looking option for multiple_choice. A short
        # random numeric guess is enough to prove the free_response
        # answer/grading/tally path actually moves end to end without
        # this bot pretending to know something it structurally can't.
        return str(random.randint(0, 99))

    try:
        while True:
            time.sleep(0.5)
            question_type = current_question_type()
            options = current_question_options()
            has_question = bool(options) if question_type == "multiple_choice" else current_index is not None
            if current_index is not None and current_index != answered_index and has_question:
                # Snapshot before the think-delay: question_index/
                # options/my_tier can all advance to the NEXT question
                # while this student is "thinking" about the current
                # one, and the answer submitted below should reflect
                # what they were actually looking at, not whatever's
                # current by the time they finish.
                answering_index = current_index
                answering_type = question_type
                answering_options = options
                answering_tier = my_tier if my_tier else DEFAULT_TIER
                time.sleep(random.uniform(MIN_THINK_SECONDS, MAX_THINK_SECONDS))
                payload = {"question_index": answering_index, "tier": answering_tier}
                if answering_type == "multiple_choice":
                    if not answering_options:
                        continue  # nothing to pick from -- wait for the next question instead
                    payload["option"] = random.randrange(len(answering_options))
                else:
                    payload["text"] = guess_free_response_answer()
                world.set_attribute(me, class_attr("answer"), payload)
                answered_index = answering_index
                print(
                    f"[{name}] answered question {answering_index} with "
                    f"{'option ' + str(payload['option']) if answering_type == 'multiple_choice' else 'text ' + repr(payload['text'])} "
                    f"(tier={answering_tier})",
                    flush=True,
                )
    except KeyboardInterrupt:
        print(f"[{name}] left the class")
