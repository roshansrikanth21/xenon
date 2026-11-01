"""Turn a goal into a small, permitted action plan with the chat model.

The planner is the only place a model proposes desktop actions. It cannot
propose shell or destructive kinds: parsing rejects anything outside the
policy's allowed set, and execution still goes through the approval runner.
"""
from __future__ import annotations

import json
import re

from xenon.actions import READ_ONLY_KINDS, SIDE_EFFECT_KINDS, Action

ALLOWED_KINDS = tuple(sorted(READ_ONLY_KINDS | SIDE_EFFECT_KINDS))
MAX_STEPS = 8

SYSTEM_PROMPT = """You are Xenon's desktop task planner.

Return JSON only, shaped exactly like:
{"steps":[{"kind":"observe","target":"","value":"","reason":"why"}]}

Allowed kinds: __KINDS__.

Rules:
- Plan the smallest number of steps that can achieve the goal. Maximum __MAX_STEPS__.
- Use "observe" first when the plan depends on the current screen.
- type_text needs value; open_app and focus_window need target; press_keys needs
  value such as "ctrl+s"; click_control needs target (control name) and may use
  value for control type.
- You cannot run shell commands, delete files, or make HTTP requests. Never
  propose those; the parser will reject them.
- Do not invent screen contents that were not provided.
"""


class PlannerError(ValueError):
    """Raised when the model does not return a usable plan."""


class ModelPlanner:
    def __init__(self, model) -> None:
        self._model = model

    def plan(self, goal: str, screen_context: str = ""):
        messages = [
            {
                "role": "system",
                "content": SYSTEM_PROMPT.replace(
                    "__KINDS__", ", ".join(ALLOWED_KINDS)
                ).replace("__MAX_STEPS__", str(MAX_STEPS)),
            },
            {
                "role": "user",
                "content": f"Goal: {goal}\n\nCurrent screen:\n{screen_context or '(not provided)'}",
            },
        ]
        response = self._model.complete(messages)
        return Plan(goal=goal, actions=self._parse(goal, response))

    def _parse(self, goal: str, response: str) -> tuple[Action, ...]:
        payload = _extract_json(response)
        steps = payload.get("steps") if isinstance(payload, dict) else None
        if not isinstance(steps, list) or not steps:
            raise PlannerError("plan must contain a non-empty steps list")
        if len(steps) > MAX_STEPS:
            raise PlannerError(f"plan exceeds {MAX_STEPS} steps")

        actions: list[Action] = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise PlannerError(f"step {index} is not an object")
            kind = str(step.get("kind", "")).strip().lower()
            if kind not in ALLOWED_KINDS:
                raise PlannerError(f"step {index} uses a forbidden kind: {kind!r}")
            action = Action(
                kind=kind,
                target=str(step.get("target", "") or "").strip(),
                value=str(step.get("value", "") or ""),
                reason=str(step.get("reason", "") or ""),
            )
            _validate_fields(index, action)
            actions.append(action)
        return tuple(actions)


class Plan:
    def __init__(self, goal: str, actions: tuple[Action, ...]) -> None:
        self.goal = goal
        self.actions = actions

    def __len__(self) -> int:
        return len(self.actions)


def _validate_fields(index: int, action: Action) -> None:
    if action.kind == "type_text" and not action.value:
        raise PlannerError(f"step {index} type_text needs a value")
    if action.kind == "press_keys" and not action.value:
        raise PlannerError(f"step {index} press_keys needs a value")
    if action.kind in {"open_app", "focus_window", "click_control"} and not action.target:
        raise PlannerError(f"step {index} {action.kind} needs a target")
    if action.kind == "write_file" and not action.target:
        raise PlannerError(f"step {index} write_file needs a target path")


def _extract_json(response: str) -> dict:
    text = response.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise PlannerError("model response did not contain a JSON object")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise PlannerError(f"invalid plan JSON: {error}") from error
