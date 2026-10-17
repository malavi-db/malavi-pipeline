/**
 * Build the "update a submission" form from code, and its notifier.
 *
 * WHY THIS EXISTS
 * ---------------
 * When a curator flags a submission and writes to the submitter, the answer that comes
 * back is a sentence, or the corrected template attached to an email. Nobody wants to go
 * through the whole submission form again, and they should not: a fresh submission mints
 * a new id and a new date, and the names they reserved would be lost to anyone who claimed
 * them in between. This form takes the answer as an UPDATE to the original submission: its
 * id, what changed, and the corrected template if there is one.
 * curation/fetch_updates.py files it as a new revision of the original (same id, same
 * date, same names) and the intake cycle screens and reports it again.
 *
 * Any curator may also fill this form in on a submitter's behalf, from an emailed answer
 * or an attached spreadsheet. That is the written-down rule, so that it does not default
 * to the maintainer.
 *
 * HOW TO RUN IT
 * -------------
 *   1. Sign in as the MalAvi operational account (malaviadmin@gmail.com). The account that
 *      runs this OWNS the form.
 *   2. script.google.com > New project, paste this file in, name it "MalAvi update form".
 *   3. Run `createUpdateForm`. Authorize when asked (Forms, Sheets).
 *   4. Read View > Logs. Then do every step in `finishByHand()` below: Apps Script cannot
 *      add a file-upload question, and the upload is the point.
 *   5. Put the responses sheet id in config/project.yml as
 *      submissions.update_responses_sheet, and the public URL as
 *      submissions.update_form_url. Share the sheet Viewer with the fetcher's service
 *      account (as the verdict sheet is; curation/GOOGLE_ACCESS.md).
 *   6. Set the CURATORS and UPDATE_FORM_ID Script Properties (settings.gs; run
 *      `showSettings` until READY), run `installUpdateTrigger`, then `showTriggers`.
 *
 * WHAT THE NOTIFIER SAYS
 * ----------------------
 * That an update arrived for submission X, from which address, with the submitter's note
 * and how many files. It goes to every curator; the flag that asked for the update was one
 * of theirs. Nothing is decided here, and the fetch decides nothing either: a revision
 * clears the earlier approvals, and the curators look at the new report.
 */

// The addresses are the CURATORS Script Property (settings.gs, pasted into this project
// as a second file; set under Project Settings > Script Properties; run showSettings).
// No live value is written in this published file.
var SETTINGS_NEEDED_UPDATE_FORM = ['CURATORS', 'UPDATE_FORM_ID'];
// CURATORS is read through curators_() (settings.gs) at the moment it is needed.

// The form's id, for installUpdateTrigger: the UPDATE_FORM_ID Script Property, set from
// createUpdateForm's log; the live value is recorded in CUSTODY_PRIVATE.md, not here.
// UPDATE_FORM_ID is read with setting_('UPDATE_FORM_ID') when the trigger is installed.

// Question titles. curation/fetch_updates.py reads the responses sheet by a lowercase
// substring of these ("submission id", "what changed", "template file"), so a change
// here is a change there.
var TITLE_ID = 'Submission id';
var TITLE_CHANGED = 'What changed?';
var TITLE_FILE = 'Upload the corrected template file (optional)';


function createUpdateForm() {
  var form = FormApp.create('MalAvi — update a submission');
  form.setDescription(
      'For answering a curator\'s question about a submission you already made, without ' +
      'filling in the submission form again. Your submission keeps its id, its date and ' +
      'the lineage names it reserved.\n\n' +
      'Give the submission id from the email you received (it looks like ' +
      'MALAVI-SUB-2026-000123), say what changed, and attach the corrected template if ' +
      'you have one. A curator will look at the new version.');

  setVerifiedEmailCollection(form);
  form.setProgressBar(false);
  form.setAllowResponseEdits(false);     // a second thought is a second update
  form.setLimitOneResponsePerUser(false);

  form.addTextItem()
      .setTitle(TITLE_ID)
      .setHelpText('From the email about your submission. Looks like MALAVI-SUB-2026-000123.')
      .setValidation(FormApp.createTextValidation()
          .requireTextMatchesPattern('^\\s*MALAVI-SUB-\\d{4}-\\d{6}\\s*$')
          .setHelpText('A MalAvi submission id looks like MALAVI-SUB-2026-000123.')
          .build())
      .setRequired(true);

  form.addParagraphTextItem()
      .setTitle(TITLE_CHANGED)
      .setHelpText('In a few words: which lineage, sample or record, and what is different ' +
                   'now. If you attach a corrected template, say what you changed in it.')
      .setRequired(true);

  // The file-upload question is added by hand: see finishByHand().

  var sheet = SpreadsheetApp.create('MalAvi — submission updates (responses)');
  // GMT, no daylight saving: the response time is what orders updates.
  sheet.setSpreadsheetTimeZone('Etc/GMT');
  form.setDestination(FormApp.DestinationType.SPREADSHEET, sheet.getId());

  Logger.log('Form (edit)     : ' + form.getEditUrl());
  Logger.log('Form (public)   : ' + form.getPublishedUrl());
  Logger.log('Form id         : ' + form.getId());
  Logger.log('Responses sheet : ' + sheet.getId());
  Logger.log('Sheet timezone  : ' + sheet.getSpreadsheetTimeZone());
  Logger.log('');
  Logger.log('NOT FINISHED: do the steps in finishByHand(), then set');
  Logger.log('  submissions.update_responses_sheet and submissions.update_form_url');
  Logger.log('in config/project.yml, and the UPDATE_FORM_ID Script Property here.');
}


/**
 * The steps Apps Script cannot do. Read this; it runs nothing.
 *
 *   1. Open the form (edit URL in the log). Add a question of type "File upload" with
 *      EXACTLY this title:
 *          Upload the corrected template file (optional)
 *      Allow only specific file types: spreadsheets. Maximum number of files: 3. Maximum
 *      file size: 10 MB. Not required.
 *      The title matters: fetch_updates.py finds the uploaded files by the words
 *      "template file" in the column heading.
 *   2. Settings > Responses: confirm "Collect email addresses" is Verified.
 *   3. Settings > Responses: "Allow response editing" off, "Limit to 1 response" off.
 *   4. Share the responses sheet Viewer with the fetcher's service account (the address
 *      curation/fetch_submissions.py prints), General access Restricted.
 *   5. Share the form's upload folder (Drive, "... (File responses)") Viewer with each
 *      curator, as the submission form's upload folders are shared (RUNBOOK section 3ca).
 */
function finishByHand() {
  Logger.log('This function only documents the manual steps. Read its comment.');
}


function setVerifiedEmailCollection(form) {
  try {
    form.setEmailCollectionType(FormApp.EmailCollectionType.VERIFIED);
    Logger.log('Email collection: VERIFIED (set through the API).');
  } catch (err) {
    try {
      form.setCollectEmail(true);
      Logger.log('WARNING: fell back to setCollectEmail(true); check Settings > Responses > ' +
                 'Collect email addresses > Verified by hand.');
    } catch (err2) {
      Logger.log('ERROR: could not enable email collection (' + err2 + '). Fix by hand.');
    }
  }
}


// ---------------------------------------------------------------------------------------
// The notifier: every curator hears that an update arrived
// ---------------------------------------------------------------------------------------

function onUpdateReceived(e) {
  var entry = { email: '', received: new Date(), id: '', changed: '', files: 0 };
  try {
    if (e && e.response) {
      entry.email = e.response.getRespondentEmail() || '';
      entry.received = e.response.getTimestamp() || new Date();
      var items = e.response.getItemResponses();
      for (var i = 0; i < items.length; i++) {
        var title = items[i].getItem().getTitle().toLowerCase();
        var value = items[i].getResponse();
        if (Object.prototype.toString.call(value) === '[object Array]') {
          entry.files += value.length;
        } else if (title.indexOf('submission id') !== -1) {
          entry.id = String(value || '').trim();
        } else if (title.indexOf('what changed') !== -1) {
          entry.changed = String(value || '').trim();
        }
      }
    }
    try {
      recordUpdateOnSheet_(entry);
    } catch (err0) {
      Logger.log('Could not record the update on the status sheet: ' + err0);
    }
    notifyCuratorsOfUpdate(entry);
  } catch (err) {
    Logger.log('FAILED to send the update notice: ' + err);
    sendMail_({ to: fallbackAddress_(), subject: 'MalAvi: update notice FAILED',
                body: 'An update arrived on the update form but the notice failed:\n\n' + err +
                      '\n\nOpen the responses sheet to see it.',
                kind: 'system', force: true, note: 'update notice failed' });
  }
}


function composeUpdateNotice(entry) {
  var subject = 'MalAvi: ' + (entry.id || 'a submission') + ': an update from the submitter';
  var lines = [
    '[this is an automatic email]',
    '',
    'An update arrived on the MalAvi update form for submission ' +
      (entry.id || '(no id given)') + ', from ' + (entry.email || 'an address the form did not record') +
      ', at ' + Utilities.formatDate(entry.received, 'GMT', "yyyy-MM-dd HH:mm 'GMT'") + '.',
    '',
    'Files attached: ' + (entry.files ? entry.files : 'none') + '.',
    ''
  ];
  if (entry.changed) {
    lines.push('What changed, in their words:');
    var parts = entry.changed.split('\n');
    for (var p = 0; p < parts.length; p++) lines.push('    ' + parts[p]);
    lines.push('');
  }
  lines.push(entry.files
    ? 'The intake programs file the attached template as a new revision of the original '
      + 'submission, screen it, and send the new report to the curators. Earlier '
      + 'acceptances do not carry over to a new revision.'
    : 'No file was attached, so there is nothing to screen. Whoever flagged the '
      + 'submission reads the words above and either files what they say as a correction '
      + 'on the verdict form or asks the submitter for the template.');
  lines.push('');
  lines.push('--');
  lines.push('Confidential. A submission may contain unpublished sequences. Please do not '
      + 'forward this email.');
  return { subject: subject, body: lines.join('\n') };
}


function notifyCuratorsOfUpdate(entry) {
  var curators = curators_();
  if (!curators.length) {
    throw new Error('CURATORS is not set (Project Settings > Script Properties)');
  }
  var notice = composeUpdateNotice(entry);
  var key = 'update|' + (entry.id || '') + '|' + String(entry.email || '').toLowerCase() +
            '|' + isoNow_(entry.received);
  var failures = [];
  for (var i = 0; i < curators.length; i++) {
    try {
      sendMail_({ to: curators[i], subject: notice.subject, body: notice.body,
                  kind: 'curator', submissionId: entry.id || '', key: key,
                  note: 'update notice' });
    } catch (err) {
      failures.push(curators[i] + ': ' + err);
    }
  }
  if (failures.length) throw new Error('could not send to ' + failures.join('; '));
}


/**
 * An update is the submitter writing to us: note it on the Submissions row
 * (last_from_submitter) and the Mail tab, so the clock stops asking them and the
 * assignee sees it. Nothing is written when there is no status sheet.
 */
function recordUpdateOnSheet_(entry) {
  if (!statusSheetEnabled_()) return;
  var stamp = isoNow_(entry.received);
  recordMail_({ direction: 'in', id: entry.id || '', from: entry.email || '',
                to: 'update form', subject: 'update form response',
                note: (entry.files ? entry.files + ' file(s); ' : 'no file; ') +
                      (entry.changed || '').slice(0, 200) });
  if (/^MALAVI-SUB-\d{4}-\d{6}$/.test(entry.id || '')) {
    var row = findRow_(TAB_SUBMISSIONS, 'id', entry.id);
    if (row) writeFields_(TAB_SUBMISSIONS, row._row, { last_from_submitter: stamp });
  }
}


// installAllTriggers (triggers.gs) attaches onUpdateReceived with the others; this one
// exists for installing it alone.
function installUpdateTrigger() {
  requireSetting_('CURATORS');
  var formId = requireSetting_('UPDATE_FORM_ID');
  var existing = ScriptApp.getProjectTriggers();
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'onUpdateReceived') ScriptApp.deleteTrigger(existing[i]);
  }
  ScriptApp.newTrigger('onUpdateReceived').forForm(formId).onFormSubmit().create();
  Logger.log('Installed onUpdateReceived on form ' + formId);
}


function testUpdateNotification() {
  requireSetting_('CURATORS');
  var curators = curators_();
  notifyCuratorsOfUpdate({ email: curators[0], received: new Date(), id: 'MALAVI-SUB-0000-TEST',
                           changed: 'This is a test of the update notice. No update was made.',
                           files: 1 });
  Logger.log('Sent a sample to ' + curators.length + ' address(es).');
}
