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
 * and deliberately does not characterise the science. An email that guessed would be worse
 * than one that waits.
 *
 * PRIVACY
 * -------
 * The curator email names the submitter, because a curator may need to go back to them and
 * cannot do that from an opaque id. It does not quote their data. It goes only to the
 * addresses listed below, never to a list, and the footer says it is confidential — a
 * submission can contain unpublished sequences whose author has told nobody else.
 *
 * INSTALLING IT
 * -------------
 *   1. Sign in as malaviadmin@gmail.com. script.google.com > New project, paste this in,
 *      name it "MalAvi submission notifier".
 *   2. Edit CURATORS below.
 *   3. Run `testNotification` once. It emails you a sample and nothing else, so you can see
 *      what a curator will get before any real submitter triggers one. Authorize when asked.
 *   4. Run `installTrigger`. Do NOT hunt for the Triggers screen: this project is
 *      standalone rather than bound to the form, and a standalone script is not offered
 *      "From form" in that dropdown at all. Creating the trigger in code works regardless,
 *      is repeatable, and cannot be half-done.
 *   5. Submit a test response and confirm both emails arrive.
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
var CURATORS = [
  'vaellis@udel.edu'
];

// The submission form. Used by testNotification and installTrigger; the trigger itself
// gets the form from the event.
//
// The live value is in CUSTODY_PRIVATE.md, not here, because this file is published. It is
// not a credential -- opening the edit URL still requires being signed in as someone with
// access -- but a published id is one that can be probed, and there is no reason to hand
// out the target.
var FORM_ID = 'PASTE_THE_SUBMISSION_FORM_ID_HERE';

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
    notifyCurators(answers);
  } catch (err) {
    // A failed curator email must be visible to somebody. Without this the submission
    // simply sits unnoticed, which is the failure this script exists to prevent.
    Logger.log('FAILED to notify curators: ' + err);
    GmailApp.sendEmail(CURATORS[0], 'MalAvi: submission notification FAILED',
        'A submission arrived but the notification email failed:\n\n' + err +
        '\n\nCheck the responses sheet directly.');
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

  lines.push('The automated checks have NOT run yet. This email only says a submission');
  lines.push('arrived; it does not say whether the names or sequences are new, and there');
  lines.push('is nothing to decide yet.');
  lines.push('');
  lines.push('A second email follows with the curator report, once the checks have run.');
  lines.push('That one carries the decision link, prefilled for this submission. If it');
  lines.push('never arrives, the screening job has not run -- tell the maintainer.');
  lines.push('');
  lines.push('--');
  lines.push('Confidential. A submission may contain unpublished sequences whose author');
  lines.push('has told nobody else. Please do not forward this email.');

  // One send per curator rather than one with several recipients, so nobody learns who
  // else is on the list and a bad address cannot take the whole send down.
  for (var i = 0; i < CURATORS.length; i++) {
    GmailApp.sendEmail(CURATORS[i], subject, lines.join('\n'), { name: 'MalAvi' });
  }
}


function acknowledgeSubmitter(a) {
  if (!a.email) return;

  var body = [
    'Thank you — your submission to MalAvi has been received.',
    '',
    'Received: ' + Utilities.formatDate(a.received, 'GMT', "yyyy-MM-dd HH:mm 'GMT'"),
    '',
    'A curator will look at it. If anything needs clarifying, someone will write to you',
    'at this address.',
    '',
    'If you proposed new lineage names, the date above is what establishes your claim on',
    'them: where two people propose the same name, the earlier submission takes it. The',
    'names are confirmed when a release is published, not before.',
    '',
    'Nothing you sent is made public by submitting it. Unpublished data stays private.',
    '',
    '--',
    'MalAvi'
  ].join('\n');

  GmailApp.sendEmail(a.email, 'MalAvi: submission received', body, { name: 'MalAvi' });
}


/**
 * Attach onSubmissionReceived to the submission form. Run this once.
 *
 * Done in code rather than through the Triggers screen because this project is standalone,
 * and the UI only offers a form trigger for a script bound to that form. It is also safely
 * repeatable: any existing trigger for this handler is removed first, so running it twice
 * cannot produce two emails per submission -- which is the usual result of clicking Add
 * trigger again after being unsure whether the first one took.
 */
function installTrigger() {
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
 * List what is currently attached, so "did that work?" has an answer.
 */
function showTriggers() {
  var triggers = ScriptApp.getProjectTriggers();
  if (!triggers.length) { Logger.log('No triggers installed.'); return; }
  for (var i = 0; i < triggers.length; i++) {
    Logger.log(triggers[i].getHandlerFunction() + '  <-  ' +
               triggers[i].getEventType() + ' on ' + triggers[i].getTriggerSourceId());
  }
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
  GmailApp.sendEmail(self, 'MalAvi: delivery test',
      'If you are reading this in the MalAvi account, sending works.\n\n' +
      'If the same test does not reach a university address, the message is being\n' +
      'filtered at that end. Check the spam folder and any institutional quarantine,\n' +
      'and add this address to your contacts.\n',
      { name: 'MalAvi' });
  Logger.log('Sent a test to the account itself: ' + self);
  Logger.log('Also check the Sent folder of ' + self + ' for the earlier messages.');
}


/**
 * Send yourself one sample of the curator email, without waiting for a real submission.
 *
 * Run this before installing the trigger. It is the only way to see what a curator
 * actually receives without making a submitter generate one.
 */
function testNotification() {
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
  Logger.log('Sent a sample to: ' + CURATORS.join(', '));
}
