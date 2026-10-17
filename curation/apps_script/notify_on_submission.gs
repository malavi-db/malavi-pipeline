/**
 * Email the curators when a submission arrives, and acknowledge the submitter.
 *
 * WHY THIS RATHER THAN THE PYTHON SIDE
 * ------------------------------------
 * Two routes were possible. A mail API key driven from the repository could send the full
 * screen result — every automated check, the sequence comparisons — but costs a secret to
 * store and rotate, and only fires when a job runs. This one fires the moment somebody
 * presses Submit, needs no credential at all, and sends from the account that owns the
 * form. For "a curator finds out promptly", that wins.
 *
 * WHAT IT CANNOT KNOW, AND WHY THE EMAIL IS SHORT
 * -----------------------------------------------
 * At submission time the automated checks have not run. Nothing here knows whether a
 * proposed lineage name is taken, whether a sequence is already a named lineage, or what
 * the opaque submission id will be — all of that is minted and computed later, by the
 * fetch and screen jobs. So this email says *that* something arrived and *where it is*,
 * and deliberately does not characterize the science. An email that guessed would be worse
 * than one that waits.
 *
 * PRIVACY
 * -------
 * The curator email names the submitter, because a curator may need to go back to them and
 * cannot do that from an opaque id. It does not quote their data. It goes only to the
 * addresses listed below, never to a list, and the footer says it is confidential — a
 * submission can contain unpublished sequences whose author has told nobody else.
 *
 * WHO HEARS ABOUT AN ARRIVAL (changed 2026-10-16)
 * ----------------------------------------------
 * The leads only (the ARRIVAL_TO Script Property, falling back to ATTENTION_TO), not every
 * curator. Mélanie asked on 2026-10-16 to receive only the report email; the arrival
 * notice exists so that somebody knows a submission is sitting unscreened if the intake
 * cycle is late, and the leads are who act on that. The arrival is also written to the
 * Arrivals tab of the status sheet (status_sheet.gs), and the daily clock reports an
 * arrival that has had no report for two days.
 *
 * INSTALLING IT
 * -------------
 * This file is one of the files of the single "MalAvi curation" project (settings.gs
 * explains). Set SUBMISSION_FORM_ID (and CURATORS, ATTENTION_TO) in the Script Properties,
 * run `testSubmissionNotification` once (it emails the arrival recipients a sample), then
 * run `installAllTriggers` in triggers.gs, which attaches onSubmissionReceived to the
 * form. Do NOT hunt for the Triggers screen: this project is standalone rather than bound
 * to the form, and a standalone script is not offered "From form" in that dropdown at all.
 * If the old separate "MalAvi submission notifier" project still has its trigger, delete
 * that trigger (or the project), or every arrival is announced twice.
 *
 * IF AN EMAIL DOES NOT ARRIVE
 * ---------------------------
 * The log saying it sent means Google accepted it, not that anyone received it. A brand
 * new Gmail account with no sending history, writing to a university address, is a
 * textbook spam-filter trigger. Run `diagnoseDelivery` below: it reports the remaining
 * send quota and mails the operational account itself, which isolates "sending is broken"
 * from "the recipient's filter ate it".
 *
 * Sends go through GmailApp rather than MailApp so a copy lands in the account's Sent
 * folder. MailApp leaves no trace, which is the difference between believing a message
 * went and being able to point at it.
 *
 * QUOTA
 * -----
 * A consumer Gmail account may send about 100 email recipients per day through Apps
 * Script. MalAvi is nowhere near that — a busy week is a handful of submissions times a
 * handful of curators — but a bulk operation would hit it.
 */

// ---------------------------------------------------------------------------------------
// WHO GETS TOLD.
//
// **This list is for NOTIFICATION only. It does not grant anybody authority.** Whether a
// verdict counts is decided by config/curators.yml in the repository, which this script
// cannot read. The two therefore have to be kept in step by hand: adding a curator means
// editing both, and a curator removed from the registry but left here keeps receiving
// submissions they can no longer vote on.
// ---------------------------------------------------------------------------------------
// The recipients are arrivalTo_() in settings.gs: the ARRIVAL_TO Script Property, or the
// leads (ATTENTION_TO) when it is empty. No live value is written in this published file,
// so pasting it over the live script blanks nothing. Nothing is read from the properties
// at the top of this file (LOAD ORDER in settings.gs).
var SETTINGS_NEEDED_SUBMISSION = ['SUBMISSION_FORM_ID', 'ARRIVAL_TO'];

// The submission form id: the SUBMISSION_FORM_ID Script Property, read when a trigger is
// installed. The live value is in CUSTODY_PRIVATE.md, not here, because this file is
// published. It is not a credential -- opening the edit URL still requires being signed in
// as someone with access -- but a published id is one that can be probed, and there is no
// reason to hand out the target.

// Question titles, matched on a lowercase substring so light rewording of the form does not
// silently blank a field. Keep in step with create_submission_form.gs.
var FIELDS = {
  name: 'first and last name',
  institution: 'institution',
  country: 'country',
  stage: 'published or unpublished',
  kind: 'template file, a pdf',
  sending: 'what are you sending',
  notes: 'notes or communication'
};


/**
 * The trigger. Runs on every submission to the form it is installed against.
 */
function onSubmissionReceived(e) {
  var answers = readResponse(e);
  try {
    if (statusSheetEnabled_()) withSheetLock_(function () { recordArrival_(answers); });
  } catch (err0) {
    Logger.log('Could not write the Arrivals tab: ' + err0);
  }
  try {
    notifyCurators(answers);
  } catch (err) {
    // A failed curator email must be visible to somebody. Without this the submission
    // simply sits unnoticed, which is the failure this script exists to prevent.
    Logger.log('FAILED to notify curators: ' + err);
    sendMail_({ to: fallbackAddress_(), subject: 'MalAvi: submission notification FAILED',
                body: 'A submission arrived but the notification email failed:\n\n' + err +
                      '\n\nCheck the responses sheet directly.',
                kind: 'system', force: true, note: 'arrival notice failed' });
  }
  try {
    acknowledgeSubmitter(answers);
  } catch (err2) {
    Logger.log('Could not acknowledge submitter: ' + err2);
  }
}


/**
 * Pull the response into a plain object, tolerating a reworded question.
 */
function readResponse(e) {
  var out = { email: '', received: new Date(), files: 0, links: [] };
  if (!e || !e.response) return out;

  out.email = e.response.getRespondentEmail() || '';
  out.received = e.response.getTimestamp() || new Date();

  var items = e.response.getItemResponses();
  for (var i = 0; i < items.length; i++) {
    var title = items[i].getItem().getTitle().toLowerCase();
    var value = items[i].getResponse();

    // A file-upload answer is an array of file ids; count them rather than linking each,
    // since a curator reaches them through the submission folder anyway.
    if (Object.prototype.toString.call(value) === '[object Array]' &&
        title.indexOf('upload') === -1 && title.indexOf('submission ') === 0) {
      out.files += value.length;
      continue;
    }
    if (Object.prototype.toString.call(value) === '[object Array]') {
      if (title.indexOf('what did you check') === -1) {
        out.files += value.length;
        // The links, not just the count. For a PDF-only submission there is no report to
        // open, so without these the email tells a curator that a paper arrived and gives
        // them no way to read it.
        for (var f = 0; f < value.length; f++) {
          out.links.push({ label: items[i].getItem().getTitle().split('(')[0].trim(),
                           url: 'https://drive.google.com/open?id=' + value[f] });
        }
      }
      continue;
    }
    for (var key in FIELDS) {
      if (title.indexOf(FIELDS[key]) !== -1) { out[key] = value; }
    }
  }
  return out;
}


function notifyCurators(a) {
  a.links = a.links || [];
  // "arrived", not "needs review". Nothing has been checked when this fires, so there is
  // nothing for a curator to review yet; the report email is the one that asks for a
  // decision. The old subject invited action this message cannot support.
  var subject = 'MalAvi: a submission arrived'
      + (a.stage ? ' (' + a.stage.toLowerCase() + ')' : '');

  var lines = [
    '[this is an automatic email]',
    '',
    'A new submission arrived on the MalAvi form.',
    '',
    'Received:     ' + Utilities.formatDate(a.received, 'GMT', "yyyy-MM-dd HH:mm 'GMT'"),
    'From:         ' + (a.name || 'not given')
                     + (a.institution ? ', ' + a.institution : '')
                     + (a.country ? ' (' + a.country + ')' : ''),
    'Contact:      ' + (a.email || 'not given'),
    'Data:         ' + (a.stage || 'not stated'),
    'Sending:      ' + (a.sending || 'not stated'),
    'Attached:     ' + (a.files ? a.files + ' file(s)' : 'no files'),
    ''
  ];

  if (a.notes) {
    lines.push('Their notes:');
    lines.push('  ' + a.notes);
    lines.push('');
  }

  if (a.links.length) {
    lines.push('The files they sent:');
    for (var L = 0; L < a.links.length; L++) {
      lines.push('  ' + a.links[L].label);
      lines.push('    ' + a.links[L].url);
    }
    lines.push('');
  }

  // "Automated" was the wrong word here and a curator read it the way anyone would:
  // as "a machine is about to do this", and then waited. The checks are automated in
  // the sense of being rule-based, not in the sense of being scheduled. Say plainly what
  // happens next, or the silence between the two emails looks like a system working
  // rather than a step nobody has run.
  //
  // Reworded 2026-10-02. The earlier text said the maintainer "will run the report
  // checking software" and asked curators to tell the maintainer if nothing came, which
  // told five people that somebody else had it in hand. This version says what the second
  // email is for and what to do if it does not come, and names nobody whose job it is to
  // act instead of the reader. A reply goes to the MalAvi address. One line per paragraph,
  // not hand-wrapped: a mail client re-wraps to its own width.
  //
  // Since 2026-10-16 this goes to the leads only, and the report email names the
  // assigned curator (publish_report.gs), so the text can say so.
  lines.push('The checks have not run yet. This email only says a submission arrived; it '
      + 'does not say whether the names or sequences are new, and there is nothing to '
      + 'decide yet.');
  lines.push('');
  lines.push('The curator report follows when the intake cycle has run (four times a day '
      + 'when scheduled). That email goes to every curator and names the one the '
      + 'submission is assigned to. The daily digest will say so if no report has gone out '
      + 'within two days.');
  lines.push('');
  lines.push('--');
  lines.push('Confidential. A submission may contain unpublished sequences. Please do not');
  lines.push('forward this email.');

  var recipients = arrivalTo_();
  if (!recipients.length) {
    // Thrown so the trigger's catch mails the account that owns the script.
    throw new Error('neither ARRIVAL_TO, ATTENTION_TO nor CURATORS is set (Project ' +
                    'Settings > Script Properties)');
  }
  // One send per recipient rather than one with several recipients, so nobody learns who
  // else is on the list and a bad address cannot take the whole send down. The key is
  // the arrival itself (who, when), so a re-run trigger announces it once.
  var key = 'arrival|' + String(a.email || '').toLowerCase() + '|' + isoNow_(a.received);
  for (var i = 0; i < recipients.length; i++) {
    sendMail_({ to: recipients[i], subject: subject, body: lines.join('\n'),
                kind: 'curator', key: key, note: 'arrival notice' });
  }
}


/**
 * One Arrivals row on the status sheet, so the daily clock can notice an arrival that
 * never got a report. Nothing is written when there is no sheet. The matched_id column
 * is filled later by the clock, when a Submissions row with the same address appears.
 */
function recordArrival_(a) {
  if (!statusSheetEnabled_()) return;
  appendRow_(TAB_ARRIVALS, {
    form_timestamp: isoNow_(a.received),
    submitter_name: a.name || '',
    submitter_email: a.email || '',
    stage: a.stage || '',
    files: String(a.files || 0),
    matched_id: ''
  });
}


function acknowledgeSubmitter(a) {
  if (!a.email) return;

  var body = [
    '[this is an automatic email]',
    '',
    'Thank you! Your submission to MalAvi has been received.',
    '',
    'Received: ' + Utilities.formatDate(a.received, 'GMT', "yyyy-MM-dd HH:mm 'GMT'"),
    '',
    'A curator will look at it. If anything needs clarifying, a curator will write to you',
    'at this email address.',
    '',
    'Thanks again for submitting data to MalAvi.',
    '',
    '--',
    'MalAvi',
    'https://malavi-db.github.io/'
  ].join('\n');

  sendMail_({ to: a.email, subject: 'MalAvi: submission received', body: body,
              kind: 'submitter', key: 'received|' + String(a.email).toLowerCase() + '|' +
              isoNow_(a.received), note: 'submission received' });
}


/**
 * Attach onSubmissionReceived to the submission form. installAllTriggers (triggers.gs)
 * does this together with the others; this one exists for installing it alone.
 *
 * Done in code rather than through the Triggers screen because this project is standalone,
 * and the UI only offers a form trigger for a script bound to that form. It is also safely
 * repeatable: any existing trigger for this handler is removed first, so running it twice
 * cannot produce two emails per submission -- which is the usual result of clicking Add
 * trigger again after being unsure whether the first one took.
 */
function installSubmissionTrigger() {
  var FORM_ID = requireSetting_('SUBMISSION_FORM_ID');
  var existing = ScriptApp.getProjectTriggers();
  var removed = 0;
  for (var i = 0; i < existing.length; i++) {
    if (existing[i].getHandlerFunction() === 'onSubmissionReceived') {
      ScriptApp.deleteTrigger(existing[i]);
      removed++;
    }
  }
  ScriptApp.newTrigger('onSubmissionReceived')
      .forForm(FORM_ID)
      .onFormSubmit()
      .create();
  Logger.log((removed ? 'Removed ' + removed + ' existing trigger(s). ' : '') +
             'Installed onSubmissionReceived on form ' + FORM_ID);
}


/**
 * Is sending working at all, and is the problem at the far end?
 *
 * Mails the operational account itself. Gmail to Gmail is barely filtered, so if that one
 * arrives and a message to a university address does not, the script is fine and the
 * recipient's spam filter is holding it.
 */
function diagnoseDelivery() {
  var quota = MailApp.getRemainingDailyQuota();
  Logger.log('Remaining send quota today: ' + quota);
  if (quota === 0) {
    Logger.log('QUOTA EXHAUSTED — nothing will send until it resets.');
    return;
  }
  var self = Session.getActiveUser().getEmail();
  // Forced: this is the test of whether sending works at all, whatever the mode.
  sendMail_({ to: self, subject: 'MalAvi: delivery test',
              body: 'If you are reading this in the MalAvi account, sending works.\n\n' +
                    'If the same test does not reach a university address, the message is being\n' +
                    'filtered at that end. Check the spam folder and any institutional quarantine,\n' +
                    'and add this address to your contacts.\n',
              kind: 'system', force: true, note: 'diagnoseDelivery' });
  Logger.log('Sent a test to the account itself: ' + self);
  Logger.log('Also check the Sent folder of ' + self + ' for the earlier messages.');
}


/**
 * Send the arrival recipients one sample of the arrival email, without waiting for a real
 * submission. Run this before installing the trigger. Nothing is written to the sheet.
 */
function testSubmissionNotification() {
  notifyCurators({
    received: new Date(),
    name: 'A Submitter',
    institution: 'An Institution',
    country: 'Sweden',
    email: 'submitter@example.edu',
    stage: 'Unpublished',
    sending: 'New lineage names and sequences',
    notes: 'This is a test of the notification email. No real submission was made.',
    files: 2,
    links: [{ label: 'Submission PDF and Supplementary Materials',
              url: 'https://drive.google.com/open?id=1EXAMPLEfileid0000000' }]
  });
  Logger.log('Sent a sample to: ' + arrivalTo_().join(', '));
}
