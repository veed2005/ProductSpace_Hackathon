"""What the model may answer with, each time it looks at the page.

The model never writes JavaScript or selectors: it picks actions from a fixed vocabulary and refers
to elements by the ids in the snapshot. Code validates every field before anything happens
(app/agent/policy.py), and the browser's reply, not the model, decides whether a step worked.

All fields are required (some nullable) so the schema works with strict structured outputs.
"""

from typing import Literal, Optional

from pydantic import BaseModel

from app.browser.protocol import ActionName


class Step(BaseModel):
    action: ActionName
    element_id: Optional[str]  # an id from the current snapshot; null for scroll / go_back / navigate
    value: Optional[str]  # text to type, option to select, scroll direction, or same-site URL


class Decision(BaseModel):
    # act: run `steps` (1-3, nothing consequential)        ask_user: ask the caller one question
    # confirm: `steps` holds the single consequential step  done: the page shows the goal is achieved
    # blocked: the person must do something at the computer (login, CAPTCHA, code) or it can't be done
    kind: Literal["act", "ask_user", "confirm", "done", "blocked"]
    steps: list[Step]
    say: str  # spoken to the caller: status, question, confirmation summary, or result
    reason: str  # one short sentence for the partner dashboard; no personal details
    evidence: Optional[str]  # for done: exact text on the current page that proves success
