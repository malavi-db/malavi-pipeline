"""The Google-side mail guard (curation/apps_script/mail.gs, 2026-10-16).

Two kinds of test. The static ones read the .gs files: nothing outside mail.gs may call
GmailApp.sendEmail, MailApp.sendEmail or GmailMessage.forward, and mail.gs must be in the
bundle (a bundle without it would make every send throw). The behavioral ones run
sendMail_ and forwardMail_ under Node with the Google services replaced by stand-ins that
record what was sent and what was remembered, the same way the verdict notifier is tested.

Each behavioral test is one rule the plan review of 2026-10-16 asked for: unset means dry
and the alarm goes once a day; live sends once and remembers; redirect carries the real
recipient in the subject and never the real cc; a curator-kind message goes only to a
listed address; a submitter never reads another submitter's submission id; the per-run cap
and the quota reserve hold; a memory that cannot be written holds the message (fail
closed); a send that throws forgets its key so the retry can happen; force bypasses the
mode for the alarm and the fallbacks.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from malavi_curation.config import repo_root

SCRIPTS = repo_root() / "curation" / "apps_script"
FILES = [str(SCRIPTS / "settings.gs"), str(SCRIPTS / "mail.gs"), str(SCRIPTS / "status_sheet.gs")]

# The driver: loads the files into a sandbox, runs the scenario script the test passes,
# and prints what was sent, what was held, the properties and the Mail tab.
HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const crypto = require("crypto");
const request = JSON.parse(process.argv[2]);
const sent = [];
const forwards = [];
class Grid {
  constructor(name, rows) { this.name = name; this.rows = rows || []; }
  _ensure(r, c) { while (this.rows.length < r) this.rows.push([]); for (const row of this.rows) while (row.length < c) row.push(""); }
  getName() { return this.name; }
  getLastRow() { let last = 0; this.rows.forEach((row, i) => { if (row.some(v => v !== "")) last = i + 1; }); return last; }
  getLastColumn() { let last = 0; this.rows.forEach(row => row.forEach((v, j) => { if (v !== "") last = Math.max(last, j + 1); })); return last; }
  getMaxRows() { return 1000; }
  getMaxColumns() { return 26; }
  setFrozenRows() {}
  getRange(r, c, nr, nc) {
    nr = nr || 1; nc = nc || 1; const grid = this;
    return {
      setNumberFormat() { return this; }, setFontWeight() { return this; },
      getValues() { grid._ensure(r + nr - 1, c + nc - 1); const out = []; for (let i = 0; i < nr; i++) out.push(grid.rows[r - 1 + i].slice(c - 1, c - 1 + nc)); return out; },
      setValues(values) { grid._ensure(r + nr - 1, c + nc - 1); values.forEach((row, i) => row.forEach((v, j) => { grid.rows[r - 1 + i][c - 1 + j] = v; })); return this; },
      setValue(v) { grid._ensure(r, c); grid.rows[r - 1][c - 1] = v; return this; },
      clearContent() { return this; },
    };
  }
}
const tabs = {};
for (const name of Object.keys(request.sheet || {})) tabs[name] = new Grid(name, request.sheet[name]);
const spreadsheet = {
  getSheetByName: (name) => tabs[name] || null,
  insertSheet: (name) => { tabs[name] = new Grid(name); return tabs[name]; },
  getSheets: () => Object.values(tabs), getUrl: () => "https://sheet", getId: () => "SHEET",
};
const properties = Object.assign({ CURATORS: "lead@example.org, second@example.org",
                                   ATTENTION_TO: "lead@example.org" }, request.properties || {});
let memoryBroken = !!request.memory_broken;
const sandbox = {
  GmailApp: {
    sendEmail: (to, subject, body, options) => {
      if (request.bounce && to === request.bounce) throw new Error("bounced");
      sent.push({ to, subject, body, options });
    },
  },
  SpreadsheetApp: { openById: () => spreadsheet },
  Utilities: {
    formatDate: () => "2026-10-16T12:00:00Z",
    computeDigest: (alg, text) => Array.from(crypto.createHash("sha256").update(String(text), "utf8").digest()).map(b => b > 127 ? b - 256 : b),
    DigestAlgorithm: { SHA_256: "SHA_256" }, Charset: { UTF_8: "UTF_8" },
  },
  Logger: { log: () => {} },
  LockService: { getScriptLock: () => ({ waitLock: () => {}, releaseLock: () => {} }) },
  MailApp: { getRemainingDailyQuota: () => (request.quota === undefined || request.quota === null ? 100 : request.quota) },
  Session: { getEffectiveUser: () => ({ getEmail: () => "owner@example.org" }) },
  PropertiesService: {
    getScriptProperties: () => ({
      getProperty: (name) => (name in properties ? properties[name] : null),
      setProperty: (name, value) => { if (memoryBroken && name.indexOf("mail:") === 0) throw new Error("properties quota"); properties[name] = String(value); },
      deleteProperty: (name) => { delete properties[name]; },
      getProperties: () => Object.assign({}, properties),
    }),
  },
  __forwards: forwards,
  __result: null,
};
vm.createContext(sandbox);
for (const file of request.files) vm.runInContext(fs.readFileSync(file, "utf8"), sandbox);
let error = "";
try { vm.runInContext(request.script, sandbox); } catch (err) { error = String(err && err.message ? err.message : err); }
const sheet = {};
for (const name of Object.keys(tabs)) sheet[name] = tabs[name].rows;
console.log(JSON.stringify({ sent, forwards, properties, sheet, result: sandbox.__result, error }));
"""


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def _run(tmp_path: Path, script: str, properties=None, sheet=None, quota=None,
         memory_broken=False, bounce=""):
    node = _node()
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    request = {"files": FILES, "script": script, "properties": properties or {},
               "sheet": sheet, "quota": quota, "memory_broken": memory_broken,
               "bounce": bounce}
    done = subprocess.run([node, str(harness), json.dumps(request)],
                          capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


SEND = ("__result = sendMail_({ to: 'second@example.org', subject: 'Hello', body: 'B', "
        "kind: 'curator', submissionId: 'MALAVI-SUB-2026-000001', key: 'k1' });")
SEND_TWICE = SEND + " var first = __result; " + SEND.replace("__result =", "var second =") + \
             " __result = { first: first, second: second };"


def _mail_rows(result):
    rows = result["sheet"].get("Mail") or []
    if not rows:
        return []
    header, *body = rows
    return [dict(zip(header, r)) for r in body if any(v != "" for v in r)]


# --------------------------------------------------------------------------------------
# Static: one chokepoint
# --------------------------------------------------------------------------------------

def _code_lines(text: str):
    """Lines that are code, not comments: block comments and // lines removed."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return [line for line in text.splitlines() if not line.strip().startswith("//")]


def test_nothing_outside_mail_gs_sends():
    offenders = []
    for path in sorted(SCRIPTS.glob("*.gs")):
        if path.name == "mail.gs":
            continue
        for number, line in enumerate(_code_lines(path.read_text(encoding="utf-8")), 1):
            if re.search(r"\b(GmailApp|MailApp)\.sendEmail\(|\.forward\(", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == [], "every send goes through mail.gs:\n" + "\n".join(offenders)


def test_mail_gs_sends_through_gmail_and_is_in_the_bundle():
    spec = importlib.util.spec_from_file_location("build_bundle", SCRIPTS / "build_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "mail.gs" in module.RUNTIME_FILES, "a bundle without mail.gs makes every send throw"
    text = (SCRIPTS / "mail.gs").read_text(encoding="utf-8")
    assert "GmailApp.sendEmail(" in text and ".forward(" in text


def test_mail_gs_reads_no_setting_at_top_level():
    text = (SCRIPTS / "mail.gs").read_text(encoding="utf-8")
    for line in _code_lines(text):
        if line.startswith("var ") and ("setting_(" in line or "settingList_(" in line):
            raise AssertionError(f"top-level setting read: {line}")
    assert "SETTINGS_NEEDED_MAIL" in text


# --------------------------------------------------------------------------------------
# The mode
# --------------------------------------------------------------------------------------

def test_unset_means_dry_and_the_alarm_goes_once(tmp_path):
    result = _run(tmp_path, SEND_TWICE, properties={"MAIL_MODE": ""})
    assert result["result"]["first"]["sent"] is False
    assert "unset (dry)" in result["result"]["first"]["held"]
    # The only message that went out is the alarm, to the fallback address, once.
    assert [m["to"] for m in result["sent"]] == ["lead@example.org"]
    assert "mail is being held" in result["sent"][0]["subject"]
    assert "NOT SET" in result["sent"][0]["body"]
    assert any(k.startswith("mailalarm:") for k in result["properties"])
    # Held count: two messages held today.
    held = [v for k, v in result["properties"].items() if k.startswith("mailheld:")]
    assert held == ["2"]


def test_explicit_dry_holds_and_records_the_reason(tmp_path):
    result = _run(tmp_path, SEND, properties={"MAIL_MODE": "dry", "STATUS_SHEET_ID": "SHEET"})
    assert result["result"] == {"sent": False, "held": "MAIL_MODE=dry", "to": "second@example.org"}
    rows = _mail_rows(result)
    ours = [r for r in rows if r["to"] == "second@example.org"]
    assert ours and "NOT SENT: MAIL_MODE=dry" in ours[0]["note"]
    assert ours[0]["kind"] == "curator" and ours[0]["mode"] == "dry"


def test_live_sends_once_and_remembers(tmp_path):
    result = _run(tmp_path, SEND_TWICE, properties={"MAIL_MODE": "live"})
    assert result["result"]["first"]["sent"] is True
    assert result["result"]["second"] == {"sent": False, "held": "duplicate",
                                          "to": "second@example.org"}
    assert [m["to"] for m in result["sent"]] == ["second@example.org"]
    assert result["sent"][0]["options"]["name"] == "MalAvi"
    keys = [k for k in result["properties"] if k.startswith("mail:")]
    assert len(keys) == 1


def test_the_same_key_to_a_different_person_is_a_different_message(tmp_path):
    script = SEND + " var a = __result; " + \
        SEND.replace("second@example.org", "lead@example.org").replace("__result =", "var b =") + \
        " __result = { a: a, b: b };"
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live"})
    assert result["result"]["a"]["sent"] and result["result"]["b"]["sent"]
    assert len(result["sent"]) == 2


def test_a_memory_older_than_the_window_does_not_count(tmp_path):
    # Remember the key as sent 31 days ago under the default window.
    script = ("var k = mailSha_('k1|second@example.org'); "
              "PropertiesService.getScriptProperties().setProperty('mail:' + k, String(Date.now() - 31 * 86400000)); "
              + SEND)
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live"})
    assert result["result"]["sent"] is True


def test_redirect_goes_to_the_redirect_address_with_the_real_recipient_in_the_subject(tmp_path):
    script = ("__result = sendMail_({ to: 'jane@example.edu', subject: 'Hello', body: 'B', "
              "kind: 'submitter', options: { cc: 'second@example.org' }, key: 'k' });")
    result = _run(tmp_path, script, properties={"MAIL_MODE": "redirect",
                                                "MAIL_REDIRECT_TO": "me@example.org"})
    assert result["result"]["held"] == "redirected"
    message = [m for m in result["sent"] if "REDIRECT" in m["subject"]][0]
    assert message["to"] == "me@example.org"
    assert message["subject"] == "[REDIRECT to jane@example.edu] Hello"
    assert "cc" not in message["options"]
    # Nothing remembered: the real recipient has not had it.
    assert not any(k.startswith("mail:") for k in result["properties"])
    # And the once-a-day alarm says so.
    assert any("mail is being held" in m["subject"] for m in result["sent"])


def test_redirect_without_an_address_holds(tmp_path):
    result = _run(tmp_path, SEND, properties={"MAIL_MODE": "redirect"})
    assert "MAIL_REDIRECT_TO is not set" in result["result"]["held"]
    assert result["sent"] == []


def test_the_redirect_address_is_never_taken_from_a_message(tmp_path):
    """Finding 3 of the review: the address lives in the property only."""
    script = ("__result = sendMail_({ to: 'second@example.org', subject: 'S', body: 'B', "
              "kind: 'curator', key: 'k', redirect_to: 'attacker@example.org' });")
    result = _run(tmp_path, script, properties={"MAIL_MODE": "redirect",
                                                "MAIL_REDIRECT_TO": "me@example.org"})
    assert all(m["to"] != "attacker@example.org" for m in result["sent"])


# --------------------------------------------------------------------------------------
# The recipient rules
# --------------------------------------------------------------------------------------

def test_a_curator_message_goes_only_to_a_listed_address(tmp_path):
    script = ("__result = sendMail_({ to: 'stranger@example.org', subject: 'S', body: 'B', "
              "kind: 'curator', key: 'k' });")
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live"})
    assert "not on CURATORS" in result["result"]["held"]
    assert result["sent"] == []


def test_an_assignee_outside_curators_is_still_a_listed_address(tmp_path):
    script = ("__result = sendMail_({ to: 'helper@example.org', subject: 'S', body: 'B', "
              "kind: 'curator', key: 'k' });")
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live",
                                                "ASSIGNEES": "helper@example.org"})
    assert result["result"]["sent"] is True


SUBMISSIONS_HEADER = ["id", "received", "submitter_name", "submitter_email", "assignee"]


def _sheet(rows):
    return {"Submissions": [SUBMISSIONS_HEADER] + rows}


def test_a_submitter_never_reads_another_submitters_id(tmp_path):
    sheet = _sheet([["MALAVI-SUB-2026-000001", "", "Jane", "jane@example.edu", ""],
                    ["MALAVI-SUB-2026-000002", "", "Ash", "ash@example.edu", ""]])
    script = ("__result = sendMail_({ to: 'jane@example.edu', subject: 'S', "
              "body: 'about MALAVI-SUB-2026-000002', kind: 'submitter', "
              "submissionId: 'MALAVI-SUB-2026-000001', key: 'k' });")
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live", "STATUS_SHEET_ID": "SHEET"},
                  sheet=sheet)
    assert "MALAVI-SUB-2026-000002" in result["result"]["held"]
    assert result["sent"] == []


def test_a_submitter_may_read_about_their_own_other_submission(tmp_path):
    """Finding 12: one person often has two in the queue."""
    sheet = _sheet([["MALAVI-SUB-2026-000001", "", "Ash", "ash@example.edu", ""],
                    ["MALAVI-SUB-2026-000002", "", "Ash", "ash@example.edu", ""]])
    script = ("__result = sendMail_({ to: 'ash@example.edu', subject: 'S', "
              "body: 'same name as in MALAVI-SUB-2026-000001', kind: 'submitter', "
              "submissionId: 'MALAVI-SUB-2026-000002', key: 'k' });")
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live", "STATUS_SHEET_ID": "SHEET"},
                  sheet=sheet)
    assert result["result"]["sent"] is True


def test_an_id_the_sheet_does_not_know_is_allowed_through(tmp_path):
    sheet = _sheet([["MALAVI-SUB-2026-000001", "", "Jane", "jane@example.edu", ""]])
    script = ("__result = sendMail_({ to: 'jane@example.edu', subject: 'S', "
              "body: 'see MALAVI-SUB-2026-000099', kind: 'submitter', "
              "submissionId: 'MALAVI-SUB-2026-000001', key: 'k' });")
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live", "STATUS_SHEET_ID": "SHEET"},
                  sheet=sheet)
    assert result["result"]["sent"] is True


# --------------------------------------------------------------------------------------
# Caps, quota, failure
# --------------------------------------------------------------------------------------

def test_the_per_run_cap_holds_the_rest_and_alarms_once(tmp_path):
    script = "var out = []; for (var i = 0; i < 4; i++) { out.push(sendMail_({ to: 'second@example.org', subject: 'S' + i, body: 'B', kind: 'curator', key: 'k' + i })); } __result = out;"
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live", "MAIL_MAX_PER_RUN": "2"})
    outcomes = result["result"]
    assert [o["sent"] for o in outcomes] == [True, True, False, False]
    assert outcomes[2]["held"] == "per-run cap"
    alarms = [m for m in result["sent"] if "cap" in m["subject"]]
    assert len(alarms) == 1 and alarms[0]["to"] == "lead@example.org"


def test_the_quota_reserve_holds(tmp_path):
    result = _run(tmp_path, SEND, properties={"MAIL_MODE": "live"}, quota=15)
    assert "quota reserve" in result["result"]["held"]
    assert result["sent"] == []


def test_a_memory_that_cannot_be_written_holds_the_message(tmp_path):
    """Fail closed (finding 11): unremembered mail is the duplicate of tomorrow."""
    result = _run(tmp_path, SEND, properties={"MAIL_MODE": "live"}, memory_broken=True)
    assert "memory could not be written" in result["result"]["held"]
    assert result["sent"] == []


def test_a_send_that_throws_forgets_its_key_and_rethrows(tmp_path):
    result = _run(tmp_path, SEND, properties={"MAIL_MODE": "live"}, bounce="second@example.org")
    assert result["error"] == "bounced"
    assert not any(k.startswith("mail:") for k in result["properties"])


def test_force_bypasses_the_mode_and_is_recorded(tmp_path):
    script = ("__result = sendMail_({ to: 'owner@example.org', subject: 'FAILED', body: 'B', "
              "kind: 'system', force: true });")
    result = _run(tmp_path, script, properties={"MAIL_MODE": "dry", "STATUS_SHEET_ID": "SHEET"})
    assert result["result"]["sent"] is True
    assert [m["to"] for m in result["sent"]] == ["owner@example.org"]
    rows = _mail_rows(result)
    assert rows and rows[-1]["mode"] == "dry (forced)"


def test_an_address_without_an_at_sign_is_held(tmp_path):
    script = "__result = sendMail_({ to: '', subject: 'S', body: 'B', kind: 'submitter' });"
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live"})
    assert result["result"]["held"] == "no usable address"


# --------------------------------------------------------------------------------------
# Forwarding
# --------------------------------------------------------------------------------------

FORWARD = ("var msg = { getId: function () { return 'MSG1'; }, getSubject: function () { return 'Re: x'; }, "
           "forward: function (to, opts) { __forwards.push({ to: to, opts: opts }); } }; "
           "__result = forwardMail_(msg, ['lead@example.org', 'second@example.org'], "
           "{ subject: '[MalAvi X] Re: x', replyTo: 'jane@example.edu' }, 'curator', "
           "'MALAVI-SUB-2026-000001', 'forward');")


def test_a_forward_goes_once_to_the_listed_recipients(tmp_path):
    result = _run(tmp_path, FORWARD + " var again = forwardMail_(msg, ['lead@example.org'], {}, 'curator', 'X', ''); __result = { first: __result, again: again };",
                  properties={"MAIL_MODE": "live"})
    assert result["result"]["first"] == {"sent": 2, "held": []}
    assert result["forwards"][0]["to"] == "lead@example.org,second@example.org"
    assert result["forwards"][0]["opts"]["replyTo"] == "jane@example.edu"
    assert result["result"]["again"]["held"] == [{"to": "lead@example.org", "reason": "duplicate"}]
    assert len(result["forwards"]) == 1


def test_a_forward_in_dry_mode_is_held(tmp_path):
    result = _run(tmp_path, FORWARD, properties={"MAIL_MODE": "dry"})
    assert result["result"]["sent"] == 0
    assert [h["reason"] for h in result["result"]["held"]] == ["MAIL_MODE=dry"] * 2
    assert result["forwards"] == []


def test_a_forward_in_redirect_mode_goes_to_the_redirect_address(tmp_path):
    result = _run(tmp_path, FORWARD, properties={"MAIL_MODE": "redirect",
                                                 "MAIL_REDIRECT_TO": "me@example.org"})
    assert result["forwards"][0]["to"] == "me@example.org"
    assert result["forwards"][0]["opts"]["subject"].startswith("[REDIRECT to lead@example.org, second@example.org]")
    assert result["result"]["sent"] == 0


def test_a_forward_to_an_unlisted_address_is_held_and_the_rest_go(tmp_path):
    script = FORWARD.replace("'second@example.org'", "'stranger@example.org'")
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live"})
    assert result["result"]["sent"] == 1
    assert result["result"]["held"][0]["to"] == "stranger@example.org"
    assert result["forwards"][0]["to"] == "lead@example.org"


# --------------------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------------------

def test_the_status_line_and_the_held_count(tmp_path):
    script = SEND + " __result = { line: mailStatusLine_(), held: mailHeldToday_() };"
    result = _run(tmp_path, script, properties={"MAIL_MODE": ""})
    assert result["result"]["held"] == 1
    assert "MAIL_MODE unset (dry)" in result["result"]["line"]
    assert "1 message(s) held today" in result["result"]["line"]


def test_pruning_removes_only_what_expired(tmp_path):
    script = ("var p = PropertiesService.getScriptProperties(); "
              "p.setProperty('mail:old', String(Date.now() - 40 * 86400000)); "
              "p.setProperty('mail:new', String(Date.now())); "
              "p.setProperty('mailheld:2020-01-01', '3'); "
              "p.setProperty('mailalarm:2020-01-01:dry', '1'); "
              "__result = pruneMailMemory_();")
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live"})
    assert result["result"] == 3
    assert "mail:new" in result["properties"] and "mail:old" not in result["properties"]
    assert "CURATORS" in result["properties"]


# --------------------------------------------------------------------------------------
# Code review of 2026-10-16: a request may downgrade the mode; the alarm's target
# --------------------------------------------------------------------------------------

def test_a_request_can_make_the_mode_more_restrictive_but_never_less(tmp_path):
    """Finding 1: MALAVI_MAIL_MODE=redirect on the machine side must be honored by a
    live endpoint, and a request can never lift the property's own restriction."""
    script = ("requestMailMode_('redirect'); var a = mailMode_(); "
              "requestMailMode_('live'); var b = mailMode_(); "
              "requestMailMode_('nonsense'); var c = mailMode_(); "
              "requestMailMode_(''); var d = mailMode_(); __result = { a: a, b: b, c: c, d: d };")
    live = _run(tmp_path, script, properties={"MAIL_MODE": "live"})["result"]
    assert live == {"a": "redirect", "b": "live", "c": "live", "d": "live"}
    dry = _run(tmp_path, script, properties={"MAIL_MODE": "dry"})["result"]
    assert dry == {"a": "dry", "b": "dry", "c": "dry", "d": "dry"}


def test_a_redirect_request_sends_to_the_redirect_address_on_a_live_project(tmp_path):
    script = "requestMailMode_('redirect'); " + SEND
    result = _run(tmp_path, script, properties={"MAIL_MODE": "live",
                                                "MAIL_REDIRECT_TO": "me@example.org"})
    assert result["result"]["held"] == "redirected"
    real = [m for m in result["sent"] if m["to"] == "second@example.org"]
    assert real == []
    assert any(m["to"] == "me@example.org" and "[REDIRECT to second@example.org]" in m["subject"]
               for m in result["sent"])


def test_in_redirect_mode_the_alarm_goes_to_the_redirect_address(tmp_path):
    """Finding 16: during development nobody but the developer hears from the project."""
    result = _run(tmp_path, SEND, properties={"MAIL_MODE": "redirect",
                                              "MAIL_REDIRECT_TO": "me@example.org"})
    assert {m["to"] for m in result["sent"]} == {"me@example.org"}
    alarm = [m for m in result["sent"] if "held" in m["subject"]]
    assert len(alarm) == 1


def test_the_alarm_marker_is_forgotten_when_the_alarm_itself_bounces(tmp_path):
    """Finding 14: a lost alarm is tried again by the next held message."""
    result = _run(tmp_path, SEND, properties={"MAIL_MODE": "dry"}, bounce="lead@example.org")
    assert result["error"] == ""                      # the send itself was held, not thrown
    assert not any(k.startswith("mailalarm:") for k in result["properties"])


def test_a_forward_in_redirect_mode_drops_the_blind_copies(tmp_path):
    script = FORWARD.replace("replyTo: 'jane@example.edu' }", "replyTo: 'jane@example.edu', bcc: 'x@example.org' }")
    result = _run(tmp_path, script, properties={"MAIL_MODE": "redirect",
                                                "MAIL_REDIRECT_TO": "me@example.org"})
    assert "bcc" not in result["forwards"][0]["opts"]
