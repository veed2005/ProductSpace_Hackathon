"""Smoke test for the Chrome extension without any LLM: pair, read the portal, click and type by id.

    uv run python scripts/extension_smoke.py [--headed]

Checks priority 1 and the action protocol: the backend sees the page, stale ids are refused,
password/hidden values never leave the browser, and actions report real page changes.
"""

import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser_harness import Server, launch_chromium, pair  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def find(state, role, text):
    for e in state["elements"]:
        if e["role"] == role and text.lower() in (e.get("label") or "").lower():
            return e
    raise AssertionError(f"no {role} {text!r} in snapshot:\n" + "\n".join(
        f"  {x.get('id')} {x['role']} {x.get('label')!r}" for x in state["elements"]))


def main() -> int:
    headed = "--headed" in sys.argv
    with Server(port=8766) as server, sync_playwright() as pw:
        context, ext = launch_chromium(pw, headless=not headed)
        try:
            pair(context, ext, server.url, phone="+15550001111", name="Margaret", pin="4821")
            print("paired; extension connected")

            page = context.new_page()
            page.goto(server.url + "/demo/riverbend/")
            page.wait_for_selector("h1")
            page.bring_to_front()
            time.sleep(0.8)

            s = server.command("get_page_state")
            print(f"snapshot: {s['site_name']!r} {s['title']!r} {len(s['elements'])} items")
            assert s["site_name"] == "Riverbend Health patient portal"
            visits = find(s, "link", "Visits")
            r = server.command("click", element_id=visits["id"], doc_id=s["doc_id"])
            print("click Visits:", r)
            assert r["success"] and r["page_changed"], r

            s = server.command("get_page_state")
            sched = find(s, "button", "Schedule an appointment")
            r = server.command("click", element_id=sched["id"], doc_id=s["doc_id"])
            assert r["success"], r
            time.sleep(0.8)  # wizard step loads

            s = server.command("get_page_state")
            smith = find(s, "radio", "Alan Smith")
            nxt = find(s, "button", "Next")
            assert nxt["enabled"] is False, nxt
            r = server.command("check", element_id=smith["id"], doc_id=s["doc_id"])
            print("check Dr. Smith:", r)
            assert r["success"], r
            s = server.command("get_page_state")
            assert find(s, "radio", "Alan Smith")["checked"] is True
            assert find(s, "button", "Next")["enabled"] is True

            stale = server.command("click", element_id=smith["id"], doc_id="zzzzz")
            print("stale doc id:", stale)
            assert not stale["success"] and stale["error"] == "stale_element"
            missing = server.command("click", element_id="e9999", doc_id=s["doc_id"])
            assert not missing["success"] and missing["error"] == "not_found", missing

            r = server.command("click", element_id=find(s, "button", "Next")["id"], doc_id=s["doc_id"])
            assert r["success"], r
            time.sleep(0.8)
            s = server.command("get_page_state")
            reason = find(s, "textbox", "why you want")
            r = server.command("type", element_id=reason["id"], doc_id=s["doc_id"], value="My knee has been hurting.")
            print("type reason:", r)
            assert r["success"] and r["value"] == "My knee has been hurting.", r
            vtype = find(s, "select", "Visit type")
            r = server.command("select", element_id=vtype["id"], doc_id=s["doc_id"], value="video")
            assert r["success"] and r["value"] == "Video visit", r

            # Privacy: a page with secrets.
            page.goto(server.url + "/demo/testbench/secrets.html")
            time.sleep(0.5)
            s = server.command("get_page_state")
            blob = str(s)
            for secret in ("hunter2-secret", "tok_SECRET123", "4111", "123-45-6789"):
                assert secret not in blob, f"{secret!r} leaked: {blob}"
            pw_field = find(s, "password", "Password")
            assert pw_field["value"] is None and pw_field["sensitive"]
            r = server.command("type", element_id=pw_field["id"], doc_id=s["doc_id"], value="x")
            assert not r["success"] and r["error"] == "blocked", r
            print("privacy: password/hidden/card/SSN values withheld; typing into password refused")

            # The search action on a Letterboxd-like page: a decoy profile search is visible, the site search is
            # hidden behind an animated icon, and Enter only works through the browser's form submission.
            page.goto(server.url + "/demo/reelbox/profile.html")
            time.sleep(1.0)
            s = server.command("get_page_state")
            assert "hidden right now" in s["elements"][0]["label"]
            r = server.command("search", value="Arrival", doc_id=s["doc_id"])
            assert r["success"] and "search.html?q=Arrival" in r["url_after"], r
            assert "opened the search with" in (r["detail"] or ""), r
            print("search: opened the hidden site search, typed, submitted ->", r["url_after"].rsplit("/", 1)[-1])
            page.goto(server.url + "/demo/reelbox/explore.html")
            time.sleep(1.0)
            s = server.command("get_page_state")
            r = server.command("search", value="Past Lives", doc_id=s["doc_id"])
            assert r["success"] and "Go" in r["detail"], r
            print("search: no form and no Enter -> used the Go button")
            shot = server.command("screenshot")
            assert shot["ok"] and shot["jpeg_base64_chars"] > 1000, shot
            print(f"screenshot (opt-in vision fallback): {shot['jpeg_base64_chars']} base64 chars")

            # Tabs: a link that opens a new tab is followed; the window's tabs are listed; switching goes back.
            page.goto(server.url + "/demo/library/index.html")
            page.bring_to_front()
            time.sleep(0.8)
            s = server.command("get_page_state")
            home_tab = s["tab_id"]
            r = server.command("click", element_id=find(s, "link", "Branch hours")["id"], doc_id=s["doc_id"])
            assert r["success"] and r["new_tab_id"] and r["new_tab_id"] != home_tab, r
            hours_tab = r["new_tab_id"]
            s = server.command("get_page_state", tab_id=hours_tab)
            assert "Branch hours" in s["title"], s["title"]
            tabs = {t["tab_id"]: t for t in server.command("list_tabs", tab_id=hours_tab)["tabs"]}
            assert tabs[hours_tab]["active"] and tabs[home_tab]["switchable"] and not tabs[home_tab]["active"], tabs
            print(f"tabs: link opened a new tab; window lists {len(tabs)} tabs:",
                  [t["title"] or "(untitled)" for t in tabs.values()])
            r = server.command("switch_tab", tab_id=hours_tab, value=str(home_tab))
            assert r["success"] and r["new_tab_id"] == home_tab and r["page_changed"], r
            time.sleep(0.6)
            s = server.command("get_page_state")  # no tab named: whichever tab is in front
            assert s["tab_id"] == home_tab and "Catalog" in s["title"], s["title"]
            tabs = {t["tab_id"]: t for t in server.command("list_tabs", tab_id=home_tab)["tabs"]}
            assert tabs[home_tab]["active"] and hours_tab in tabs, tabs  # switching back closed nothing
            r = server.command("switch_tab", tab_id=home_tab, value=str(hours_tab))  # a tab the click opened, by id
            assert r["success"], r
            gone = server.command("switch_tab", tab_id=hours_tab, value="99999999")
            assert not gone["success"] and gone["error"] == "no_tab", gone
            blocked = [t for t in tabs.values() if not t["switchable"]]
            if blocked:
                r = server.command("switch_tab", tab_id=hours_tab, value=str(blocked[0]["tab_id"]))
                assert not r["success"] and r["error"] == "unsupported_page", r
                assert blocked[0]["url"] == "", blocked[0]
            print("tabs: switched back to the first tab and forward again; closed and unusable tabs refused")
            before = len(tabs)
            r = server.command("new_tab", tab_id=hours_tab, value="maple county library hours")
            assert r["success"] and r["new_tab_id"] not in tabs and r["page_changed"], r
            assert r["url_after"].startswith("https://www.google.com/") and "maple" in r["url_after"].lower(), r
            now = {t["tab_id"]: t for t in server.command("list_tabs", tab_id=r["new_tab_id"])["tabs"]}
            assert len(now) == before + 1 and now[r["new_tab_id"]]["active"], now
            empty = server.command("new_tab", tab_id=hours_tab, value="  ")
            assert not empty["success"] and empty["error"] == "invalid_action", empty
            print("new tab: opened a web search in a new tab next to the current one ->", r["url_after"][:60])
            if blocked:  # a lookup from a blank tab: the page can't be read, but a search still opens next to it
                try:
                    server.command("get_page_state", tab_id=blocked[0]["tab_id"])
                    raise AssertionError("a blank tab should not be readable")
                except urllib.error.HTTPError as e:
                    assert e.code == 409, e
                r = server.command("new_tab", tab_id=blocked[0]["tab_id"], value="weather in springfield")
                assert r["success"] and r["new_tab_id"] not in now and r["url_after"].startswith("https://www.google.com/"), r
                print("new tab: a blank tab can't be read, but a lookup from it still opens a search")
            print("\nEXTENSION SMOKE TEST PASSED")
            return 0
        except Exception:
            print("--- server log ---\n" + server.tail())
            raise
        finally:
            context.close()


if __name__ == "__main__":
    sys.exit(main())
