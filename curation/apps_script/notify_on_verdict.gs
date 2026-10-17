/**
 * Email every curator the moment something is recorded on the verdict form.
 *
 * WHY THIS EXISTS
 * ---------------
 * Without this script a response on the verdict form does nothing and tells nobody. It
 * becomes a ledger entry, and an email to the other curators, only when the maintainer
 * runs curation/fetch_verdicts.py. So a curator who records a verdict hears nothing back,
 * the other curators hear nothing until the next fetch, and the maintainer finds out only
 * by running the fetch or opening the responses sheet. A form that answers nobody is a
 * letterbox, and people stop posting into it.
 *
 * This is the same pattern as notify_on_submission.gs: a form-submit trigger that runs as
 * the operational account, needs no credential and no maintainer, and keeps working when
 * nobody is at a terminal.
 *
 * WHAT IT SAYS, AND WHAT IT DOES NOT CLAIM
 * ----------------------------------------
 * It reports what was ENTERED ON THE FORM, in the form's own words. It does not say the
 * entry counted. Whether it counts is decided by the ledger when it next reads the form
 * (curation/src/malavi_curation/ledger.py): that the address belongs to a registered
 * curator, that the revision is the current one, that whoever filed a revision is not the
 * one approving it. This script cannot see the ledger, so the email says exactly that, in
 * one line. curation/fetch_verdicts.py still sends the ledger's own notice when it applies
 * the entry; this one stops the fetch being the only way anyone learns anything.
 *
 * It does not replace fetch_verdicts.py, and it does not change what a verdict does.
 *
 * WHY IT LISTS EVERY ANSWER RATHER THAN PICKING FIELDS
 * ----------------------------------------------------
 * The verdict form has six routes (verdict, withdraw a flag, clear a hold, correction,
 * approve a correction, close). Mapping each route's questions by name would be a second
 * copy of create_verdict_form.gs that drifts the first time a question is reworded. So the
 * body is every answered question, in the form's order, under the form's own titles. Only
 * the subject line reads specific answers, and it falls back to plain words if it cannot
 * find them.
 *
 * PRIVACY, AND THE MESSAGE TO THE SUBMITTER (2026-10-16)
 * ------------------------------------------------------
 * A reason can quote what is wrong with somebody's unpublished data. The notice therefore
 * goes only to the curators, one message each, and says it is confidential. The "Why?"
 * answer is the curators' INTERNAL note and never leaves them.
 *
 * The form has a second free-text question, "Message to the submitter" (added by
 * add_submitter_message_question.gs; the curators asked for two separate fields on
 * 2026-10-15/16). When a curator FLAGS a submission and fills that field, this trigger
 * emails the submitter that text as written, from MalAvi, with the curator and the
 * assigned curator in copy, and records the send on the status sheet. Nothing is sent to
 * the submitter on Accept or Reject, and nothing is sent when the field is empty: then the
 * notice to the curators says so, and the daily clock reminds the assignee to write.
 * The submitter's address comes from the status sheet (status_sheet.gs), where the report
 * publisher put it; with no sheet, or no address on the row, the message is not sent and
 * the curators are told why.
 *
 * INSTALLING IT
 * -------------
 * This file is one of the files of the single "MalAvi curation" project (settings.gs
 * explains). Set VERDICT_FORM_ID and CURATORS in the Script Properties, run
 * `testVerdictNotification` once (it emails the curators a clearly marked sample), then
 * run `installAllTriggers` in triggers.gs, which attaches onVerdictRecorded to the form.
 * Do not look for the Triggers screen: a standalone project is not offered a form trigger
 * there. To test end to end, record an entry on the verdict form against a submission id
 * that does not exist (MALAVI-SUB-0000-TEST): the ledger refuses an unknown id at the next
 * fetch, so the test changes nothing.
 *
 * QUOTA
 * -----
 * One message per curator per form response. A consumer Gmail account may send about 100
 * recipients a day through Apps Script; five curators and a handful of verdicts a week is
 * nowhere near it.
 */

// ---------------------------------------------------------------------------------------
// WHO GETS TOLD.
//
// The addresses are the CURATORS Script Property, read through curators_() (settings.gs)
// at the moment they are needed. No live value is written in this published file.
// config/curators.yml is the record of who is a curator.
//
// This list is for NOTIFICATION only. It grants nobody authority: whether a verdict counts
// is decided by config/curators.yml, which this script cannot read.
// ---------------------------------------------------------------------------------------
var SETTINGS_NEEDED_VERDICT = ['CURATORS', 'VERDICT_FORM_ID', 'STATUS_SHEET_ID',
                               'UPDATE_FORM_URL'];

// Question titles this script reads, matched on a lowercase substring so light rewording
// of the form does not blank a field. Keep in step with create_verdict_form.gs. Nothing
// else in this script depends on a question's wording.
var TITLE_SUBMISSION_ID = 'submission id';
var TITLE_ACTION = 'what are you recording';
var TITLE_VERDICT = 'your verdict on this revision';
var TITLE_WHY = 'why?';
var TITLE_MESSAGE = 'message to the submitter';

// The three verdict answers, as create_verdict_form.gs spells them, and the word each
// becomes in a subject line.
var VERDICT_WORDS = {
  'Accept': 'accepted',
  'Flag for further review': 'flagged for further review',
  'Reject': 'rejected'
};
var VERDICT_FLAG = 'Flag for further review';
var VERDICT_REJECT = 'Reject';


/**
 * The trigger. Runs on every response to the verdict form.
 *
 * Three independent jobs, each in its own try so one failing cannot stop the others:
 * record the entry on the status sheet, send the message to the submitter if there is
 * one to send, and tell the curators (including what happened with the first two).
 */
function onVerdictRecorded(e) {
  var entry = readEntry(e);
  var outcome = { sheet: '', submitter: '' };
  try {
    outcome.sheet = statusSheetEnabled_()
        ? withSheetLock_(function () { return recordVerdictOnSheet_(entry); })
        : '';
  } catch (err) {
    outcome.sheet = 'The status sheet could not be updated: ' + err;
    Logger.log(outcome.sheet);
  }
  try {
    outcome.submitter = sendSubmitterMessage_(entry);
  } catch (err2) {
    outcome.submitter = 'The message to the submitter FAILED to send: ' + err2 +
                        '. A curator must write to them.';
    Logger.log(outcome.submitter);
  }
  try {
    notifyCuratorsOfEntry(entry, outcome);
  } catch (err3) {
    // A failed notice must be visible to somebody, or the form is a letterbox again.
    Logger.log('FAILED to send the verdict notice: ' + err3);
    sendMail_({ to: fallbackAddress_(), subject: 'MalAvi: verdict notice FAILED',
                body: 'Something was recorded on the verdict form but the notice to the ' +
                      'curators failed:\n\n' + err3 + '\n\nOpen the responses sheet to see ' +
                      'what was recorded.',
                kind: 'system', force: true, note: 'verdict notice failed' });
  }
}


/**
 * Pull the response into a plain object: who, when, and every answered question in order.
 */
function readEntry(e) {
  var entry = { email: '', recorded: new Date(), answers: [] };
  if (!e || !e.response) return entry;

  entry.email = e.response.getRespondentEmail() || '';
  entry.recorded = e.response.getTimestamp() || new Date();

  var items = e.response.getItemResponses();
  for (var i = 0; i < items.length; i++) {
    var value = items[i].getResponse();
    // A checkbox answer is an array; show it as one line.
    if (Object.prototype.toString.call(value) === '[object Array]') {
      value = value.join(', ');
    }
    value = String(value == null ? '' : value).trim();
    if (!value) continue;       // an optional question left empty says nothing
    entry.answers.push({ title: items[i].getItem().getTitle(), value: value });
  }
  return entry;
}


/**
 * The answer to the first question whose title contains `fragment`, or ''.
 */
function answerTo(entry, fragment) {
  for (var i = 0; i < entry.answers.length; i++) {
    if (entry.answers[i].title.toLowerCase().indexOf(fragment) !== -1) {
      return entry.answers[i].value;
    }
  }
  return '';
}


/**
 * Is this address on the list of curators this script emails?
 *
 * Only used to word the email and to decide whether a message to the submitter may go
 * out. It is not the authorization check, which belongs to the ledger; an address missing
 * here may still be a registered curator who is not on the notification list yet.
 */
function onCuratorList(address) {
  var wanted = String(address || '').trim().toLowerCase();
  var curators = curators_();
  for (var i = 0; i < curators.length; i++) {
    if (curators[i].toLowerCase() === wanted) return true;
  }
  return false;
}


// ---------------------------------------------------------------------------------------
// The status sheet
// ---------------------------------------------------------------------------------------

/**
 * Append the entry to the Verdicts tab and refresh the one-line summary on the
 * Submissions row. Returns a sentence for the notice, or '' when there is no sheet.
 */
function recordVerdictOnSheet_(entry) {
  if (!statusSheetEnabled_()) return '';
  var submission = answerTo(entry, TITLE_SUBMISSION_ID);
  var all = [];
  for (var i = 0; i < entry.answers.length; i++) {
    all.push(entry.answers[i].title.replace(/[?:]\s*$/, '') + ': ' + entry.answers[i].value);
  }
  appendRow_(TAB_VERDICTS, {
    recorded_at: isoNow_(entry.recorded),
    id: submission,
    curator: entry.email,
    action: answerTo(entry, TITLE_ACTION),
    verdict: answerTo(entry, TITLE_VERDICT),
    internal_note: answerTo(entry, TITLE_WHY),
    message_to_submitter: answerTo(entry, TITLE_MESSAGE),
    answers: all.join('\n')
  });
  // The summary goes on the Submissions row only when the row exists. Creating one here
  // would let a mistyped or invented id (the form takes anything) sit in the digest as an
  // open submission forever (review 2026-10-16, finding 8).
  var row = findRow_(TAB_SUBMISSIONS, 'id', submission);
  if (row) {
    writeFields_(TAB_SUBMISSIONS, row._row, { verdicts: verdictSummary_(submission) });
    return 'Recorded on the status sheet.';
  }
  return 'Recorded on the Verdicts tab; the status sheet has no row for ' +
         (submission || 'this id') + ' (the ledger will say whether the id is real).';
}


/**
 * Email the submitter the "Message to the submitter" text, when the rules allow it.
 *
 * Sent only when ALL of these hold: the entry is a FLAG; the field is not empty; the
 * entry came from an address on the curator list (never for a stranger with the link);
 * the status sheet is on and holds the submitter's address for this submission. Every
 * other case returns a sentence saying what did not happen and why, which goes into the
 * notice to the curators, so a message that did not go out is never mistaken for one that
 * did.
 */
function sendSubmitterMessage_(entry) {
  var verdict = answerTo(entry, TITLE_VERDICT);
  var message = answerTo(entry, TITLE_MESSAGE);
  var submission = answerTo(entry, TITLE_SUBMISSION_ID);

  if (!message) {
    if (verdict === VERDICT_FLAG) {
      return 'No message to the submitter was written on the form, so nothing was sent to ' +
             'them: whoever flagged writes to them (reply-all on this email keeps the ' +
             'MalAvi address in the thread), or asks the assigned curator to.';
    }
    return '';
  }
  if (verdict !== VERDICT_FLAG) {
    return 'A message to the submitter was written but NOT sent: only a flag sends it, ' +
           'and this entry was ' + (verdict ? VERDICT_WORDS[verdict] || verdict : 'not a verdict') +
           '. A reject reaches the submitter only when a lead closes the submission.';
  }
  if (!onCuratorList(entry.email)) {
    return 'A message to the submitter was written but NOT sent: the entry came from an ' +
           'address that is not on the curator list.';
  }
  if (!statusSheetEnabled_()) {
    return 'A message to the submitter was written but NOT sent: no status sheet is ' +
           'configured, so their address is not known here. A curator must write to them.';
  }
  var row = findRow_(TAB_SUBMISSIONS, 'id', submission);
  var to = row ? String(row.submitter_email || '').trim() : '';
  if (!to || to.indexOf('@') === -1) {
    return 'A message to the submitter was written but NOT sent: the status sheet has no ' +
           'address for ' + (submission || 'this submission') + '. A curator must write to them.';
  }

  var who = curatorName_(entry.email);
  var name = row.submitter_name ? row.submitter_name.split(/\s+/)[0] : 'colleague';
  var updateForm = setting_('UPDATE_FORM_URL');
  var lines = [
    'Dear ' + name + ',',
    '',
    'A curator reviewing your MalAvi submission ' + submission + ' has a question or a ' +
      'request. Their message, as written:',
    ''
  ];
  var parts = message.split('\n');
  for (var p = 0; p < parts.length; p++) lines.push('    ' + parts[p]);
  lines.push('');
  lines.push('Please reply to this email; your reply reaches the curators. ' +
      (updateForm
        ? 'If the answer is a corrected template, upload it through the update form: ' +
          updateForm
        : 'If the answer is a corrected template, attach it to your reply.'));
  lines.push('');
  lines.push('Your submission keeps its id and its reserved lineage names while this is ' +
      'resolved.');
  lines.push('');
  lines.push('Best regards,');
  lines.push(who + ', for the MalAvi curators');
  lines.push('https://malavi-db.github.io/');

  // The curator who wrote it and the assignee are in copy, so a reply-all reaches the
  // people who can answer; the MalAvi address receives the reply as the sender.
  var copies = [entry.email];
  if (row.assignee && row.assignee.toLowerCase() !== entry.email.toLowerCase()) {
    copies.push(row.assignee);
  }
  var subject = 'MalAvi submission ' + submission + ': a question from the curators';
  var result = sendAndRecord_(to, subject, lines.join('\n'), { cc: copies.join(',') },
                              submission, 'flag message written by ' + entry.email);
  if (!result.sent) {
    // Held by mail.gs (the mode, a duplicate, a cap, the quota): the sheet must not say
    // sent, and the clock then reminds the curator to write (code review 2026-10-16, 3).
    return 'A message to the submitter was written but NOT sent (' + result.held + '). ' +
           'A curator must write to them, or it goes out when the mail mode allows.';
  }

  var stamp = isoNow_();
  writeFields_(TAB_SUBMISSIONS, row._row, { last_to_submitter: stamp });
  // Mark the Verdicts row just written (the last one for this id by this curator).
  var verdicts = readTable_(TAB_VERDICTS);
  for (var v = verdicts.length - 1; v >= 0; v--) {
    if (verdicts[v].id === submission && verdicts[v].curator === entry.email) {
      writeFields_(TAB_VERDICTS, verdicts[v]._row, { sent_to_submitter_at: stamp });
      break;
    }
  }
  return 'The message to the submitter was sent to ' + to + ' as written, from MalAvi, ' +
         'with ' + copies.join(' and ') + ' in copy.';
}


// ---------------------------------------------------------------------------------------
// The notice to the curators
// ---------------------------------------------------------------------------------------

/**
 * Compose the subject and body for one form response. Separate from the sending so that
 * `testVerdictNotification` and a reader of this file can see exactly what goes out.
 * `outcome` carries what the sheet and the submitter message did (both optional).
 */
function composeEntryNotice(entry, outcome) {
  var result = outcome || { sheet: '', submitter: '' };
  var submission = answerTo(entry, TITLE_SUBMISSION_ID) || 'a submission';
  var action = answerTo(entry, TITLE_ACTION);
  var verdict = answerTo(entry, TITLE_VERDICT);
  var who = entry.email || 'an address the form did not record';

  // The subject says what happened in the fewest words that are still true. For a verdict
  // that is the verdict; for the five other routes it is the route's own name.
  var what;
  if (verdict) {
    what = (VERDICT_WORDS[verdict] || verdict) + ' on the form';
  } else if (action) {
    what = action;
  } else {
    what = 'an entry on the verdict form';
  }
  var subject = 'MalAvi: ' + submission + ': ' + what + ' (' + who + ')';

  var lines = [
    '[this is an automatic email]',
    '',
    who + ' recorded this on the MalAvi verdict form at ' +
      Utilities.formatDate(entry.recorded, 'GMT', "yyyy-MM-dd HH:mm 'GMT'") + ':',
    ''
  ];

  // Every answered question, in the form's order and under the form's own titles. A long
  // answer (the reasoning) goes on its own indented lines so it reads as a quotation.
  for (var i = 0; i < entry.answers.length; i++) {
    var title = entry.answers[i].title.replace(/[?:]\s*$/, '');
    var value = entry.answers[i].value;
    if (value.length > 60 || value.indexOf('\n') !== -1) {
      lines.push(title + ':');
      var parts = value.split('\n');
      for (var p = 0; p < parts.length; p++) lines.push('    ' + parts[p]);
    } else {
      lines.push(title + ': ' + value);
    }
  }

  lines.push('');
  // One line per paragraph, not hand-wrapped: a mail client re-wraps to its own width.
  lines.push('This is what was entered on the form, sent the moment it was submitted. It '
      + 'takes effect when the ledger next reads the form and checks it against the rules '
      + '(a registered curator, the current revision of the submission).');

  if (entry.email && !onCuratorList(entry.email)) {
    lines.push('');
    lines.push('Note: ' + entry.email + ' is not on the list of curators this notice is '
        + 'sent to. The ledger decides whether the entry counts.');
  }

  if (verdict === VERDICT_FLAG) {
    lines.push('');
    lines.push('A flag blocks the submission until the curator who placed it withdraws it '
        + 'or a lead curator clears it.');
  }
  if (verdict === VERDICT_REJECT) {
    lines.push('');
    lines.push('Reject is for data that should not be in MalAvi at all. Nothing is sent to '
        + 'the submitter until a lead curator closes the submission. If the problem could '
        + 'be fixed, a flag is the right entry instead.');
  }
  if (result.submitter) {
    lines.push('');
    lines.push('Submitter: ' + result.submitter);
  }
  if (result.sheet) {
    lines.push('');
    lines.push(result.sheet);
  }

  lines.push('');
  lines.push('--');
  lines.push('Confidential. A reason can quote unpublished data. Please do not forward '
      + 'this email.');

  return { subject: subject, body: lines.join('\n') };
}


/**
 * Send the notice: to every curator when a curator made the entry, to the first address
 * only when somebody else did.
 *
 * The person who clicked gets it too, on purpose: it is their acknowledgment that the
 * entry arrived, which the form itself never gave them.
 *
 * AN ENTRY FROM AN ADDRESS THAT IS NOT ON THE LIST GOES TO THE FIRST ADDRESS ONLY. The
 * form accepts a response from anyone signed in to Google who has its link, and this
 * script mails whatever they typed. Sent to everyone, that would let a stranger with a
 * forwarded link put arbitrary text in every curator's inbox under MalAvi's name and use
 * up the account's daily sending quota. A registered curator answering from an address
 * that is not on this list yet is the innocent case; the first address (the lead) sees it
 * and the ledger still decides whether it counts.
 *
 * One send per address, each in its own try: a bad address must not stop the others.
 */
function notifyCuratorsOfEntry(entry, outcome) {
  var curators = curators_();
  if (!curators.length) {
    // Thrown, not returned: the trigger's catch then mails the account that owns the
    // script, which is the only address left to tell.
    throw new Error('CURATORS is not set (Project Settings > Script Properties)');
  }
  var notice = composeEntryNotice(entry, outcome);
  var recipients = onCuratorList(entry.email) ? curators : curators.slice(0, 1);
  var submission = answerTo(entry, TITLE_SUBMISSION_ID);
  // The identity of this notice is the RESPONSE: who recorded what, when. The same
  // response announced twice is one message (mail.gs duplicate check).
  var key = 'verdict-form|' + submission + '|' + String(entry.email).toLowerCase() + '|' +
            isoNow_(entry.recorded);
  var sent = 0;
  var failures = [];
  for (var i = 0; i < recipients.length; i++) {
    try {
      var result = sendMail_({ to: recipients[i], subject: notice.subject, body: notice.body,
                               kind: 'curator', submissionId: submission, key: key,
                               note: 'verdict form entry by ' + entry.email });
      if (result.sent) sent += 1;
    } catch (err) {
      failures.push(recipients[i] + ': ' + err);
    }
  }
  if (failures.length) {
    // Raised after the loop, so everyone who could be told was told; the trigger's own
    // handler then reports the failure to the first address.
    throw new Error('could not send to ' + failures.join('; '));
  }
  return sent;
}


/**
 * Attach onVerdictRecorded to the verdict form. installAllTriggers (triggers.gs) does
 * this together with the others; this one exists for installing it alone. Safely
 * repeatable: any existing trigger for this handler is removed first.
 */
function installVerdictTrigger() {
  requireSetting_('CURATORS');
  var formId = requireSetting_('VERDICT_FORM_ID');
  var existing = ScriptApp.getProjectTriggers();
  var removed = 0;
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'onVerdictRecorded') {
      ScriptApp.deleteTrigger(existing[i]);
      removed++;
    }
  }
  ScriptApp.newTrigger('onVerdictRecorded')
      .forForm(formId)
      .onFormSubmit()
      .create();
  Logger.log((removed ? 'Removed ' + removed + ' existing trigger(s). ' : '') +
             'Installed onVerdictRecorded on form ' + formId);
}


/**
 * Send the curators one clearly marked sample, without anyone recording a real entry.
 * Nothing is written to the status sheet and nothing goes to any submitter.
 */
function testVerdictNotification() {
  requireSetting_('CURATORS');
  var curators = curators_();
  var sent = notifyCuratorsOfEntry({
    email: curators[0],
    recorded: new Date(),
    answers: [
      { title: 'Submission id', value: 'MALAVI-SUB-0000-TEST' },
      { title: 'Revision', value: '1' },
      { title: 'What are you recording?', value: 'Record a verdict on a submission' },
      { title: 'Your verdict on this revision', value: 'Flag for further review' },
      { title: 'Why?', value: 'This is a test of the verdict notice. No verdict was ' +
                               'recorded and no submission is affected.' },
      { title: 'Message to the submitter', value: '(a test; nothing was sent to anyone)' },
      { title: 'What did you check?', value: 'Proposed names, Sequences' }
    ]
  }, { sheet: '', submitter: 'A test: no message was sent to any submitter.' });
  Logger.log('Sent a sample to ' + sent + ' address(es).');
}
