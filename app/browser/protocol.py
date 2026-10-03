"""What the Chrome extension and the backend send each other.

The extension turns the live page into a compact semantic snapshot (`PageState`): controls with
temporary ids, headings, and short text blocks. The backend only ever asks it to perform one of
`ACTIONS` on one of those ids; it never sends JavaScript or CSS selectors.

Websocket messages (JSON, all with a "type"):
  extension -> backend
    hello         {installation_id, token, version, user_agent}      first message, within 10 s
    tab           {tab: {tab_id, url, title}}                        the active tab changed or loaded
    page_changed  {tab_id, url}                                      the page's DOM changed meaningfully
    result        {id, ok, data | error}                             answer to a command
    pong          {}
  backend -> extension
    welcome       {profile_name}
    command       {id, action, args, tab_id}                         action is "get_page_state" or one of ACTIONS
    ping          {}
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field

ActionName = Literal["click", "type", "clear", "select", "check", "uncheck", "press_enter", "scroll",
                     "go_back", "navigate", "focus"]
ACTIONS: tuple[str, ...] = ActionName.__args__  # type: ignore[attr-defined]

# Roles a control can have in a snapshot. "text", "heading", "alert" and "dialog" carry content only.
CONTROL_ROLES = {"button", "link", "textbox", "searchbox", "checkbox", "radio", "switch", "select", "combobox",
                 "listbox", "option", "tab", "menuitem", "slider", "spinbutton", "password", "treeitem"}


class PageElement(BaseModel):
    id: Optional[str] = None  # set on controls; text-only items have none
    role: str
    label: str = ""
    value: Optional[str] = None
    checked: Optional[bool] = None
    selected: Optional[bool] = None
    expanded: Optional[bool] = None
    enabled: bool = True
    required: bool = False
    invalid: bool = False
    sensitive: bool = False  # value withheld (password, card number, SSN, one-time code...)
    level: Optional[int] = None  # heading level
    options: list[str] = Field(default_factory=list)  # <select> option texts
    group: Optional[str] = None  # radio group / fieldset legend
    description: Optional[str] = None  # aria-describedby text, e.g. a validation message
    placeholder: Optional[str] = None
    input_type: Optional[str] = None
    href: Optional[str] = None
    in_dialog: bool = False
    in_view: bool = True


class PageState(BaseModel):
    doc_id: str  # changes on every new document, so ids from an old page are rejected
    tab_id: Optional[int] = None
    url: str
    title: str = ""
    site_name: Optional[str] = None
    elements: list[PageElement] = Field(default_factory=list)
    dialog_open: bool = False
    truncated: bool = False
    at_top: bool = True
    at_bottom: bool = True

    def control(self, element_id: Optional[str]) -> Optional[PageElement]:
        if not element_id:
            return None
        return next((e for e in self.elements if e.id == element_id), None)


class ActionResult(BaseModel):
    success: bool
    action: str
    url_before: Optional[str] = None
    url_after: Optional[str] = None
    page_changed: bool = False
    value: Optional[str] = None  # what the field holds after type/select
    new_tab_id: Optional[int] = None  # the action opened a new tab, which is now the page
    # stale_element | not_found | disabled | not_visible | intercepted | invalid_action | blocked |
    # unsupported_page | no_tab | timeout | disconnected | value_mismatch
    error: Optional[str] = None
    detail: Optional[str] = None
