/**
 * The private status sheet: where every submission stands, who owns it, and what was said.
 *
 * WHY THIS EXISTS
 * ---------------
 * Until 2026-10-16 the only record of what a submission was waiting on lived in the review
 * ledger on BIOMIX, and the only record of who had written to a submitter lived in one
 * person's inbox. Reports went to five people and nobody owned them; 000007 and 000009
 * each sat a week with no verdict (results/PLAN_SUBMISSION_SYSTEM_RETHINK_2026-10-14.md,
 * section 2). The curators agreed on 2026-10-15/16 to one assigned reviewer per
 * submission, a two-week clock, and reminders at 7 and 14 days.
 *
 * This file is the shared memory those pieces need. One private Google Sheet, "MalAvi
 * submissions", owned by malaviadmin and shared Viewer with each curator, with these tabs:
 *
 *   Submissions  one row per submission: id, submitter, ASSIGNEE, report, the state and
 *                "waiting on" as last synced from the ledger, verdicts so far, when the
 *                submitter last wrote and was last written to, when the last reminder went
 *   Verdicts     one row per verdict-form response, written the moment it is submitted
 *   Mail         every message in or out that the project saw: inbox triage and sends
 *   Arrivals     one row per submission-form response, before it has an id
 *   Curators     the registry (names, addresses, roles), synced from config/curators.yml
 *   Log          every automatic action, so "why did I get this email" has an answer
 *
 * WHO WRITES WHAT
 * ---------------
 *   notify_on_submission.gs   Arrivals row, on the form trigger
 *   publish_report.gs         Submissions row (report link, submitter, ASSIGNMENT) when a
 *                             report is published; the whole Submissions tab's state
 *                             columns and the Curators tab on the `status` action, which
 *                             the intake cycle sends after every run
 *   notify_on_verdict.gs      Verdicts row, the verdict summary on the Submissions row,
 *                             and the message to the submitter when a flag carries one
 *   inbox_triage.gs           Mail rows and last_from_submitter
 *   daily_clock.gs            last_reminder, Log rows
 *
 * THE SHEET IS A VIEW, NOT THE LEDGER
 * ----------------------------------
 * The rules (which verdict counts, when a hold elapses, which names are reserved) stay in
 * curation/src/malavi_curation/ledger.py. The sheet shows what the ledger last said and
 * what happened in Google since. Where they disagree the ledger is right, and the
 * `synced_at` column says how old the sheet's copy is. The assignee, the mail log and the
 * reminders exist ONLY here: they are Google-side bookkeeping the ledger never sees.
 *
 * PRIVACY
 * -------
 * The sheet carries submitter names and addresses and a verdict's internal note. Every
 * curator already receives all of that by email, so the sheet adds no exposure, but it
 * must never be shared by link and never made public. It is NOT the encrypted board on
 * the public site (status_board.py), which carries no names; that board stays as the
 * curators' password-only view and may be retired later.
 *
 * NOTHING HAPPENS UNTIL STATUS_SHEET_ID IS SET
 * -------------------------------------------
 * Every writer below checks statusSheetEnabled_() first and returns quietly when the
 * property is empty, so the notifiers and the publisher work exactly as before until the
 * sheet exists. Run createStatusSheet() once (as malaviadmin) to make it; it prints the id
 * to put into the STATUS_SHEET_ID Script Property.
 *
 * DATES ARE TEXT
 * --------------
 * Every timestamp is written as an ISO 8601 UTC string into cells formatted as plain
 * text ('@'). Sheets would otherwise parse them into its own date type in the
 * spreadsheet's time zone, and a value read back would no longer compare with what the
 * ledger wrote.
 */

var SETTINGS_NEEDED_STATUS_SHEET = ['STATUS_SHEET_ID', 'ASSIGNEES', 'CURATORS'];

var STATUS_SHEET_TITLE = 'MalAvi submissions';

var TAB_SUBMISSIONS = 'Submissions';
var TAB_VERDICTS = 'Verdicts';
var TAB_MAIL = 'Mail';
var TAB_ARRIVALS = 'Arrivals';
var TAB_CURATORS = 'Curators';
var TAB_LOG = 'Log';

// Column headers, by tab. Everything is addressed by header name, never by letter, so a
// column added by hand at the right end breaks nothing and a header renamed here is a
// visible diff.
var TAB_HEADERS = {};
TAB_HEADERS[TAB_SUBMISSIONS] = [
  'id', 'received', 'submitter_name', 'submitter_email',
  'assignee', 'assigned_at',
  'report_url', 'report_at', 'revision',
  'state', 'waiting_on', 'waiting_since', 'hold_ends', 'embargoed', 'names',
  'verdicts',
  'last_from_submitter', 'last_to_submitter', 'last_reminder',
  'synced_at', 'notes'
];
TAB_HEADERS[TAB_VERDICTS] = [
  'recorded_at', 'id', 'curator', 'action', 'verdict',
  'internal_note', 'message_to_submitter', 'sent_to_submitter_at', 'answers'
];
TAB_HEADERS[TAB_MAIL] = [
  'at', 'direction', 'id', 'from', 'to', 'subject', 'thread_url', 'message_id', 'note',
  // Added 2026-10-16 by mail.gs: what kind of message, the mode it was sent (or held)
  // under, and its duplicate-check key. statusTab_ appends missing headers on first use.
  'kind', 'mode', 'key'
];
TAB_HEADERS[TAB_ARRIVALS] = [
  'form_timestamp', 'submitter_name', 'submitter_email', 'stage', 'files', 'matched_id'
];
// name..synced_at are written by the ledger sync (publish_report.gs statusSync_); the
// last two are edited BY HAND on the sheet and the sync never touches them:
//   away_until  a date (YYYY-MM-DD); assignment skips this curator until then
//   max_open    a number; assignment skips this curator while they hold that many open
//               submissions (Rust's triagebot "capacity" and GitHub's "Busy" flag; see
//               results/RESEARCH_curated_db_practices_2026-10-16.md, item 3)
TAB_HEADERS[TAB_CURATORS] = ['name', 'email', 'role', 'active', 'synced_at',
                             'away_until', 'max_open'];
TAB_HEADERS[TAB_LOG] = ['at', 'function', 'message'];

// The ledger's live states (ledger.py LIVE_STATES). A row whose state is one of these, or
// still blank because the ledger has not synced yet, is "open" for the rotation and the
// clock. Kept in step by hand; the ledger is the authority.
var LIVE_STATES = ['received', 'screening_failed', 'ready_for_review', 'in_review', 'held',
                   'awaiting_submitter', 'approved'];

// How a verdict word is shown on the sheet (the form's labels are long).
var SHEET_VERDICT_WORDS = {
  'Accept': 'accepted',
  'Flag for further review': 'flagged',
  'Reject': 'rejected'
};


// ---------------------------------------------------------------------------------------
// Access
// ---------------------------------------------------------------------------------------

/** True when a status sheet is configured. Every writer checks this first. */
function statusSheetEnabled_() {
  return !!setting_('STATUS_SHEET_ID');
}


/** The spreadsheet. Throws a readable sentence if the id is unset or not openable. */
function statusSpreadsheet_() {
  var id = requireSetting_('STATUS_SHEET_ID');
  try {
    return SpreadsheetApp.openById(id);
  } catch (err) {
    throw new Error('STATUS_SHEET_ID does not open as a spreadsheet this account can ' +
                    'read: ' + err);
  }
}


/** ISO 8601 UTC, to the second, for every timestamp this project writes. */
function isoNow_(date) {
  return Utilities.formatDate(date || new Date(), 'UTC', "yyyy-MM-dd'T'HH:mm:ss'Z'");
}


/** Parse one of our ISO strings (or anything Date understands) to a Date, or null. */
function parseStamp_(text) {
  if (!text) return null;
  if (Object.prototype.toString.call(text) === '[object Date]') return text;
  var parsed = new Date(String(text));
  return isNaN(parsed.getTime()) ? null : parsed;
}


/** Any readable stamp re-emitted in this project's one format (ISO UTC, 'Z'); '' when
 *  unreadable. The ledger writes '+00:00', the sheet writes 'Z'; compared as strings they
 *  must agree (review 2026-10-16, finding 9). */
function normStamp_(text) {
  var parsed = parseStamp_(text);
  return parsed ? isoNow_(parsed) : '';
}


/** Whole days between two stamps, as a number; null when either is unreadable. */
function daysBetween_(earlier, later) {
  var a = parseStamp_(earlier);
  var b = parseStamp_(later || new Date());
  if (!a || !b) return null;
  return Math.floor((b.getTime() - a.getTime()) / 86400000);
}


/**
 * A tab by name, created with its header row if missing. A header present in TAB_HEADERS
 * but absent from the sheet is appended at the right end, so adding a column here never
 * requires touching the live sheet by hand; a column somebody added by hand is left alone.
 */
function statusTab_(name) {
  var spreadsheet = statusSpreadsheet_();
  var sheet = spreadsheet.getSheetByName(name);
  var wanted = TAB_HEADERS[name];
  if (!wanted) throw new Error('no such status tab: ' + name);
  if (!sheet) {
    sheet = spreadsheet.insertSheet(name);
    // Everything as text, so dates and ids stay exactly the strings that were written.
    sheet.getRange(1, 1, sheet.getMaxRows(), Math.max(wanted.length, sheet.getMaxColumns()))
         .setNumberFormat('@');
    sheet.getRange(1, 1, 1, wanted.length).setValues([wanted]).setFontWeight('bold');
    sheet.setFrozenRows(1);
    return sheet;
  }
  var lastColumn = Math.max(sheet.getLastColumn(), 1);
  var present = sheet.getRange(1, 1, 1, lastColumn).getValues()[0]
      .map(function (h) { return String(h).trim(); });
  var missing = wanted.filter(function (h) { return present.indexOf(h) === -1; });
  if (missing.length) {
    // Appended after the LAST used column, not after the count of non-empty headers: a
    // header somebody cleared in the middle must not make a new header land on a column
    // that still holds data (review 2026-10-16, finding 17).
    var start = lastColumn + 1;
    sheet.getRange(1, start, 1, missing.length).setValues([missing]).setFontWeight('bold');
    sheet.getRange(1, start, sheet.getMaxRows(), missing.length).setNumberFormat('@');
  }
  return sheet;
}


/** The header row of a tab as an array of names, with their 1-based column numbers. */
function tabColumns_(sheet) {
  var lastColumn = Math.max(sheet.getLastColumn(), 1);
  var headers = sheet.getRange(1, 1, 1, lastColumn).getValues()[0];
  var columns = {};
  for (var i = 0; i < headers.length; i++) {
    var name = String(headers[i]).trim();
    if (name && !(name in columns)) columns[name] = i + 1;
  }
  return columns;
}


/**
 * Every data row of a tab as an object keyed by header, each with `_row` (its 1-based
 * sheet row). Values come back as strings, trimmed.
 */
function readTable_(name) {
  var sheet = statusTab_(name);
  var columns = tabColumns_(sheet);
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return [];
  var width = Math.max(sheet.getLastColumn(), 1);
  var values = sheet.getRange(2, 1, lastRow - 1, width).getValues();
  var rows = [];
  for (var r = 0; r < values.length; r++) {
    var row = { _row: r + 2 };
    var empty = true;
    for (var header in columns) {
      var cell = values[r][columns[header] - 1];
      var text = cell === null || cell === undefined ? '' : String(cell).trim();
      if (Object.prototype.toString.call(cell) === '[object Date]') text = isoNow_(cell);
      row[header] = text;
      if (text) empty = false;
    }
    if (!empty) rows.push(row);
  }
  return rows;
}


/** The first row of `name` whose `column` equals `value`, or null. */
function findRow_(name, column, value) {
  var wanted = String(value || '').trim();
  if (!wanted) return null;
  var rows = readTable_(name);
  for (var i = 0; i < rows.length; i++) {
    if (rows[i][column] === wanted) return rows[i];
  }
  return null;
}


/** Write the given fields (by header name) into one existing row. Unknown headers are
 *  refused loudly rather than dropped: a typo here would otherwise lose data silently. */
function writeFields_(name, rowNumber, fields) {
  var sheet = statusTab_(name);
  var columns = tabColumns_(sheet);
  var width = Math.max(sheet.getLastColumn(), 1);
  // One read and one write for the whole row: a call per cell was 26 round trips per
  // submission and would outrun the caller's timeout (review 2026-10-16, finding 3).
  var range = sheet.getRange(rowNumber, 1, 1, width);
  var values = range.getValues()[0];
  for (var header in fields) {
    if (!(header in columns)) throw new Error(name + ' has no column ' + header);
    var value = fields[header];
    values[columns[header] - 1] = (value === null || value === undefined) ? '' : String(value);
  }
  range.setNumberFormat('@').setValues([values]);
}


/**
 * Create or update MANY Submissions rows in one pass: the tab is read once, existing rows
 * are rewritten one range each, and all new rows go down in a single setValues. For the
 * ledger sync, which touches every row every time. `rowsById` maps id -> fields (same
 * rules as upsertSubmission_: undefined fields are left alone). Returns { written, created }.
 */
function bulkUpsertSubmissions_(rowsById) {
  var sheet = statusTab_(TAB_SUBMISSIONS);
  var columns = tabColumns_(sheet);
  var width = Math.max(sheet.getLastColumn(), 1);
  var lastRow = sheet.getLastRow();
  var existing = lastRow >= 2 ? sheet.getRange(2, 1, lastRow - 1, width).getValues() : [];
  var idColumn = columns.id;
  if (!idColumn) throw new Error('Submissions has no id column');

  var rowIndexById = {};
  for (var r = 0; r < existing.length; r++) {
    var cellId = String(existing[r][idColumn - 1] || '').trim();
    if (cellId && !(cellId in rowIndexById)) rowIndexById[cellId] = r;
  }

  var written = 0, created = 0;
  var appended = [];
  for (var id in rowsById) {
    var fields = rowsById[id];
    if (id in rowIndexById) {
      var index = rowIndexById[id];
      var values = existing[index];
      var changed = false;
      for (var header in fields) {
        if (fields[header] === undefined) continue;
        if (!(header in columns)) throw new Error(TAB_SUBMISSIONS + ' has no column ' + header);
        var text = fields[header] === null ? '' : String(fields[header]);
        if (String(values[columns[header] - 1]) !== text) { values[columns[header] - 1] = text; changed = true; }
      }
      if (changed) {
        sheet.getRange(index + 2, 1, 1, width).setNumberFormat('@').setValues([values]);
      }
      written += 1;
    } else {
      var fresh = [];
      for (var i = 0; i < width; i++) fresh.push('');
      fresh[idColumn - 1] = id;
      for (var h in fields) {
        if (fields[h] === undefined) continue;
        if (!(h in columns)) throw new Error(TAB_SUBMISSIONS + ' has no column ' + h);
        fresh[columns[h] - 1] = fields[h] === null ? '' : String(fields[h]);
      }
      appended.push(fresh);
      written += 1;
      created += 1;
    }
  }
  if (appended.length) {
    sheet.getRange(lastRow + 1, 1, appended.length, width).setNumberFormat('@').setValues(appended);
  }
  return { written: written, created: created };
}


/**
 * Run `work` holding the project's script lock, so two executions (a form trigger, the
 * endpoint, the clock) cannot both append a row for the same id (review 2026-10-16,
 * finding 12). Waits up to 30 s; a lock that cannot be had is an error the caller sees.
 * Not re-entrant: call it at the entry point of an action, never inside a helper.
 */
function withSheetLock_(work) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    return work();
  } finally {
    lock.releaseLock();
  }
}


/** Append one row built from fields by header name. Returns the new row number. */
function appendRow_(name, fields) {
  var sheet = statusTab_(name);
  var columns = tabColumns_(sheet);
  var width = Math.max(sheet.getLastColumn(), 1);
  var values = [];
  for (var i = 0; i < width; i++) values.push('');
  for (var header in fields) {
    if (!(header in columns)) throw new Error(name + ' has no column ' + header);
    var value = fields[header];
    values[columns[header] - 1] = (value === null || value === undefined) ? '' : String(value);
  }
  var rowNumber = sheet.getLastRow() + 1;
  sheet.getRange(rowNumber, 1, 1, width).setNumberFormat('@').setValues([values]);
  return rowNumber;
}


/**
 * Create or update the Submissions row for one id. Returns { row, created }. Fields that
 * are undefined are not touched, so a caller that knows only the report link does not
 * blank the assignee.
 */
function upsertSubmission_(submissionId, fields) {
  var id = String(submissionId || '').trim();
  if (!id) throw new Error('upsertSubmission_ needs a submission id');
  var existing = findRow_(TAB_SUBMISSIONS, 'id', id);
  var clean = {};
  for (var header in fields) {
    if (fields[header] !== undefined) clean[header] = fields[header];
  }
  if (existing) {
    writeFields_(TAB_SUBMISSIONS, existing._row, clean);
    return { row: existing._row, created: false };
  }
  clean.id = id;
  return { row: appendRow_(TAB_SUBMISSIONS, clean), created: true };
}


/** Append a Log row and echo it to the execution log. Never throws: a failed log line
 *  must not stop the action it describes. */
function logEvent_(functionName, message) {
  Logger.log(functionName + ': ' + message);
  if (!statusSheetEnabled_()) return;
  try {
    appendRow_(TAB_LOG, { at: isoNow_(), 'function': functionName, message: message });
  } catch (err) {
    Logger.log('could not write the Log tab: ' + err);
  }
}


/** Record one message, in or out, on the Mail tab. Returns the row number, or 0 when
 *  the write failed or there is no sheet. Never throws. */
function recordMail_(fields) {
  if (!statusSheetEnabled_()) return 0;
  try {
    var row = { at: isoNow_() };
    for (var key in fields) row[key] = fields[key];
    return appendRow_(TAB_MAIL, row);
  } catch (err) {
    Logger.log('could not write the Mail tab: ' + err);
    return 0;
  }
}


/**
 * Send one email through the account and record it on the Mail tab. Every message the
 * project sends to a SUBMITTER goes through here, so the Submissions row can say when
 * they were last written to. Options are passed to GmailApp.sendEmail unchanged, with the
 * sender name defaulting to "MalAvi".
 */
var AUTOMATIC_FOOTER = '(Sent by the MalAvi curation system; a reply reaches the curators.)';

function sendAndRecord_(to, subject, body, options, submissionId, note) {
  // The footer marks the message as ours, so the inbox triage's outgoing pass does not
  // record it a second time as "written by hand" (review 2026-10-16, finding 11). The
  // send itself, and the Mail row, are mail.gs's: mode, duplicate check, caps.
  return sendMail_({ to: to, subject: subject, body: body + '\n\n' + AUTOMATIC_FOOTER,
                     options: options || {}, kind: 'submitter', submissionId: submissionId,
                     note: note || '' });
}


// ---------------------------------------------------------------------------------------
// Names
// ---------------------------------------------------------------------------------------

/** Address -> display name from the Curators tab; the address itself when unknown.
 *  Read once per execution. */
var curatorNameCache_ = null;

function curatorName_(address) {
  var wanted = String(address || '').trim().toLowerCase();
  if (!wanted) return '';
  if (curatorNameCache_ === null) {
    curatorNameCache_ = {};
    if (statusSheetEnabled_()) {
      try {
        var rows = readTable_(TAB_CURATORS);
        for (var i = 0; i < rows.length; i++) {
          if (rows[i].email) curatorNameCache_[rows[i].email.toLowerCase()] = rows[i].name;
        }
      } catch (err) {
        Logger.log('could not read the Curators tab: ' + err);
      }
    }
  }
  return curatorNameCache_[wanted] || address;
}


/** The first (given) name, for a friendly subject line; the whole name if it is one word. */
function firstName_(address) {
  var name = curatorName_(address);
  if (name === address) return address;
  return name.split(/\s+/)[0];
}


// ---------------------------------------------------------------------------------------
// Assignment
// ---------------------------------------------------------------------------------------

/**
 * Who gets the next submission.
 *
 * Least-loaded first: the assignee with the fewest OPEN rows (state blank or live). Ties
 * go to whoever comes next in the ASSIGNEES list after the most recent assignment, so with
 * equal loads it is a plain rotation. A curator whose Curators-tab row says away_until a
 * future date, or whose open count has reached their max_open, is skipped; if that leaves
 * nobody, the skips are ignored rather than leaving a submission unowned. `excluding` is a
 * list of addresses to leave out (the curator being relieved, on reassignment). Nothing
 * here decides who is a curator (config/curators.yml does); it only picks who is asked
 * first.
 */
function nextAssignee_(excluding) {
  var pool = assignees_();
  var skipList = (excluding || []).map(function (a) { return String(a).toLowerCase(); });
  pool = pool.filter(function (a) { return skipList.indexOf(a.toLowerCase()) === -1; });
  if (!pool.length) pool = assignees_();        // better the same person than nobody
  if (!pool.length) throw new Error('ASSIGNEES and CURATORS are both empty; nobody to assign');
  var lower = pool.map(function (a) { return a.toLowerCase(); });
  var limits = curatorLimits_();

  var open = {};
  var latestAt = '';
  var latestWho = '';
  for (var p = 0; p < lower.length; p++) open[lower[p]] = 0;

  var rows = readTable_(TAB_SUBMISSIONS);
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    var who = (row.assignee || '').toLowerCase();
    if (!who) continue;
    var isOpen = !row.state || LIVE_STATES.indexOf(row.state) !== -1;
    if (isOpen && who in open) open[who] += 1;
    if (row.assigned_at && row.assigned_at > latestAt) {
      latestAt = row.assigned_at;
      latestWho = who;
    }
  }

  // Who is available today: not away, and under their own limit.
  var today = isoNow_().slice(0, 10);
  var available = [];
  for (var v = 0; v < lower.length; v++) {
    var limit = limits[lower[v]] || {};
    if (limit.away_until && limit.away_until >= today) continue;
    if (limit.max_open !== null && limit.max_open !== undefined &&
        open[lower[v]] >= limit.max_open) continue;
    available.push(v);
  }
  if (!available.length) {
    for (var w = 0; w < lower.length; w++) available.push(w);   // everyone is away: ignore it
  }

  var fewest = Infinity;
  for (var q = 0; q < available.length; q++) {
    fewest = Math.min(fewest, open[lower[available[q]]]);
  }

  // Walk the list starting just after the last person assigned, take the first available
  // curator with the minimum load.
  var start = latestWho ? (lower.indexOf(latestWho) + 1) % lower.length : 0;
  if (start < 0) start = 0;
  for (var step = 0; step < lower.length; step++) {
    var candidate = (start + step) % lower.length;
    if (available.indexOf(candidate) !== -1 && open[lower[candidate]] === fewest) {
      return pool[candidate];
    }
  }
  return pool[available[0]];
}


/** Per-curator limits from the Curators tab: address -> { away_until, max_open }. */
function curatorLimits_() {
  var limits = {};
  try {
    var rows = readTable_(TAB_CURATORS);
    for (var i = 0; i < rows.length; i++) {
      if (!rows[i].email) continue;
      var max = String(rows[i].max_open || '').trim();
      limits[rows[i].email.toLowerCase()] = {
        away_until: String(rows[i].away_until || '').trim().slice(0, 10),
        max_open: /^\d+$/.test(max) ? parseInt(max, 10) : null
      };
    }
  } catch (err) {
    Logger.log('could not read curator limits: ' + err);
  }
  return limits;
}


/**
 * Assign a submission if it has no assignee yet. Returns the assignee's address (existing
 * or new) and whether this call made the assignment. Writes nothing when the sheet is off.
 */
function assignSubmission_(submissionId, fields) {
  if (!statusSheetEnabled_()) return { assignee: '', assigned: false };
  var row = findRow_(TAB_SUBMISSIONS, 'id', submissionId);
  if (row && row.assignee) return { assignee: row.assignee, assigned: false };
  var chosen = nextAssignee_();
  var extra = fields || {};
  extra.assignee = chosen;
  extra.assigned_at = isoNow_();
  upsertSubmission_(submissionId, extra);
  logEvent_('assignSubmission_', submissionId + ' assigned to ' + chosen);
  return { assignee: chosen, assigned: true };
}


/**
 * HAND FUNCTION. Move a submission to another curator: set the two values in the editor's
 * argument-free way by editing the two lines below, run, then put them back; or call it
 * from another function. The new assignee is emailed; the old one and the leads are told.
 */
function reassign() {
  var SUBMISSION_ID = 'MALAVI-SUB-0000-EDITME';
  var NEW_ASSIGNEE = 'someone@example.org';
  reassignSubmission_(SUBMISSION_ID, NEW_ASSIGNEE, 'reassigned by hand');
}


function reassignSubmission_(submissionId, newAssignee, why) {
  var address = String(newAssignee || '').trim();
  var known = curators_().concat(assignees_()).map(function (a) { return a.toLowerCase(); });
  if (known.indexOf(address.toLowerCase()) === -1) {
    throw new Error(address + ' is in neither CURATORS nor ASSIGNEES; add them there (and ' +
                    'to config/curators.yml) first');
  }
  var row = findRow_(TAB_SUBMISSIONS, 'id', submissionId);
  if (!row) throw new Error('no Submissions row for ' + submissionId);
  var previous = row.assignee;
  writeFields_(TAB_SUBMISSIONS, row._row, { assignee: address, assigned_at: isoNow_(),
                                            last_reminder: '' });
  logEvent_('reassignSubmission_', submissionId + ': ' + (previous || 'nobody') + ' -> ' +
            address + ' (' + (why || 'no reason given') + ')');

  var subject = 'MalAvi: ' + submissionId + ' is now assigned to you';
  var body = [
    '[this is an automatic email]',
    '',
    'Submission ' + submissionId + ' has been reassigned to you' +
      (previous ? ' (it was with ' + curatorName_(previous) + ')' : '') + '.',
    '',
    row.report_url ? 'Report: ' + row.report_url : 'The report link is on the status sheet.',
    '',
    'The two-week clock restarts today. Record your verdict on the form linked from the ' +
      'report; the other curators remain in copy.',
    '',
    '--', 'Confidential. A submission may contain unpublished sequences.'
  ].join('\n');
  var stamp = isoNow_();
  sendMail_({ to: address, subject: subject, body: body, kind: 'curator',
              submissionId: submissionId, key: 'reassign|' + submissionId + '|' + stamp,
              note: 'reassigned (' + (why || 'no reason given') + ')' });

  var told = attentionTo_().slice();
  if (previous && told.indexOf(previous) === -1) told.push(previous);
  for (var i = 0; i < told.length; i++) {
    if (told[i].toLowerCase() === address.toLowerCase()) continue;
    sendMail_({ to: told[i], subject: 'MalAvi: ' + submissionId + ' reassigned to ' +
                curatorName_(address),
                body: '[this is an automatic email]\n\n' + submissionId + ' is now assigned to ' +
                      curatorName_(address) + (previous ? ', previously ' + curatorName_(previous) : '') +
                      '. ' + (why || '') + '\n',
                kind: 'curator', submissionId: submissionId,
                key: 'reassign-told|' + submissionId + '|' + stamp, note: 'reassignment notice' });
  }
}


// ---------------------------------------------------------------------------------------
// Verdict summary on the Submissions row
// ---------------------------------------------------------------------------------------

/** Rebuild the one-line verdict summary for a submission from the Verdicts tab. */
function verdictSummary_(submissionId) {
  var rows = readTable_(TAB_VERDICTS);
  var parts = [];
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].id !== submissionId || !rows[i].verdict) continue;
    parts.push((SHEET_VERDICT_WORDS[rows[i].verdict] || rows[i].verdict) + ' by ' +
               firstName_(rows[i].curator) + ' ' + String(rows[i].recorded_at).slice(0, 10));
  }
  return parts.join('; ');
}


// ---------------------------------------------------------------------------------------
// Setup and inspection (run by hand)
// ---------------------------------------------------------------------------------------

/**
 * Create the status spreadsheet, once. Refuses if STATUS_SHEET_ID is already set, so it
 * cannot make a second sheet by accident. Prints the id to put into the property, and
 * shares the sheet Viewer with every address in CURATORS (never by link).
 */
function createStatusSheet() {
  if (setting_('STATUS_SHEET_ID')) {
    throw new Error('STATUS_SHEET_ID is already set (' + setting_('STATUS_SHEET_ID') +
                    '). To start over, clear the property first.');
  }
  var curators = curators_();
  if (!curators.length) throw new Error('set CURATORS first, so the sheet can be shared');

  var spreadsheet = SpreadsheetApp.create(STATUS_SHEET_TITLE);
  var id = spreadsheet.getId();
  // Set the property NOW so statusTab_ can find the sheet while we build the tabs.
  PropertiesService.getScriptProperties().setProperty('STATUS_SHEET_ID', id);
  var names = [TAB_SUBMISSIONS, TAB_VERDICTS, TAB_MAIL, TAB_ARRIVALS, TAB_CURATORS, TAB_LOG];
  for (var i = 0; i < names.length; i++) statusTab_(names[i]);
  var first = spreadsheet.getSheets()[0];
  if (first.getName() !== TAB_SUBMISSIONS && names.indexOf(first.getName()) === -1) {
    spreadsheet.deleteSheet(first);          // the default "Sheet1"
  }
  for (var c = 0; c < curators.length; c++) {
    try {
      spreadsheet.addViewer(curators[c]);
    } catch (err) {
      Logger.log('could not share with ' + curators[c] + ': ' + err);
    }
  }
  logEvent_('createStatusSheet', 'created; shared Viewer with ' + curators.length +
            ' curator(s)');
  Logger.log('Created "' + STATUS_SHEET_TITLE + '": ' + spreadsheet.getUrl());
  Logger.log('STATUS_SHEET_ID has been set to ' + id + ' in this project\'s Script ' +
             'Properties. Record it in CUSTODY_PRIVATE.md. Next: run installAllTriggers.');
}


/** Print the Submissions tab, one line each, newest first. Sends nothing. */
function showStatusSheet() {
  if (!statusSheetEnabled_()) { Logger.log('STATUS_SHEET_ID is not set.'); return; }
  var rows = readTable_(TAB_SUBMISSIONS);
  Logger.log(rows.length + ' submission row(s); sheet ' + statusSpreadsheet_().getUrl());
  for (var i = rows.length - 1; i >= 0; i--) {
    var r = rows[i];
    Logger.log([r.id, r.state || '(not synced)', 'waiting on ' + (r.waiting_on || '?'),
                'assigned ' + (r.assignee ? curatorName_(r.assignee) : 'nobody'),
                r.verdicts || 'no verdicts', 'synced ' + (r.synced_at || 'never')]
               .join(' | '));
  }
}
