import json
import unittest

from xenon.planner import ModelPlanner, PlannerError


class FakeModel:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> str:
        self.messages.append(messages)
        return self.response


def encoded(steps: list[dict]) -> str:
    return json.dumps({"steps": steps})


VALID = encoded(
    [
        {"kind": "observe", "reason": "see the current window"},
        {"kind": "type_text", "value": "hello", "reason": "write the note"},
    ]
)


class ModelPlannerTests(unittest.TestCase):
    def test_parses_a_valid_plan(self) -> None:
        model = FakeModel(VALID)

        plan = ModelPlanner(model).plan("write a note", "Notepad is open")

        self.assertEqual([action.kind for action in plan.actions], ["observe", "type_text"])
        self.assertEqual(plan.actions[1].value, "hello")
        prompt = json.dumps(model.messages[0])
        self.assertIn("write a note", prompt)
        self.assertIn("Notepad is open", prompt)
        self.assertIn("type_text", prompt)

    def test_accepts_json_inside_a_code_fence(self) -> None:
        model = FakeModel("```json\n" + VALID + "\n```")

        plan = ModelPlanner(model).plan("write a note")

        self.assertEqual(len(plan.actions), 2)

    def test_rejects_invalid_json(self) -> None:
        with self.assertRaises(PlannerError):
            ModelPlanner(FakeModel("not json")).plan("do something")

    def test_rejects_shell_actions(self) -> None:
        model = FakeModel(encoded([{"kind": "run_command", "value": "rm -rf /"}]))

        with self.assertRaises(PlannerError):
            ModelPlanner(model).plan("clean up")

    def test_rejects_empty_plans(self) -> None:
        with self.assertRaises(PlannerError):
            ModelPlanner(FakeModel(encoded([]))).plan("nothing")

    def test_rejects_side_effects_without_required_fields(self) -> None:
        cases = [
            {"kind": "type_text"},
            {"kind": "open_app"},
            {"kind": "press_keys"},
            {"kind": "click_control"},
        ]
        for step in cases:
            with self.subTest(step=step):
                with self.assertRaises(PlannerError):
                    ModelPlanner(FakeModel(encoded([step]))).plan("do it")

    def test_caps_plan_length(self) -> None:
        steps = [{"kind": "observe"} for _ in range(20)]

        with self.assertRaises(PlannerError):
            ModelPlanner(FakeModel(encoded(steps))).plan("loop forever")

    def test_prompt_lists_allowed_kinds_and_forbids_shell(self) -> None:
        model = FakeModel(VALID)

        ModelPlanner(model).plan("anything")

        prompt = model.messages[0][0]["content"] + model.messages[0][1]["content"]
        self.assertIn("press_keys", prompt)
        self.assertIn("never", prompt.lower())


if __name__ == "__main__":
    unittest.main()
