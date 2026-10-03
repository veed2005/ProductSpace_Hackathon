"""What the model may answer with, each time it looks at the page.

The model never writes JavaScript or selectors: it picks actions from a fixed vocabulary and refers
to elements by the ids in the snapshot. Code validates every field before anything happens
(app/agent/policy.py), and the browser's reply, not the model, decides whether a step worked.

All fields are required (some nullable) so the schema works with strict structured outputs.
"""

from typing import Literal, Optional

from pydantic import BaseModel

# What the model may do. `navigate` (typing an address) is deliberately left out: the agent uses the site's
# own links, buttons and search box, so the person sees each step and nothing is guessed.
AgentAction = Literal["search", "click", "type", "clear", "select", "check", "uncheck", "press_enter", "scroll",
                      "go_back", "focus", "switch_tab"]


class Step(BaseModel):
    action: AgentAction
    element_id: Optional[str]  # an id from the current snapshot; null for scroll / go_back / site search / switch_tab
    # words to search for, text to type, option to select, scroll direction, or for switch_tab "previous" or a
    # tab handle from the tab list ("T2")
    value: Optional[str]


class Remember(BaseModel):
    key: str  # canonical profile path, e.g. "date_of_birth", "address.city"
    value: str  # as the caller said it; code converts and checks it (app/agent/memory.py)


class Decision(BaseModel):
    # act: run `steps` (1-3, nothing consequential)        ask_user: ask the caller one question
    # confirm: `steps` holds the single consequential step  done: the page shows the goal is achieved
    # answer: reply to the caller's question from what the page/document says (conversation continues)
    # blocked: the person must do something at the computer (login, CAPTCHA, code) or it can't be done
    kind: Literal["act", "ask_user", "confirm", "done", "answer", "blocked"]
    steps: list[Step]
    say: str  # spoken to the caller: status, question, confirmation summary, or result
    reason: str  # one short sentence for the partner dashboard; no personal details
    evidence: Optional[str]  # done/answer: exact text on the current page or document that supports it
    # Personal details the CALLER said that are worth reusing next time. Only a proposal: the caller is asked
    # before anything is saved. Defaults to None so code and tests can omit it; strict schemas still require it.
    remember: Optional[list[Remember]] = None
