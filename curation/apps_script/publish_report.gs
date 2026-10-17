/**
 * Receive a curator report from BIOMIX, put it in Drive, and tell the curators.
 *
 * WHY THIS EXISTS RATHER THAN A SERVICE ACCOUNT
 * ---------------------------------------------
 * The plan was for a service account to upload the PDF straight to Drive. It was tested
 * before being built and it cannot work:
 *
 *     HTTP 403  storageQuotaExceeded
 *     "Service Accounts do not have storage quota. Leverage shared drives, or use
 *      OAuth delegation instead."
 *
 * A file created by a service account is owned by that service account no matter which
 * folder it goes in, and a service account has no Drive storage. Shared Drives would fix
 * it and need Google Workspace; malaviadmin@gmail.com is a consumer account. So the write
 * has to be performed by a human account, and this script runs as one.
 *
 * It also removes a wart from the original design. That version needed a second script on
 * a timer to notice new files and email about them, so a report could sit unannounced for
 * up to an hour. Here the write and the email are one request.
 *
 * THE SECURITY POSITION, STATED HONESTLY
 * --------------------------------------
 * This must be deployed "Anyone can access", because BIOMIX has no Google credential to
 * present. The endpoint is therefore public, and the shared secret is all that stands in
 * front of it. That is weaker than a key and it is a deliberate trade, so the blast radius
 * is kept small:
 *
 *   - Every request must carry an HMAC-SHA256 signature over the exact body, in ?sig=.
 *     Nothing is parsed or written before that is verified.
 *   - The signed body carries a timestamp; anything older than MAX_AGE_SECONDS is refused,
 *     so a captured request is not a permanent key.
 *   - The script can do exactly one thing: write a PDF into REPORTS_FOLDER_ID and email
 *     the curators. It never reads a submission, never lists another folder, and has no
 *     path that returns Drive contents to the caller.
 *
 * Somebody who steals the secret can put a PDF in the reports folder and cause an email.
 * That is recoverable. They cannot read submitter data, which is the thing worth guarding.
 *
 * INSTALLING IT
 * -------------
 *   1. Sign in as malaviadmin@gmail.com. Create the Drive folder that will hold curator
 *      reports. Share it as Viewer with each curator's address. Do NOT link-share it:
 *      undoing link-sharing was the entire point of the 2026-08-06 change.
 *   2. script.google.com > New project, name it "MalAvi report publisher". Paste this
 *      file in, and paste settings.gs in as a second file (Files > "+" > Script,
 *      named "settings").
 *   3. Generate a secret on BIOMIX:  openssl rand -hex 32
 *      Save it there at ~/.config/malavi/report_secret.txt (chmod 600).
 *   4. Project Settings (gear) > Script Properties > Add: REPORTS_FOLDER_ID (the folder
 *      id out of its URL), SHARED_SECRET (the same value as on BIOMIX), CURATORS and
 *      ATTENTION_TO (comma-separated addresses, matching config/curators.yml). No live
 *      value is ever written in this file.
 *   5. Run `showSettings` until it says READY.
 *   6. Run `testSetup` once. It checks the folder and the secret without needing a
 *      request, and emails you a sample notification so you can see what a curator gets.
 *      Authorize when asked.
 *   7. Deploy > New deployment > Web app. Execute as: **Me**. Who has access: **Anyone**.
 *      Copy the /exec URL into google.report_endpoint in config/project.yml.
 *   8. From BIOMIX:  curation/publish_report.py --check
 *      then:         curation/publish_report.py <submission_id>
 *
 * THE PERMISSION THAT DOES NOT AUTO-DETECT
 * ----------------------------------------
 * Apps Script normally works out which permissions a script needs by reading it. It gets
 * this one wrong, and the way it fails is nasty: the FIRST publish of any report works,
 * because creating a file uses DriveApp, and every publish AFTERWARDS fails, because
 * replacing a file's contents uses UrlFetchApp -- which needs
 * https://www.googleapis.com/auth/script.external_request, and that scope does not get
 * requested. So the failure appears the first time somebody corrects a report, which could
 * be months later, and the error names a permission rather than a cause:
 *
 *     You do not have permission to call UrlFetchApp.fetch
 *
 * The cure is to stop letting it guess. curation/apps_script/appsscript.json in this
 * repository lists the three scopes this script actually needs; paste it over the manifest
 * in the Apps Script project (Project Settings > "Show appsscript.json manifest file in
 * editor"), then run any function once to re-authorize, then push a new version of the
 * deployment. Found the hard way on 2026-08-08, on the second publish.
 *
 * RE-DEPLOYING
 * ------------
 * Editing this file changes nothing on its own. Deploy > Manage deployments > edit the
 * existing deployment > Version: New version. Creating a *new* deployment instead gives a
 * new URL and quietly leaves the old one running the old code, which is the single easiest
 * way to spend an afternoon debugging a change that was never live.
 *
 * Since 2026-10-14 pasting this file over the live one is safe: the live values are
 * Script Properties, not code. Paste, run showSettings (READY), then push a new version.
 *
 * ONE PROJECT, AND WHAT WAS ADDED ON 2026-10-16
 * ---------------------------------------------
 * This project now also holds the other .gs files of this directory (settings.gs explains
 * the merge). Two things changed here:
 *   - When a report is published for the first time, the submission is ASSIGNED to one
 *     curator in rotation (status_sheet.gs). The assignee's email says "needs your
 *     verdict"; the others are told who has it and that they are in copy. The payload may
 *     carry the submitter's name and address and the revision, which go onto the status
 *     sheet so the verdict trigger and the inbox triage can reach the submitter. A
 *     corrected report (action "updated") keeps its assignee.
 *   - A `status` action: the intake cycle sends the ledger's view of every submission and
 *     the curator registry after each run (curation/push_status.py), and this writes them
 *     to the status sheet. Without STATUS_SHEET_ID both are no-ops and the publisher
 *     behaves as it did before.
 * No value is read from Script Properties at the top of this file any more: every read
 * is a function call at the moment of use (see LOAD ORDER in settings.gs).
 */

// ---------------------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------------------

// The Drive folder that holds curator reports. Shared as Viewer with each curator.
// The live value is "MalAvi curator reports" under malaviadmin@gmail.com, created
// 2026-08-08, and it is recorded in CUSTODY_PRIVATE.md rather than here. A folder id is
// not a credential -- without permission on the folder it gets you a 404 -- but this file
// is published, and printing the id would mean that any future mistake in the folder's
// sharing settings is instantly exploitable by anyone who read the repository.
// Since 2026-10-14 every live value is a Script Property (settings.gs, pasted into this
// project as a second file); this code holds none, so pasting it over the live script
// blanks nothing. Set them under Project Settings > Script Properties and run
// showSettings(), then testSetup().
var SETTINGS_NEEDED_PUBLISHER = ['REPORTS_FOLDER_ID', 'SHARED_SECRET', 'CURATORS',
                                 'ATTENTION_TO', 'STATUS_SHEET_ID'];
function reportsFolderId_() { return setting_('REPORTS_FOLDER_ID'); }

// Must equal the contents of ~/.config/malavi/report_secret.txt on BIOMIX.
function sharedSecret_() { return setting_('SHARED_SECRET'); }

// Who is told when a report is ready: curators_() in settings.gs (the CURATORS Script
// Property). Keep in step with config/curators.yml, which is the record of who is a
// curator; the list only decides who gets told.

// Must match MAX_AGE_SECONDS in curation/src/malavi_curation/report_delivery.py.
var MAX_AGE_SECONDS = 600;

// Apps Script will happily accept a huge body and then fail obscurely. Refuse early.
var MAX_PDF_BYTES = 8 * 1024 * 1024;

// How long this script remembers that a name confirmation went out, so that the same one
// is not mailed twice. The retry that matters arrives the next day (BIOMIX records the
// send only after the reply, so a request that timed out after the mail had gone is sent
// again by the next run); 30 days covers a run that was not made for weeks.
var CONFIRMATION_MEMORY_DAYS = 30;
var CONFIRMATION_KEY_PREFIX = 'confirmed:';

// The date of the last change to what this script DOES, and every action it answers.
//
// These exist for one question that had no answer before 2026-10-02: "is the deployed
// script the one in the repository?" Editing this file changes nothing until a new version
// of the deployment is pushed (see RE-DEPLOYING above), and nothing reported which code
// was live, so a change could sit undeployed for weeks while the Python side believed it
// was in service. The `describe` action returns both values to a signed request, and
// `curation/publish_report.py --check --live` compares them with this file.
//
// Change CODE_VERSION whenever an action is added or its behavior changes. A request for
// an action missing from the deployed script falls through to the PDF branch and is
// refused there ("unusable filename") without writing or emailing anything, which is how
// the Python side recognizes a deployment older than `describe` itself.
var CODE_VERSION = '2026-10-16-mailguard';
var ACTIONS = ['publish_report', 'confirm_names', 'decline_notice', 'verdict_notice',
               'attention_notice', 'describe', 'status'];

// Who is told when an unattended run of the intake cycle needs a person
// (curation/intake_cycle.py). The lead curators, not every curator: the message is
// program output and can name a submitter or quote a response that could not be parsed.
// The ATTENTION_TO Script Property, read through attentionTo_() in settings.gs.
var MAX_ATTENTION_BODY = 20000;


// ---------------------------------------------------------------------------------------
// The endpoint
// ---------------------------------------------------------------------------------------

/**
 * Handle one publish request.
 *
 * Every failure returns JSON with ok:false and a reason the Python side can print. It
 * never throws out of this function: an uncaught error in a web app returns an HTML error
 * page, which the caller cannot parse and which tells whoever is debugging nothing.
 */
function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return reply_({ ok: false, error: 'empty request body' });
    }
    var body = e.postData.contents;

    // 0. No secret configured means no request can be genuine. Refuse before any
    //    signature arithmetic: an HMAC over an empty key is still a valid HMAC, and a
    //    freshly pasted project with its Script Properties not yet set must not accept
    //    anything from anyone.
    if (!sharedSecret_()) {
      return reply_({ ok: false, error: 'endpoint not configured (SHARED_SECRET unset)' });
    }

    // 1. Signature, before anything else touches the body.
    var provided = (e.parameter && e.parameter.sig) || '';
    if (!provided) {
      return reply_({ ok: false, error: 'no signature' });
    }
    if (!signatureValid_(body, provided)) {
      return reply_({ ok: false, error: 'bad signature' });
    }

    // 2. Only now is the body worth parsing.
    var payload;
    try {
      payload = JSON.parse(body);
    } catch (err) {
      return reply_({ ok: false, error: 'body is not JSON' });
    }

    // 3. Freshness, so a captured request cannot be replayed.
    var age = Math.abs((Date.now() / 1000) - Number(payload.issued_at || 0));
    if (!payload.issued_at || age > MAX_AGE_SECONDS) {
      return reply_({ ok: false, error: 'stale request (age ' + Math.round(age) + 's)' });
    }

    // 3b. The request may ask for a MORE restrictive mail mode than the project's own
    //     (the maintainer reading the mail himself); never a less restrictive one, and
    //     never an address: mail.gs takes the redirect address from MAIL_REDIRECT_TO only.
    requestMailMode_(payload.mail_mode);

    // 4. Which of the two jobs is this? Routing happens only after the signature and the
    //    freshness check, so an unsigned request never reaches either branch.
    var action = String(payload.action || 'publish_report');
    if (action === 'confirm_names') {
      return reply_(withMailStatus_(confirmNames_(payload)));
    }
    if (action === 'decline_notice') {
      return reply_(withMailStatus_(declineNotice_(payload)));
    }
    if (action === 'verdict_notice') {
      return reply_(withMailStatus_(verdictNotice_(payload)));
    }
    if (action === 'attention_notice') {
      return reply_(withMailStatus_(attentionNotice_(payload)));
    }
    if (action === 'describe') {
      return reply_(describe_());
    }
    if (action === 'status') {
      return reply_(statusSync_(payload));
    }

    // 5. The file itself.
    var filename = String(payload.filename || '');
    if (!filename || filename.indexOf('/') !== -1 || filename.slice(-4) !== '.pdf') {
      return reply_({ ok: false, error: 'unusable filename' });
    }
    var bytes = Utilities.base64Decode(payload.pdf_b64 || '');
    if (!bytes.length) {
      return reply_({ ok: false, error: 'empty PDF' });
    }
    if (bytes.length > MAX_PDF_BYTES) {
      return reply_({ ok: false, error: 'PDF too large (' + bytes.length + ' bytes)' });
    }

    // Verify the content matches what was signed. The signature covers the base64, so this
    // is belt and braces -- but it is the check that catches a truncated upload, which is
    // the failure that would otherwise put a half-rendered report in front of a curator.
    var digest = hex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes));
    if (payload.sha256 && payload.sha256 !== digest) {
      return reply_({ ok: false, error: 'checksum mismatch' });
    }

    // A report nobody would be told about must not be written: the Python side records
    // a publish as announced, and --all-pending never sends it again. (Review of
    // 2026-10-14: with the lists in Script Properties, an unset or misspelled CURATORS
    // is an empty list, and an empty loop used to look like success.)
    if (payload.notify && !curators_().length) {
      return reply_({ ok: false, error: 'CURATORS is not set (Script Properties); ' +
                                        'nothing written, nobody emailed' });
    }

    var blob = Utilities.newBlob(bytes, 'application/pdf', filename);
    var written = writeOrUpdate_(filename, blob);

    // 5a. The status sheet: the report link, the submitter, and -- for a first report --
    //     the assignment. A sheet failure must not undo a publish that already happened,
    //     so it is caught and reported in the reply; the report is announced either way.
    var submissionId = String(payload.submission_id || '');
    var assignment = { assignee: '', assigned: false, error: '' };
    try {
      assignment = statusSheetEnabled_()
          ? withSheetLock_(function () { return recordReport_(submissionId, payload, written); })
          : recordReport_(submissionId, payload, written);
    } catch (err) {
      assignment.error = String(err && err.message ? err.message : err);
      Logger.log('status sheet not updated for ' + submissionId + ': ' + assignment.error);
    }

    // 5b. Tell the curators, unless the caller asked us not to (a re-send during debugging).
    var notified = 0;
    if (payload.notify) {
      notified = notifyCurators_(submissionId, written, assignment.assignee,
                                 String(payload.mail_key || ''), payload.announced_before);
    }

    return reply_(withMailStatus_({
      ok: true,
      fileId: written.id,
      url: written.url,
      action: written.action,
      sha256: digest,
      notified: notified,
      // What became of the notice: emailed, already_sent (every recipient had it), held
      // (the mode, a cap or the quota), or none (notify was false).
      notice: !payload.notify ? 'none' : noticeOutcome_(notified),
      assignee: assignment.assignee,
      assigned: assignment.assigned,
      sheet_error: assignment.error
    }));

  } catch (err) {
    // Anything unforeseen still comes back as JSON, with enough to act on.
    return reply_({ ok: false, error: 'unhandled: ' + (err && err.message ? err.message : err) });
  }
}


/**
 * Add what the mail guard did in this execution to a reply: how many messages were
 * sent, which were held and why, and the mode in force. The Python side raises MailHeld
 * on any held message that is not a duplicate, so nothing is recorded as sent that was
 * not (review of 2026-10-16, finding 5).
 */
function withMailStatus_(result) {
  if (result && result.ok) {
    result.sent = mailRunCount_;
    result.held = mailRunHeld_.slice();
    result.mail_mode = mailMode_();
  }
  return result;
}

/** 'emailed' when something was sent, 'already_sent' when everything held was a
 *  duplicate, 'held' otherwise. */
function noticeOutcome_(sent) {
  if (sent > 0) return 'emailed';
  if (!mailRunHeld_.length) return 'held';
  for (var i = 0; i < mailRunHeld_.length; i++) {
    if (mailRunHeld_[i].reason !== 'duplicate') return 'held';
  }
  return 'already_sent';
}


/**
 * Tell the lead curators that the intake cycle needs a person.
 *
 * The same rule as verdictNotice_: the text comes from the maintainer's side, which is
 * where the programs ran, and THE RECIPIENTS DO NOT. They are ATTENTION_TO, so the secret's
 * holder still cannot use this endpoint to send mail to anyone of their choosing.
 */
function attentionNotice_(payload) {
  var subject = String(payload.subject || '').trim();
  var body = String(payload.body || '').trim();
  if (!subject || !body) {
    return { ok: false, error: 'an attention notice needs both a subject and a body' };
  }
  if (body.length > MAX_ATTENTION_BODY + 200) {
    return { ok: false, error: 'attention notice too long (' + body.length + ' characters)' };
  }
  var leads = settingList_('ATTENTION_TO');
  if (!leads.length) {
    return { ok: false, error: 'ATTENTION_TO is not set (Script Properties); nobody emailed' };
  }
  var sent = 0;
  for (var i = 0; i < leads.length; i++) {
    var result = sendMail_({ to: leads[i], subject: subject, body: body, kind: 'lead',
                             key: String(payload.mail_key || ''), note: 'attention notice' });
    if (result.sent) sent += 1;
  }
  return { ok: true, action: noticeOutcome_(sent), notified: sent };
}


/**
 * Say which code is deployed. Reads nothing, writes nothing and emails nobody.
 *
 * `curators` and `attention_to` are how many addresses the live CURATORS and ATTENTION_TO
 * properties hold. Counts and never the addresses: enough to see that appointing a curator reached this script (the second
 * of the three grants in RUNBOOK section 3ca) without the endpoint ever listing who they
 * are.
 */
function describe_() {
  return {
    ok: true,
    action: 'described',
    code_version: CODE_VERSION,
    actions: ACTIONS,
    curators: curators_().length,
    attention_to: settingList_('ATTENTION_TO').length,
    status_sheet: statusSheetEnabled_(),
    // The mail guard (mail.gs, 2026-10-16): the mode in force and today's held count, so
    // the intake cycle can say before sending anything whether it would be sent.
    mail_mode: mailMode_(),
    mail_mode_explicit: mailModeIsExplicit_(),
    held_today: mailHeldToday_()
  };
}


// ---------------------------------------------------------------------------------------
// The status sheet (status_sheet.gs): assignment at publish time, and the `status` action
// ---------------------------------------------------------------------------------------

/**
 * Put the report on the Submissions row and, for a first report, assign the submission.
 *
 * Returns { assignee, assigned }. With no status sheet configured it returns empty values
 * and touches nothing, so the publisher behaves exactly as before 2026-10-16.
 *
 * The payload's `assign` defaults to true; the Python side sends false when it re-sends a
 * report during debugging (--no-notify) so a test publish does not hand somebody work.
 * An "updated" report (a corrected one) never reassigns: the person who has it keeps it.
 */
function recordReport_(submissionId, payload, written) {
  if (!statusSheetEnabled_() || !submissionId) return { assignee: '', assigned: false };
  var fields = {
    report_url: written.url,
    report_at: isoNow_(),
    submitter_name: payload.submitter_name !== undefined ? String(payload.submitter_name) : undefined,
    submitter_email: payload.submitter_email !== undefined ? String(payload.submitter_email) : undefined,
    received: payload.received_at !== undefined ? String(payload.received_at) : undefined,
    revision: payload.revision !== undefined ? String(payload.revision) : undefined
  };
  var wantAssignment = payload.assign === undefined ? true : !!payload.assign;
  if (wantAssignment && payload.notify) {
    // assignSubmission_ upserts the row with these fields and the assignee together.
    return assignSubmission_(submissionId, fields);
  }
  upsertSubmission_(submissionId, fields);
  var row = findRow_(TAB_SUBMISSIONS, 'id', submissionId);
  return { assignee: row && row.assignee ? row.assignee : '', assigned: false };
}


/**
 * The `status` action: the ledger's view of every submission, and the curator registry,
 * written onto the sheet. Sent by curation/push_status.py at the end of each intake cycle.
 *
 * Only the columns the ledger owns are written (state, waiting_on, waiting_since,
 * hold_ends, embargoed, names, revision, received, submitter, synced_at). The assignee,
 * the mail columns and the reminders are Google-side and are never overwritten here.
 * A row the ledger no longer lists (excluded or purged) is left as it is; its synced_at
 * stops moving, which the daily clock reports.
 */
function statusSync_(payload) {
  if (!statusSheetEnabled_()) {
    return { ok: false, error: 'STATUS_SHEET_ID is not set (Script Properties); nothing ' +
                               'to write to. Run createStatusSheet in the project.' };
  }
  var rows = payload.rows;
  if (Object.prototype.toString.call(rows) !== '[object Array]') {
    return { ok: false, error: 'a status payload needs a rows array' };
  }
  return withSheetLock_(function () { return statusSyncLocked_(rows, payload.curators); });
}


function statusSyncLocked_(rows, curatorRows) {
  var stamp = isoNow_();
  var byId = {};
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i] || {};
    var id = String(r.id || '').trim();
    if (!/^MALAVI-SUB-\d{4}-\d{6}$/.test(id)) continue;      // never write a junk row
    // Every stamp re-emitted in the sheet's one format (normStamp_), so the ledger's
    // '+00:00' and the sheet's 'Z' never sit in the same column.
    byId[id] = {
      received: r.received !== undefined ? normStamp_(r.received) || String(r.received) : undefined,
      submitter_name: r.submitter_name !== undefined ? String(r.submitter_name) : undefined,
      submitter_email: r.submitter_email !== undefined ? String(r.submitter_email) : undefined,
      revision: r.revision !== undefined ? String(r.revision) : undefined,
      state: String(r.state || ''),
      waiting_on: String(r.waiting_on || ''),
      waiting_since: r.waiting_since ? normStamp_(r.waiting_since) : '',
      hold_ends: r.hold_ends ? normStamp_(r.hold_ends) : '',
      embargoed: r.embargoed ? 'yes' : 'no',
      names: r.names !== undefined ? String(r.names) : undefined,
      report_url: r.report_url ? String(r.report_url) : undefined,
      report_at: r.report_at ? normStamp_(r.report_at) : undefined,
      synced_at: stamp
    };
  }
  // One read of the tab, one write per changed row, one write for all new rows.
  var result = bulkUpsertSubmissions_(byId);
  var written = result.written;
  var created = result.created;

  var curatorsWritten = 0;
  if (Object.prototype.toString.call(curatorRows) === '[object Array]' && curatorRows.length) {
    // One row per address, matched by email: the registry columns are rewritten, the
    // hand-edited ones (away_until, max_open) are kept. A curator no longer in the
    // registry keeps their row with active = no.
    var existing = readTable_(TAB_CURATORS);
    var seen = {};
    for (var c = 0; c < curatorRows.length; c++) {
      var person = curatorRows[c] || {};
      var address = String(person.email || '').trim();
      if (!address) continue;
      seen[address.toLowerCase()] = true;
      var fields = { name: String(person.name || ''), email: address,
                     role: String(person.role || ''), active: person.active ? 'yes' : 'no',
                     synced_at: stamp };
      var match = null;
      for (var e = 0; e < existing.length; e++) {
        if ((existing[e].email || '').toLowerCase() === address.toLowerCase()) { match = existing[e]; break; }
      }
      if (match) writeFields_(TAB_CURATORS, match._row, fields);
      else appendRow_(TAB_CURATORS, fields);
      curatorsWritten += 1;
    }
    for (var g = 0; g < existing.length; g++) {
      if (existing[g].email && !seen[existing[g].email.toLowerCase()] && existing[g].active !== 'no') {
        writeFields_(TAB_CURATORS, existing[g]._row, { active: 'no', synced_at: stamp });
      }
    }
    curatorNameCache_ = null;
  }

  logEvent_('statusSync_', written + ' submission row(s) synced (' + created + ' new), ' +
            curatorsWritten + ' curator(s)');
  return { ok: true, action: 'synced', rows: written, created: created,
           curators: curatorsWritten, synced_at: stamp };
}


/**
 * Write the report, replacing the previous version *in place* if there is one.
 *
 * Idempotency is the point. A corrected report must land on the same file id, because the
 * link in a curator's inbox points at that id. Creating a second file would leave the
 * curator reading a superseded report with no indication that it was superseded -- the
 * worst outcome this whole path can produce.
 *
 * DriveApp cannot replace binary content, so the update goes through the Drive REST API
 * with this script's own OAuth token. Creation stays on DriveApp, which is simpler and has
 * no such limitation.
 */
function writeOrUpdate_(filename, blob) {
  var folder = DriveApp.getFolderById(reportsFolderId_());
  var existing = folder.getFilesByName(filename);

  if (existing.hasNext()) {
    var file = existing.next();
    var id = file.getId();
    var response = UrlFetchApp.fetch(
      'https://www.googleapis.com/upload/drive/v3/files/' + id + '?uploadType=media',
      {
        method: 'patch',
        contentType: 'application/pdf',
        payload: blob.getBytes(),
        headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
        muteHttpExceptions: true
      });
    if (response.getResponseCode() !== 200) {
      throw new Error('update failed: HTTP ' + response.getResponseCode() + ' ' +
                      response.getContentText().slice(0, 200));
    }
    return { id: id, url: file.getUrl(), action: 'updated' };
  }

  var created = folder.createFile(blob);
  return { id: created.getId(), url: created.getUrl(), action: 'created' };
}


/**
 * Tell a submitter which lineage names are theirs, once a curator has approved them.
 *
 * WHY THIS IS AUTOMATED AND THE REST OF THE SUBMITTER CORRESPONDENCE IS NOT
 * ------------------------------------------------------------------------
 * The MalAvi site tells every visitor, in the name checker, that a curator will confirm
 * their proposed name and that they should wait for that before depositing in GenBank.
 * That promise was kept by somebody remembering to write an email. A submitter who gives
 * up waiting deposits the wrong name, which is the exact failure the two-stage submission
 * process exists to prevent -- so this one message is worth automating and the rest are
 * not.
 *
 * WHEN IT FIRES, AND WHY NOT SOONER
 * ---------------------------------
 * Only after a curator has approved AND the publish hold has elapsed with no objection
 * standing. Not at screening: the name is derived from the host species, which is the
 * single thing curators most often correct, so a name confirmed by machine could be
 * withdrawn by a person. Not at approval either: the hold exists precisely so a second
 * curator can still object, and a confirmation sent into that window could be retracted.
 * A slow email is recoverable. A retracted lineage name, already in a manuscript, is not.
 *
 * WHAT IT MUST SAY
 * ----------------
 * The granted names, and -- separately and unmissably -- any name that CHANGED from what
 * was proposed. A submitter who proposed TUMIG06 and was granted TUMIG25 has to see that,
 * because the granted name is the one that goes in the paper and in GenBank.
 */
function confirmNames_(payload) {
  var to = String(payload.to || '');
  if (to.indexOf('@') === -1) {
    return { ok: false, error: 'no usable submitter address' };
  }
  var names = payload.names || [];
  if (!names.length) {
    return { ok: false, error: 'no names to confirm' };
  }

  // Sent once. BIOMIX writes its "already told them" record only after this reply
  // arrives, so a request that timed out on its side AFTER the mail had gone is sent
  // again by the next run -- and a second "your names are confirmed" invites the reader
  // to look for a difference that is not there. The key is the submission, the address
  // and the names, which is what makes two requests the same message; see
  // confirmationKey_. Checked before composing anything, and recorded only after the
  // mail has actually gone.
  var key = confirmationKey_(payload);
  if (alreadyConfirmed_(key)) {
    return { ok: true, action: 'already_sent', notified: 0 };
  }

  var corrections = payload.corrections || {};
  var changed = [];
  for (var proposed in corrections) {
    if (corrections.hasOwnProperty(proposed) && corrections[proposed] !== proposed) {
      changed.push('    ' + proposed + '  ->  ' + corrections[proposed]);
    }
  }

  var who = String(payload.submitter_name || '').trim();
  var lines = [
    '[this is an automatic email]',
    '',
    who ? 'Dear ' + who + ',' : 'Hello,',
    '',
    'This is an automatic message from MalAvi, sent once a curator has approved your',
    'submission. Replies go straight to the curators, so if anything below looks wrong,',
    'or you have a question, just reply to this email.',
    '',
    'A MalAvi curator has reviewed your submission and confirmed the lineage names below.',
    'These are the names to use in your manuscript and when you deposit the sequences in',
    'GenBank, so that the same name appears in all three places.',
    '',
    'Confirmed lineage names:'
  ];
  for (var i = 0; i < names.length; i++) {
    lines.push('    ' + names[i]);
  }

  if (changed.length) {
    lines.push('');
    lines.push('PLEASE NOTE -- the following names are NOT the ones you proposed. The name');
    lines.push('you proposed was already in use in MalAvi, so a free one was assigned:');
    lines.push('');
    lines = lines.concat(changed);
    lines.push('');
    lines.push('Use the name on the right.');
  }

  if (payload.reference) {
    lines.push('');
    lines.push('Submission: ' + payload.reference);
  }

  // What happens to the RECORDS, told from what the submitter actually selected rather
  // than as an "if you asked us to..." conditional the reader has to resolve themselves.
  //
  // The two booleans are decided in Python (form_metadata.records_are_held and
  // records_were_included) and arrive already settled. Deciding them here would put an
  // untested branch in charge of whether somebody is told their unpublished records are
  // about to be published.
  var selections = payload.selections || {};
  var stage = String(selections.stage || '').trim();
  var sending = String(selections.sending || '').trim();

  if (stage || sending) {
    lines.push('');
    if (stage && sending) {
      lines.push('You selected: "' + stage + '" data, sending "' + sending + '".');
    } else {
      lines.push('You selected: "' + (stage || sending) + '".');
    }
  }

  lines.push('');
  if (!selections.records_included) {
    // They said they were sending names and sequences only, so the host and geography
    // records are still to come. Saying so is the whole reason this branch exists.
    lines.push('Your host and geography records have not reached MalAvi. Please submit');
    lines.push('those as soon as your study is accepted for publication.');
  } else if (selections.records_held) {
    lines.push('Your host and geography records are held until your study is published,');
    lines.push('as you asked. Let us know when it is accepted and they will go into the');
    lines.push('next MalAvi data release after that.');
  } else {
    lines.push('The records themselves appear in the next MalAvi data release.');
  }

  lines = lines.concat([
    '',
    '--',
    'MalAvi',
    'https://malavi-db.github.io/'
  ]);

  // replyTo is set explicitly rather than left to default to the sending account. The
  // default would work today, but this message tells the reader to reply, and that
  // instruction must not quietly stop being true if the sending alias ever changes.
  // mail.gs applies the mode, its own duplicate memory (keyed on the same identity), the
  // caps and the submitter rule. The older confirmation memory above is consulted for
  // messages sent before 2026-10-16 and no longer written to.
  var result = sendMail_({ to: to,
                           subject: 'MalAvi: your lineage names are confirmed (automatic message)',
                           body: lines.join('\n'),
                           options: { replyTo: 'malaviadmin@gmail.com' },
                           kind: 'submitter', submissionId: String(payload.submission_id || ''),
                           key: key, note: 'name confirmation' });
  if (result.sent) return { ok: true, action: 'emailed', notified: 1 };
  return { ok: true, action: result.held === 'duplicate' ? 'already_sent' : 'held',
           notified: 0 };
}


/**
 * The identity of one name confirmation, for the sent-once check in confirmNames_.
 *
 * An explicit `idempotency_key` in the payload wins, so that BIOMIX can name the message
 * itself without this script changing again. Absent that, the key is derived from what
 * makes two requests the same message: the submission, the address, and the sorted names.
 * It is deliberately NOT built from `issued_at`, which is stamped fresh on every attempt
 * and would therefore never match the retry this check exists to catch. A re-approval
 * after a revision that changed a name produces a different key and is sent, which is
 * right: it is a different message.
 */
function confirmationKey_(payload) {
  var explicit = String(payload.idempotency_key || '').trim();
  if (explicit) {
    return CONFIRMATION_KEY_PREFIX + explicit;
  }
  var names = (payload.names || []).map(function (name) { return String(name); });
  names.sort();
  return CONFIRMATION_KEY_PREFIX +
    String(payload.submission_id || '') + '|' +
    String(payload.to || '').trim().toLowerCase() + '|' +
    names.join(',');
}


/**
 * Has this confirmation gone out within CONFIRMATION_MEMORY_DAYS?
 *
 * Script properties rather than CacheService: the cache is allowed to evict an entry at
 * any time, and an eviction here means a second email. Properties persist until deleted;
 * rememberConfirmation_ prunes the old ones so they do not accumulate forever.
 */
function alreadyConfirmed_(key) {
  var sentAt = PropertiesService.getScriptProperties().getProperty(key);
  if (!sentAt) {
    return false;
  }
  var ageMs = Date.now() - Number(sentAt);
  return ageMs >= 0 && ageMs < CONFIRMATION_MEMORY_DAYS * 24 * 3600 * 1000;
}


/**
 * Record that a confirmation was mailed, and forget any older than the memory window.
 *
 * Called only after GmailApp.sendEmail returned: a key recorded before the send would,
 * on a failed send, suppress the retry that is supposed to fix it.
 */
function rememberConfirmation_(key) {
  var properties = PropertiesService.getScriptProperties();
  var now = Date.now();
  var cutoff = now - CONFIRMATION_MEMORY_DAYS * 24 * 3600 * 1000;
  var all = properties.getProperties();
  for (var name in all) {
    if (all.hasOwnProperty(name) && name.indexOf(CONFIRMATION_KEY_PREFIX) === 0 &&
        Number(all[name]) < cutoff) {
      properties.deleteProperty(name);
    }
  }
  properties.setProperty(key, String(now));
}


/**
 * Tell a submitter their submission was not accepted, and point them at a person.
 *
 * WHY THIS CARRIES NO REASON
 * --------------------------
 * A curator's written reasoning quotes the submission and is usually a judgment about the
 * data. Put in an automatic message it becomes an assertion nobody can answer: the reader
 * has a question the moment they finish reading, and the sender cannot respond. So this
 * says what happened, says it plainly, and hands the conversation to a human -- which is
 * also the honest description of what a decline actually is at this stage.
 *
 * WHY IT EXISTS AT ALL
 * --------------------
 * The alternative is silence. A submitter who hears nothing does not conclude "declined";
 * they conclude "lost", and they wait, and their reserved names sit claimed while they do.
 * Being told is better than being left to infer, even when the news is unwelcome.
 *
 * TONE IS DELIBERATE
 * ------------------
 * Not accepted "in its current form", and an explicit invitation to write back. Most
 * declines at this stage are fixable -- a workbook that could not be read, records that
 * could not be reconciled with the paper -- and a submitter who reads this as a permanent
 * verdict is a contributor MalAvi has lost for no reason.
 */
/**
 * Tell the other curators that somebody recorded a verdict.
 *
 * Until this existed, a verdict was visible only to whoever next ran fetch_verdicts on
 * BIOMIX. A hold was invisible to the curator it was blocking, and an override was
 * invisible to the curator whose objection had just been set aside -- which is the one
 * person entitled to know.
 *
 * The body is composed on the maintainer's side, because that is where the ledger is: the
 * verdict id, the reason, the resulting state and the link a reader should follow are all
 * things this script has no way to know.
 *
 * BUT THE RECIPIENTS ARE NOT TAKEN FROM THE PAYLOAD. They are CURATORS, minus whoever the
 * payload says performed the act. A signed request that could name its own recipients
 * would turn this endpoint into a mailer for anyone who ever obtained the secret, and the
 * whole argument for the secret being an acceptable risk (see the header) is that its
 * holder can do exactly two harmless things. Sending mail to strangers is not one of them.
 */
function verdictNotice_(payload) {
  var subject = String(payload.subject || '').trim();
  var body = String(payload.body || '').trim();
  if (!subject || !body) {
    return { ok: false, error: 'a verdict notice needs both a subject and a body' };
  }
  var actor = String(payload.actor_email || '').trim().toLowerCase();
  // Who hears it: every curator, or the leads only (the ledger's hold id and clear-hold
  // link are a lead's tools; the form's trigger already told the curators of the flag).
  var audience = String(payload.audience || 'curators') === 'leads' ? 'leads' : 'curators';
  var recipients = audience === 'leads' ? attentionTo_() : curators_();
  if (!recipients.length) {
    return { ok: false, error: (audience === 'leads' ? 'ATTENTION_TO' : 'CURATORS') +
                               ' is not set (Script Properties); nobody emailed' };
  }

  var sent = 0;
  for (var i = 0; i < recipients.length; i++) {
    if (recipients[i].toLowerCase() === actor) {
      continue;                     // nobody needs mail about their own click
    }
    var result = sendMail_({ to: recipients[i], subject: subject, body: body,
                             kind: audience === 'leads' ? 'lead' : 'curator',
                             submissionId: String(payload.submission_id || ''),
                             key: String(payload.mail_key || ''), note: 'verdict notice' });
    if (result.sent) sent += 1;
  }
  return { ok: true, action: noticeOutcome_(sent), notified: sent };
}


function declineNotice_(payload) {
  var to = String(payload.to || '');
  if (to.indexOf('@') === -1) {
    return { ok: false, error: 'no usable submitter address' };
  }
  var who = String(payload.submitter_name || '').trim();

  var lines = [
    '[this is an automatic email]',
    '',
    who ? 'Dear ' + who + ',' : 'Hello,',
    '',
    'This is an automatic message from MalAvi. Replies go straight to the curators, so',
    'please reply to this email with any questions.',
    '',
    'A curator has reviewed your submission, and it has not been accepted into MalAvi in',
    'its current form.',
    '',
    'The most common reason is new sequences that do not pass quality control checks.',
    'A curator can tell you the specific issues.',
    '',
    'Please write back and we will determine if we can work through this with you.'
  ];

  if (payload.reference) {
    lines.push('');
    lines.push('Submission: ' + payload.reference);
  }

  lines = lines.concat([
    '',
    'Any lineage names you proposed are no longer reserved, so they may be assigned to',
    'another submission. If you resubmit, propose them again and a curator will check',
    'whether they are still free.',
    '',
    '--',
    'MalAvi',
    'https://malavi-db.github.io/'
  ]);

  var result = sendMail_({ to: to,
                           subject: 'MalAvi: about your submission (automatic message)',
                           body: lines.join('\n'),
                           options: { replyTo: 'malaviadmin@gmail.com' },
                           kind: 'submitter', submissionId: String(payload.submission_id || ''),
                           key: String(payload.mail_key || ''), note: 'decline notice' });
  if (result.sent) return { ok: true, action: 'emailed', notified: 1 };
  return { ok: true, action: result.held === 'duplicate' ? 'already_sent' : 'held',
           notified: 0 };
}


/**
 * Email the curators that a report is ready.
 *
 * Deliberately short, and deliberately says nothing about what the report found. The
 * report itself carries the disposition line and the prefilled verdict link; an email that
 * summarized the findings would be a second place for them to be wrong, and would put
 * something about an unpublished submission into an inbox that may not be the curator's
 * only device. The subject line does not name the submitter either.
 */
function notifyCurators_(submissionId, written, assignee, mailKey, announcedBefore) {
  // "updated" only when the curators were TOLD of an earlier version. Drive's own
  // created/updated says whether the file existed, which is not the same thing: a first
  // report whose notice was held (mail mode) exists in Drive before anyone heard of it
  // (code review 2026-10-16, finding 10). The Python side says what the ledger knows;
  // an older caller that says nothing falls back to Drive's word.
  var verb = (announcedBefore === true || announcedBefore === false)
      ? (announcedBefore ? 'updated' : 'ready')
      : (written.action === 'updated' ? 'updated' : 'ready');
  var corrected = verb === 'updated';
  var curators = curators_();
  var owner = String(assignee || '').trim();
  var ownerName = owner ? curatorName_(owner) : '';

  // Two subjects when somebody owns it: the assignee is asked for a verdict by name, the
  // others are told who has it. One subject, as before 2026-10-16, when nobody does.
  var subjectForOwner = 'MalAvi: ' + submissionId + ' needs your verdict (curator report ' +
                        verb + ')';
  var subjectForOthers = owner
      ? 'MalAvi: curator report ' + verb + ' (' + submissionId + '; assigned to ' +
        ownerName + ', you are in copy)'
      : 'MalAvi: curator report ' + verb + ' (' + submissionId + ')';

  var lines = [
    '[this is an automatic email]',
    '',
    'A curator report is ' + (corrected
      ? 'available in a corrected version.'
      : 'ready for review.'),
    '',
    'Submission: ' + submissionId,
    'Report:     ' + written.url,
    '',
    // One line per paragraph, not hand-wrapped. A mail client re-wraps to its own
    // width, so a sentence split across two source lines comes out broken at whatever
    // word the author happened to stop at -- "...what was / submitted..." is what that
    // looked like in the first real report email.
    'The report opens in the browser. It carries the automated check results, what was '
      + 'submitted, and a link that records your decision.',
    '',
    'Opening it needs the Google account the reports folder was shared with. If the link '
      + 'says you need access, check which account your browser is signed into.',
    '',
    corrected
      ? 'This replaces the earlier version at the same link. If you already read it, the '
        + 'findings may have changed.'
      : 'Nothing is added to MalAvi without a curator decision.',
    ''
  ];
  if (owner) {
    // One line per paragraph, not hand-wrapped (a mail client re-wraps to its width).
    lines.push('This submission is assigned to ' + ownerName + ', who is asked to record a '
        + 'verdict within two weeks; the others are in copy and may still comment or record '
        + 'a verdict of their own. Reminders go to the assignee after 7 days and to everyone '
        + 'after 14. If you cannot take it, reply to this email and a lead will reassign it.');
    lines.push('');
  }
  lines.push('Confidential: a submission can contain unpublished sequences.');
  var body = lines.join('\n');

  // The key names the report (submission and PDF bytes, from the Python side), so the
  // same report posted twice is announced once and a corrected one is announced again.
  var key = mailKey || ('report|' + submissionId + '|' + written.id + '|' + written.action);
  var sent = 0;
  var ownerTold = false;
  for (var i = 0; i < curators.length; i++) {
    var isOwner = owner && curators[i].toLowerCase() === owner.toLowerCase();
    var result = sendMail_({ to: curators[i],
                             subject: isOwner ? subjectForOwner : subjectForOthers, body: body,
                             kind: 'curator', submissionId: submissionId, key: key,
                             note: 'report ' + verb });
    if (result.sent) sent += 1;
    if (isOwner) ownerTold = true;
  }
  // An assignee who is in ASSIGNEES but not in CURATORS still has to hear about it.
  if (owner && !ownerTold) {
    var ownerResult = sendMail_({ to: owner, subject: subjectForOwner, body: body,
                                  kind: 'curator', submissionId: submissionId, key: key,
                                  note: 'report ' + verb + ' (assignee)' });
    if (ownerResult.sent) sent += 1;
  }
  return sent;
}


// ---------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------

/**
 * Constant-time-ish comparison of the provided signature against the expected one.
 *
 * Apps Script has no constant-time compare. Over HTTPS, against a hex digest, a timing
 * attack is not a practical route in -- but comparing every character rather than
 * returning at the first difference costs nothing, so there is no reason to hand out the
 * information.
 */
function signatureValid_(body, provided) {
  var raw = Utilities.computeHmacSha256Signature(body, sharedSecret_());
  var expected = hex_(raw);
  if (provided.length !== expected.length) {
    return false;
  }
  var difference = 0;
  for (var i = 0; i < expected.length; i++) {
    difference |= (expected.charCodeAt(i) ^ provided.charCodeAt(i));
  }
  return difference === 0;
}


/** Byte array to lowercase hex. Apps Script bytes are signed, hence the & 0xFF. */
function hex_(bytes) {
  var out = '';
  for (var i = 0; i < bytes.length; i++) {
    var b = (bytes[i] & 0xFF).toString(16);
    out += (b.length === 1 ? '0' : '') + b;
  }
  return out;
}


/** Every response is JSON, including every failure. See the note on doPost. */
function reply_(object) {
  return ContentService
    .createTextOutput(JSON.stringify(object))
    .setMimeType(ContentService.MimeType.JSON);
}


// ---------------------------------------------------------------------------------------
// Run this once, by hand, before deploying
// ---------------------------------------------------------------------------------------

/**
 * Force Google to ask for the permissions the web app needs, in the editor.
 *
 * Editing appsscript.json is not always enough on its own: Google decides whether to
 * re-prompt, and if it decides not to, the missing permission stays missing and only shows
 * up as a failure inside the deployed web app -- where there is no way to consent, because
 * a web app request has no user sitting in front of it.
 *
 * This function calls UrlFetchApp for real. Running it from the editor either raises the
 * authorization dialog (which is the point) or succeeds, which proves the permission is
 * already there and the problem is elsewhere. Either way it answers the question, which
 * staring at the manifest does not.
 *
 * The request it makes is deliberately trivial and read-only: it asks Drive who this
 * script is running as.
 */
function forceAuthorization() {
  var response = UrlFetchApp.fetch(
    'https://www.googleapis.com/drive/v3/about?fields=user',
    {
      headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
      muteHttpExceptions: true
    });
  Logger.log('UrlFetchApp is authorized. Drive replied: ' +
             response.getContentText().slice(0, 200));
}


/**
 * Check the configuration without needing a request, and send yourself a sample email.
 *
 * Worth running before the first deployment, because the two most likely mistakes -- an
 * unedited folder id and an unedited secret -- both produce failures at the far end of the
 * pipeline, on BIOMIX, where the cause is invisible.
 */
function testSetup() {
  var problems = [];
  var folderId = reportsFolderId_();
  var secret = sharedSecret_();
  var curators = curators_();

  if (!folderId) {
    problems.push('REPORTS_FOLDER_ID is not set (Project Settings > Script Properties).');
  } else {
    try {
      var folder = DriveApp.getFolderById(folderId);
      Logger.log('Reports folder: ' + folder.getName());
    } catch (err) {
      problems.push('REPORTS_FOLDER_ID is not a folder this account can open: ' + err);
    }
  }

  if (!secret) {
    problems.push('SHARED_SECRET is not set (Project Settings > Script Properties).');
  } else if (secret.length < 32) {
    problems.push('SHARED_SECRET is shorter than 32 characters; generate one with ' +
                  '`openssl rand -hex 32`.');
  }

  if (!curators.length) {
    problems.push('CURATORS is not set, so nobody would be told a report exists.');
  }

  if (!settingList_('ATTENTION_TO').length) {
    problems.push('ATTENTION_TO is not set, so nobody would be told the intake cycle ' +
                  'needs a person.');
  }

  if (!statusSheetEnabled_()) {
    Logger.log('Note: STATUS_SHEET_ID is not set, so reports will not be assigned and ' +
               'nothing is written to a status sheet. Run createStatusSheet to set it up.');
  }

  if (problems.length) {
    Logger.log('NOT READY:\n  ' + problems.join('\n  '));
    return;
  }

  // Show what a curator will actually receive, using a fake submission id. Sent to the
  // LEADS only (ATTENTION_TO), not to every curator: with real curators on the list a
  // test report is noise. The sample names the first lead as the assignee so both subject
  // lines are seen; nothing is written to the status sheet by this test.
  var leads = attentionTo_();
  var sample = { id: 'example', url: 'https://drive.google.com/file/d/EXAMPLE/view',
                 action: 'created' };
  var bodyLines = ['[this is an automatic email]', '',
                   'This is testSetup in the MalAvi curation project: a SAMPLE of the two ' +
                   'report emails (assignee / in copy). No report exists and nothing was ' +
                   'written anywhere.', '', 'Submission: MALAVI-SUB-0000-TEST',
                   'Report:     ' + sample.url];
  var delivered = 0;
  var stamp = String(Date.now());
  for (var L = 0; L < leads.length; L++) {
    var one = sendMail_({ to: leads[L],
                          subject: 'MalAvi: MALAVI-SUB-0000-TEST needs your verdict (curator ' +
                                   'report ready) [SAMPLE]',
                          body: bodyLines.join('\n'), kind: 'lead',
                          key: 'testSetup-owner|' + stamp, note: 'testSetup sample' });
    var two = sendMail_({ to: leads[L],
                          subject: 'MalAvi: curator report ready (MALAVI-SUB-0000-TEST; assigned ' +
                                   'to ' + curatorName_(leads[0]) + ', you are in copy) [SAMPLE]',
                          body: bodyLines.join('\n'), kind: 'lead',
                          key: 'testSetup-copy|' + stamp, note: 'testSetup sample' });
    delivered += (one.sent ? 1 : 0) + (two.sent ? 1 : 0);
  }
  Logger.log('Configuration looks right. ' + delivered + ' of ' + (2 * leads.length) +
             ' sample notifications went to the ' + leads.length + ' lead address(es) in ' +
             'ATTENTION_TO (not to the curators). CURATORS holds ' + curators.length +
             ' address(es). ' + mailStatusLine_());
}
