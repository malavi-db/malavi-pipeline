"""The verdict-form notifier (curation/apps_script/notify_on_verdict.gs).

Apps Script cannot be run here, but the part of the notifier that matters -- what the
email says for a given form response, and what goes to the SUBMITTER -- is plain
JavaScript, so it is run under Node with the Google services it touches replaced by
stand-ins that record what was sent. Since 2026-10-16 the notifier also writes the status
sheet (status_sheet.gs) and sends a flag's "Message to the submitter"; the harness carries
an in-memory SpreadsheetApp so those paths run too. Two further tests guard the strings the
notifier shares with the form builder.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from malavi_curation.config import repo_root

SCRIPTS = repo_root() / "curation" / "apps_script"
NOTIFIER = SCRIPTS / "notify_on_verdict.gs"
STATUS_SHEET = SCRIPTS / "status_sheet.gs"
FORM_BUILDER = SCRIPTS / "create_verdict_form.gs"
ADD_QUESTION = SCRIPTS / "add_submitter_message_question.gs"

# Stand-ins for the Apps Script services, and a driver that feeds one form response to the
# trigger and prints every message it sent as JSON, followed by the sheet's contents.
#
# The SpreadsheetApp stand-in is a small in-memory grid per tab that supports exactly the
# calls status_sheet.gs makes (getRange(...).getValues/setValues/setValue/setNumberFormat/
# setFontWeight, getLastRow, getLastColumn, insertSheet, getSheetByName). Pre-seeded rows
# come from request.sheet: { "Submissions": [[header...], [row...]], ... }.
HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const sent = [];
class Grid {
  constructor(name, rows) { this.name = name; this.rows = rows || []; this.frozen = 0; }
  _ensure(r, c) {
    while (this.rows.length < r) this.rows.push([]);
    for (const row of this.rows) while (row.length < c) row.push("");
  }
  getName() { return this.name; }
  getLastRow() { let last = 0; this.rows.forEach((row, i) => { if (row.some(v => v !== "")) last = i + 1; }); return last; }
  getLastColumn() { let last = 0; this.rows.forEach(row => row.forEach((v, j) => { if (v !== "") last = Math.max(last, j + 1); })); return last; }
  getMaxRows() { return 1000; }
  getMaxColumns() { return 26; }
  setFrozenRows(n) { this.frozen = n; }
  getRange(r, c, nr, nc) {
    nr = nr || 1; nc = nc || 1; const grid = this;
    return {
      setNumberFormat() { return this; },
      setFontWeight() { return this; },
      getValues() { grid._ensure(r + nr - 1, c + nc - 1); const out = []; for (let i = 0; i < nr; i++) out.push(grid.rows[r - 1 + i].slice(c - 1, c - 1 + nc)); return out; },
      setValues(values) { grid._ensure(r + nr - 1, c + nc - 1); values.forEach((row, i) => row.forEach((v, j) => { grid.rows[r - 1 + i][c - 1 + j] = v; })); return this; },
      setValue(v) { grid._ensure(r, c); grid.rows[r - 1][c - 1] = v; return this; },
      clearContent() { for (let i = 0; i < nr; i++) for (let j = 0; j < nc; j++) { grid._ensure(r + i, c + j); grid.rows[r - 1 + i][c - 1 + j] = ""; } return this; },
    };
  }
}
const request = JSON.parse(process.argv[2]);
const tabs = {};
for (const name of Object.keys(request.sheet || {})) tabs[name] = new Grid(name, request.sheet[name]);
const spreadsheet = {
  getSheetByName: (name) => tabs[name] || null,
  insertSheet: (name) => { tabs[name] = new Grid(name); return tabs[name]; },
  getSheets: () => Object.values(tabs),
  getUrl: () => "https://docs.google.com/spreadsheets/d/SHEET",
  getId: () => "SHEET",
};
const sandbox = {
  GmailApp: { sendEmail: (to, subject, body, options) => sent.push({ to, subject, body, options }) },
  SpreadsheetApp: { openById: () => spreadsheet },
  Utilities: {
    formatDate: (date, zone, pattern) => pattern.indexOf("'T'") !== -1 ? "2026-10-02T15:04:00Z" : "2026-10-02 15:04 GMT",
    // mail.gs hashes message keys; Apps Script returns signed bytes, so does this.
    computeDigest: (alg, text) => Array.from(require("crypto").createHash("sha256").update(String(text), "utf8").digest()).map(b => b > 127 ? b - 256 : b),
    DigestAlgorithm: { SHA_256: "SHA_256" },
    Charset: { UTF_8: "UTF_8" },
  },
  Logger: { log: () => {} },
  LockService: { getScriptLock: () => ({ waitLock: () => {}, releaseLock: () => {} }) },
  MailApp: { getRemainingDailyQuota: () => 100 },
  ScriptApp: {},
  Session: { getEffectiveUser: () => ({ getEmail: () => "owner@example.org" }) },
};
// MAIL_MODE live by default: these tests are about what the notifier says, and the mail
// guard (mail.gs, 2026-10-16) sends nothing until the mode is set.
const properties = { CURATORS: request.curators.join(", "), VERDICT_FORM_ID: "FORM",
                     MAIL_MODE: request.mail_mode || "live" };
if (request.sheet) properties.STATUS_SHEET_ID = "SHEET";
if (request.update_form_url) properties.UPDATE_FORM_URL = request.update_form_url;
sandbox.PropertiesService = {
  getScriptProperties: () => ({
    getProperty: (name) => (name in properties ? properties[name] : null),
    setProperty: (name, value) => { properties[name] = String(value); },
    deleteProperty: (name) => { delete properties[name]; },
    getProperties: () => Object.assign({}, properties),
  }),
};
vm.createContext(sandbox);
for (const file of process.argv.slice(3)) vm.runInContext(fs.readFileSync(file, "utf8"), sandbox);
const event = {
  response: {
    getRespondentEmail: () => request.email,
    getTimestamp: () => new Date(0),
    getItemResponses: () => request.answers.map(([title, value]) => ({
      getItem: () => ({ getTitle: () => title }),
      getResponse: () => value,
    })),
  },
};
sandbox.onVerdictRecorded(event);
const sheet = {};
for (const name of Object.keys(tabs)) sheet[name] = tabs[name].rows;
console.log(JSON.stringify({ sent, sheet }));
"""

FILES = [str(SCRIPTS / "settings.gs"), str(SCRIPTS / "mail.gs"), str(STATUS_SHEET),
         str(NOTIFIER)]


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    return node


def _run_full(tmp_path: Path, email: str, answers, curators=("lead@example.org",
                                                              "second@example.org"),
              sheet=None, update_form_url="", harness=HARNESS):
    """Feed one form response to the trigger; return {sent, sheet}."""
    node = _node()
    script = tmp_path / "harness.js"
    script.write_text(harness, encoding="utf-8")
    request = {"email": email, "answers": answers, "curators": list(curators),
               "sheet": sheet, "update_form_url": update_form_url}
    done = subprocess.run([node, str(script), json.dumps(request), *FILES],
                          capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def _run(tmp_path: Path, email: str, answers, curators=("lead@example.org",
                                                         "second@example.org")):
    """Feed one form response to the trigger with NO status sheet; return the messages."""
    return _run_full(tmp_path, email, answers, curators)["sent"]


FLAG = [["Submission id", "MALAVI-SUB-0000-000003"], ["Revision", "1"],
        ["What are you recording?", "Record a verdict on a submission"],
        ["Your verdict on this revision", "Flag for further review"],
        ["Why?", "EXAMPL01 carries two bases that the nearest lineage does not share; "
                 "please re-read the trace."],
        ["What did you check?", ["Proposed names", "Sequences"]]]


def test_every_curator_is_told_including_the_one_who_clicked(tmp_path):
    sent = _run(tmp_path, "second@example.org", FLAG)
    assert [message["to"] for message in sent] == ["lead@example.org", "second@example.org"]
    assert len({message["body"] for message in sent}) == 1


def test_a_flag_is_announced_with_its_reason_and_what_happens_next(tmp_path):
    message = _run(tmp_path, "second@example.org", FLAG)[0]
    assert message["subject"] == ("MalAvi: MALAVI-SUB-0000-000003: flagged for further "
                                  "review on the form (second@example.org)")
    body = message["body"]
    assert body.startswith("[this is an automatic email]")
    assert "second@example.org recorded this on the MalAvi verdict form" in body
    assert "re-read the trace" in body                     # the reason, in full
    assert "What did you check: Proposed names, Sequences" in body
    # It reports the entry; it does not claim the entry counted.
    assert "takes effect when the ledger next reads the form" in body
    # Without a status sheet nothing can go to the submitter, and the body says so
    # rather than implying somebody else will write.
    assert "No message to the submitter was written" in body
    assert "not on the list of curators" not in body


def test_an_accept_says_nothing_about_flags_or_rejections(tmp_path):
    answers = [["Submission id", "MALAVI-SUB-0000-000001"], ["Revision", "2"],
               ["What are you recording?", "Record a verdict on a submission"],
               ["Your verdict on this revision", "Accept"], ["Why?", "All checks pass."]]
    message = _run(tmp_path, "lead@example.org", answers)[0]
    assert "accepted on the form" in message["subject"]
    assert "A flag blocks" not in message["body"]
    assert "Reject is for" not in message["body"]


def test_the_other_routes_are_named_by_the_form_s_own_words(tmp_path):
    answers = [["Submission id", "MALAVI-SUB-0000-000002"], ["Revision", "1"],
               ["What are you recording?", "Withdraw a flag you placed yourself"],
               ["Which flag are you withdrawing?", "V1"]]
    message = _run(tmp_path, "lead@example.org", answers)[0]
    assert message["subject"] == ("MalAvi: MALAVI-SUB-0000-000002: Withdraw a flag you "
                                  "placed yourself (lead@example.org)")
    assert "Which flag are you withdrawing: V1" in message["body"]


def test_an_entry_from_an_address_off_the_list_goes_to_the_first_address_only(tmp_path):
    """Anyone with the form's link can submit it. What they typed must not be mailed to
    every curator under MalAvi's name; the first address sees it and says so."""
    sent = _run(tmp_path, "stranger@example.org", FLAG)
    assert [message["to"] for message in sent] == ["lead@example.org"]
    assert "stranger@example.org is not on the list of curators" in sent[0]["body"]


def test_one_bad_address_does_not_stop_the_others(tmp_path):
    """The send to the first curator throws; the second is still told, and the failure
    is reported to the first address by the trigger's own handler."""
    bouncing = HARNESS.replace(
        "GmailApp: { sendEmail: (to, subject, body, options) => sent.push({ to, subject, body, options }) },",
        "GmailApp: { sendEmail: (to, subject, body, options) => {"
        " if (to === 'lead@example.org' && !subject.includes('FAILED')) throw new Error('bounced');"
        " sent.push({ to, subject, body, options }); } },")
    sent = _run_full(tmp_path, "second@example.org", FLAG, harness=bouncing)["sent"]
    assert [message["to"] for message in sent] == ["second@example.org", "lead@example.org"]
    assert sent[1]["subject"] == "MalAvi: verdict notice FAILED"
    assert "bounced" in sent[1]["body"]


def test_with_no_curators_set_the_owner_is_told_the_notice_failed(tmp_path):
    """CURATORS unset (property missing or misspelled) must not be a silent no-op: the
    notifier throws, the trigger's catch mails the account that owns the script."""
    sent = _run(tmp_path, "second@example.org", FLAG, curators=())
    assert [message["to"] for message in sent] == ["owner@example.org"]
    assert "FAILED" in sent[0]["subject"]
    assert "CURATORS is not set" in sent[0]["body"]


def test_an_empty_optional_answer_is_left_out(tmp_path):
    answers = FLAG[:5] + [["What did you check?", []]]
    assert "What did you check" not in _run(tmp_path, "lead@example.org", answers)[0]["body"]


# --------------------------------------------------------------------------------------
# With a status sheet: the Verdicts tab, the summary, and the message to the submitter
# --------------------------------------------------------------------------------------

SUBMISSIONS_HEADER = ["id", "received", "submitter_name", "submitter_email", "assignee",
                      "assigned_at", "report_url", "report_at", "revision", "state",
                      "waiting_on", "waiting_since", "hold_ends", "embargoed", "names",
                      "verdicts", "last_from_submitter", "last_to_submitter",
                      "last_reminder", "synced_at", "notes"]


def _sheet_with(submission_id="MALAVI-SUB-0000-000003", email="jane@example.edu",
                assignee="lead@example.org"):
    row = {h: "" for h in SUBMISSIONS_HEADER}
    row.update({"id": submission_id, "submitter_name": "Jane Smith",
                "submitter_email": email, "assignee": assignee,
                "report_url": "https://drive.google.com/file/d/R/view", "state": "in_review"})
    return {"Submissions": [SUBMISSIONS_HEADER, [row[h] for h in SUBMISSIONS_HEADER]],
            "Curators": [["name", "email", "role", "active", "synced_at", "away_until",
                          "max_open"],
                         ["Lead Person", "lead@example.org", "lead", "yes", "", "", ""],
                         ["Second Person", "second@example.org", "curator", "yes", "", "", ""]]}


FLAG_WITH_MESSAGE = FLAG + [["Message to the submitter",
                             "Could you re-read the chromatogram at positions 240 and 324?"]]


def _rows(table):
    header, *rows = table
    return [dict(zip(header, row)) for row in rows if any(v != "" for v in row)]


def test_a_flag_with_a_message_emails_the_submitter_verbatim_with_the_curators_in_copy(tmp_path):
    result = _run_full(tmp_path, "second@example.org", FLAG_WITH_MESSAGE, sheet=_sheet_with(),
                       update_form_url="https://forms.example/update")
    to_submitter = [m for m in result["sent"] if m["to"] == "jane@example.edu"]
    assert len(to_submitter) == 1
    message = to_submitter[0]
    assert message["subject"] == ("MalAvi submission MALAVI-SUB-0000-000003: a question from "
                                  "the curators")
    assert "Dear Jane," in message["body"]
    assert "    Could you re-read the chromatogram at positions 240 and 324?" in message["body"]
    assert "https://forms.example/update" in message["body"]
    assert "Second Person, for the MalAvi curators" in message["body"]
    # The "Why?" (internal) text never reaches the submitter.
    assert "re-read the trace" not in message["body"]
    assert set(message["options"]["cc"].split(",")) == {"second@example.org", "lead@example.org"}
    assert message["options"]["name"] == "MalAvi"
    # The curators' notice says it went.
    notice = [m for m in result["sent"] if m["to"] == "lead@example.org"][0]
    assert "The message to the submitter was sent to jane@example.edu as written" in notice["body"]


def test_the_sheet_records_the_verdict_the_summary_and_the_send(tmp_path):
    result = _run_full(tmp_path, "second@example.org", FLAG_WITH_MESSAGE, sheet=_sheet_with())
    verdicts = _rows(result["sheet"]["Verdicts"])
    assert len(verdicts) == 1
    assert verdicts[0]["id"] == "MALAVI-SUB-0000-000003"
    assert verdicts[0]["curator"] == "second@example.org"
    assert verdicts[0]["verdict"] == "Flag for further review"
    assert verdicts[0]["internal_note"].startswith("EXAMPL01 carries two bases")
    assert verdicts[0]["message_to_submitter"].startswith("Could you re-read")
    assert verdicts[0]["sent_to_submitter_at"] == "2026-10-02T15:04:00Z"
    submissions = _rows(result["sheet"]["Submissions"])
    assert submissions[0]["verdicts"] == "flagged by Second 2026-10-02"
    assert submissions[0]["last_to_submitter"] == "2026-10-02T15:04:00Z"
    mail = _rows(result["sheet"]["Mail"])
    assert mail[0]["direction"] == "out" and mail[0]["to"] == "jane@example.edu"


def test_an_accept_with_a_message_sends_nothing_to_the_submitter_and_says_why(tmp_path):
    answers = [["Submission id", "MALAVI-SUB-0000-000003"], ["Revision", "1"],
               ["What are you recording?", "Record a verdict on a submission"],
               ["Your verdict on this revision", "Accept"], ["Why?", "fine"],
               ["Message to the submitter", "Thanks, all good."]]
    result = _run_full(tmp_path, "second@example.org", answers, sheet=_sheet_with())
    assert [m["to"] for m in result["sent"]] == ["lead@example.org", "second@example.org"]
    assert "NOT sent: only a flag sends it" in result["sent"][0]["body"]
    assert _rows(result["sheet"]["Verdicts"])[0]["sent_to_submitter_at"] == ""


def test_a_reject_with_a_message_sends_nothing_to_the_submitter(tmp_path):
    answers = [["Submission id", "MALAVI-SUB-0000-000003"], ["Revision", "1"],
               ["What are you recording?", "Record a verdict on a submission"],
               ["Your verdict on this revision", "Reject"], ["Why?", "not avian"],
               ["Message to the submitter", "This is not an avian parasite."]]
    result = _run_full(tmp_path, "second@example.org", answers, sheet=_sheet_with())
    assert all(m["to"] != "jane@example.edu" for m in result["sent"])


def test_a_stranger_s_flag_message_never_reaches_the_submitter(tmp_path):
    """Anyone with the link can submit the form. Their text must not be relayed to a
    submitter under MalAvi's name."""
    result = _run_full(tmp_path, "stranger@example.org", FLAG_WITH_MESSAGE, sheet=_sheet_with())
    assert [m["to"] for m in result["sent"]] == ["lead@example.org"]
    assert "NOT sent: the entry came from an address that is not on the curator list" in \
        result["sent"][0]["body"]


def test_a_flag_without_an_address_on_the_sheet_tells_the_curators_to_write(tmp_path):
    result = _run_full(tmp_path, "second@example.org", FLAG_WITH_MESSAGE,
                       sheet=_sheet_with(email=""))
    assert all(m["to"] in ("lead@example.org", "second@example.org") for m in result["sent"])
    assert "NOT sent: the status sheet has no address" in result["sent"][0]["body"]


def test_a_flag_without_a_message_and_a_sheet_asks_the_flagging_curator_to_write(tmp_path):
    result = _run_full(tmp_path, "second@example.org", FLAG, sheet=_sheet_with())
    assert all(m["to"] != "jane@example.edu" for m in result["sent"])
    assert "No message to the submitter was written" in result["sent"][0]["body"]


def test_the_question_added_to_the_live_form_matches_the_builder_and_the_notifier():
    """One title in three files: the builder (future rebuilds), the add-on script (the live
    form) and the notifier (reads it). Reworded in one place, the message silently stops."""
    builder = FORM_BUILDER.read_text(encoding="utf-8")
    add_on = ADD_QUESTION.read_text(encoding="utf-8")
    notifier = NOTIFIER.read_text(encoding="utf-8")
    title = re.search(r"var SUBMITTER_MESSAGE_TITLE = '([^']+)';", add_on).group(1)
    assert f".setTitle('{title}')" in builder
    fragment = re.search(r"var TITLE_MESSAGE = '([^']+)';", notifier).group(1)
    assert fragment in title.lower()
    # And the Python side reads the same column.
    from malavi_curation import verdicts
    assert verdicts.COL_MESSAGE == title


# --------------------------------------------------------------------------------------
# The strings shared with the form builder
# --------------------------------------------------------------------------------------

def _declared(script: str, name: str) -> str:
    found = re.search(rf"var {name} = '([^']*)';", script)
    assert found, f"{name} is not declared"
    return found.group(1)


def test_the_verdict_words_are_the_form_builder_s_verdicts():
    """A verdict reworded on the form must be reworded here, or subjects go generic."""
    builder = FORM_BUILDER.read_text(encoding="utf-8")
    notifier = NOTIFIER.read_text(encoding="utf-8")
    verdicts = {_declared(builder, name)
                for name in ("VERDICT_APPROVE", "VERDICT_HOLD", "VERDICT_DECLINE")}
    block = re.search(r"var VERDICT_WORDS = \{(.*?)\};", notifier, flags=re.DOTALL)
    assert block, "VERDICT_WORDS is not declared in notify_on_verdict.gs"
    assert set(re.findall(r"'([^']+)':", block.group(1))) == verdicts
    assert _declared(notifier, "VERDICT_FLAG") == _declared(builder, "VERDICT_HOLD")
    assert _declared(notifier, "VERDICT_REJECT") == _declared(builder, "VERDICT_DECLINE")


def test_the_titles_the_subject_reads_exist_on_the_form():
    builder = FORM_BUILDER.read_text(encoding="utf-8")
    notifier = NOTIFIER.read_text(encoding="utf-8")
    titles = [title.lower() for title in re.findall(r"\.setTitle\('([^']*)'\)", builder)]
    for name in ("TITLE_SUBMISSION_ID", "TITLE_ACTION", "TITLE_VERDICT"):
        fragment = _declared(notifier, name)
        assert any(fragment in title for title in titles), (
            f"{name} = {fragment!r} matches no question title in create_verdict_form.gs")


def test_the_published_file_carries_no_live_values():
    """The file is published. Since 2026-10-14 every live value is a Script Property read
    through settings.gs; since 2026-10-16 nothing is read at a file's top level (the files
    share one project and load in an order nobody controls), so the file declares what it
    needs in a SETTINGS_NEEDED_<NAME> global and reads through the accessors."""
    notifier = NOTIFIER.read_text(encoding="utf-8")
    assert not re.search(r"(?m)^var \w+ = setting(List)?_\(", notifier), (
        "a setting read at top level; use an accessor at the point of use")
    needed = re.search(r"var SETTINGS_NEEDED_VERDICT = \[(.*?)\];", notifier, flags=re.DOTALL)
    assert needed and set(re.findall(r"'([^']+)'", needed.group(1))) == {
        "CURATORS", "VERDICT_FORM_ID", "STATUS_SHEET_ID", "UPDATE_FORM_URL"}
    assert "PASTE_" not in notifier
    code = re.sub(r"(?m)^\s*(//|\*|/\*\*).*$", "", notifier)
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", code), (
        "an email address in the code, not in a comment")


# How each setting may be read: the accessor functions in settings.gs, or the raw readers.
READERS = {
    "CURATORS": r"curators_\(\)|onCuratorList|settingList_\('CURATORS'\)|requireSetting_\('CURATORS'\)",
    "ATTENTION_TO": r"attentionTo_\(\)|settingList_\('ATTENTION_TO'\)",
    "ARRIVAL_TO": r"arrivalTo_\(\)",
    "ASSIGNEES": r"assignees_\(\)",
    "STATUS_SHEET_ID": r"statusSheetEnabled_\(\)|setting_\('STATUS_SHEET_ID'\)|requireSetting_\('STATUS_SHEET_ID'\)",
}

RUNTIME_SCRIPTS = ("publish_report.gs", "notify_on_submission.gs", "notify_on_verdict.gs",
                   "create_update_form.gs", "inspect_verdict_form.gs", "status_sheet.gs",
                   "inbox_triage.gs", "daily_clock.gs", "add_submitter_message_question.gs",
                   "triggers.gs")


def test_every_script_reads_its_live_values_from_script_properties():
    """The same rule for every file of the project: no PASTE_ placeholder, no top-level
    read, a SETTINGS_NEEDED_<NAME> list whose entries settings.gs knows and the file
    actually reads (through an accessor or a raw reader)."""
    settings = (SCRIPTS / "settings.gs").read_text(encoding="utf-8")
    known = set(re.findall(r"^  ([A-Z_]+): '", settings, flags=re.M))
    for name in RUNTIME_SCRIPTS:
        script = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "PASTE_" not in script, name
        assert not re.search(r"(?m)^var \w+ = setting(List)?_\(", script), (
            f"{name} reads a setting at top level")
        assert not re.search(r"(?m)^var SETTINGS_NEEDED = ", script), (
            f"{name} uses the old SETTINGS_NEEDED name; suffix it")
        if name == "triggers.gs":
            continue                      # installs by setting name; declares nothing
        needed = re.search(r"var SETTINGS_NEEDED_\w+ = \[(.*?)\];", script, flags=re.DOTALL)
        assert needed, f"{name} declares no SETTINGS_NEEDED_<NAME>"
        for setting in re.findall(r"'([^']+)'", needed.group(1)):
            assert setting in known, f"{name} needs {setting}, unknown to settings.gs"
            pattern = READERS.get(setting, rf"setting(List)?_\('{setting}'\)|requireSetting_\('{setting}'\)")
            assert re.search(pattern, script), f"{name} lists {setting} but never reads it"


def test_no_function_is_declared_twice_across_the_project():
    """In one Apps Script project the last definition silently wins. The bundle builder
    refuses such a clash; this says the same thing without running it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_bundle", SCRIPTS / "build_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sources = {name: (SCRIPTS / name).read_text(encoding="utf-8") for name in module.RUNTIME_FILES}
    assert module.duplicate_functions(sources) == {}


def test_the_bundle_is_current():
    """dist/malavi_curation.gs is what gets pasted; a stale one ships yesterday's code."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("build_bundle", SCRIPTS / "build_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.DIST.read_text(encoding="utf-8") == module.build(), (
        "rebuild with: .venv/bin/python curation/apps_script/build_bundle.py")
