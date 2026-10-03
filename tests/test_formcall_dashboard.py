"""The dashboard shows the phone form session: state, provenance, verification, notices, receipt; never secrets."""

import json

from fastapi.testclient import TestClient

from test_formcall_flow import (ENGLISH, PHONE, Caller, mail, model, onboard_english, phone_forms,  # noqa: F401
                                task_row)


def test_dashboard_shows_the_session_without_secrets(model, mail):  # noqa: F811
    c = Caller(PHONE, "sms")
    onboard_english(c)
    for line in ENGLISH[:4]:
        c.say(line)
    c.say("6789")
    for line in ENGLISH[5:]:
        c.say(line)
    reply = c.say("yes")  # read the first notice's exact wording
    reply = c.say("I don't agree to that")
    while "exact wording" in reply:
        reply = c.say("that's fine")
    c.say("that's correct")
    c.say("yes")
    c.say("yes")
    c.say("ana.lopez@example.com")
    c.say("yes")

    from app.main import app

    with TestClient(app) as client:
        view = client.get(f"/api/tasks/{task_row().id}").json()
    fc = view["formcall"]
    assert fc["state"] == "prepared" and fc["approved"] and fc["language"] == "en"
    income = fc["fields"]["monthly_income"]
    assert income["provenance"] == "caller" and income["verification"] == "confirmed"
    assert income["heard"] == "300 a week" and income["normalized"] == "$300 each week"
    statuses = {n["category"]: n["status"] for n in fc["notices"]}
    assert statuses["data_sharing"] == "disagreed" and statuses["third_party_contact"] == "acknowledged"
    assert fc["receipt"]["status"] == "sent" and fc["receipt"]["to"] == "a•••@example.com"
    blob = json.dumps(view)
    assert "4821" not in blob  # PIN
    assert "6789" not in blob.replace("***-**-6789", "")  # SSN only masked
