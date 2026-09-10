"""Math Student — the other half of the math class first slice (see
math_teacher.py). Each running instance is one simulated student:
watches the teacher's current question and submits an answer after a
short random "thinking" delay, standing in for a real student clicking
an option until there's a real UI for it — swapping that random pick
for an actual click later doesn't change the protocol at all, only
where the option index comes from.

Entities published:
  Its own entity, kind="math_student":
    student_name — a short auto-generated label, purely for display
      once a real UI shows a roster; not read by math_teacher.py.
    answer — {"question_index": int, "option": int}, rewritten every
      time this student answers a new question.

Reads (subscribing to names, not a specific entity — there's exactly
one math-teacher-control entity in practice, but this never assumes
that; the FIRST value seen for each name is what's used, matching how
every other plugin here treats a "there should only be one" attribute):
    question_options, question_index
"""

import random
import string
import time

from sotrice_client import World

MIN_THINK_SECONDS = 2.0
MAX_THINK_SECONDS = 8.0


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

    current_options: list = []
    current_index = None
    answered_index = None

    def on_question_options(entity, attribute, value, source):
        global current_options
        if isinstance(value, list):
            current_options = value

    def on_question_index(entity, attribute, value, source):
        global current_index
        if isinstance(value, int):
            current_index = value

    world.subscribe("question_options", on_question_options, replay=True)
    world.subscribe("question_index", on_question_index, replay=True)

    try:
        while True:
            time.sleep(0.5)
            if current_index is not None and current_index != answered_index and current_options:
                # Snapshot before the think-delay: question_index/
                # question_options can advance to the NEXT question
                # while this student is "thinking" about the current
                # one, and the answer submitted below should reflect
                # what they were actually looking at, not whatever's
                # current by the time they finish.
                answering_index = current_index
                answering_options = current_options
                time.sleep(random.uniform(MIN_THINK_SECONDS, MAX_THINK_SECONDS))
                option = random.randrange(len(answering_options))
                world.set_attribute(me, "answer", {"question_index": answering_index, "option": option})
                answered_index = answering_index
                print(f"[{name}] answered question {answering_index} with option {option}", flush=True)
    except KeyboardInterrupt:
        print(f"[{name}] left the class")
