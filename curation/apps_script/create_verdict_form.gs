/**
 * Build the MalAvi curator verdict form from code, not by hand.
 *
 * WHY THIS EXISTS
 * ---------------
 * The form is part of the review system's interface, so it should be a reviewable
 * artifact like everything else: a diff shows what changed, a mistake is undone by
 * re-running rather than by remembering which of forty settings was clicked, and rebuilding
 * it after a bad edit takes a minute. Hand-built forms drift silently from what the code
 * that reads them expects, and nobody notices until a verdict cannot be parsed.
 *
 * It also encodes decisions that are load-bearing and easy to undo by accident — that a
 * hold and a reject REQUIRE written reasoning, that an override REQUIRES naming who was
 * consulted, and that the submission id and revision are carried rather than typed.
 *
 * HOW TO RUN IT
 * -------------
 *   1. Sign in to Google as the MalAvi operational account (malaviadmin@gmail.com).
 *      Whatever account runs this OWNS the form. Running it as yourself puts MalAvi's
 *      form on your personal account, which is the thing that account was created to stop.
 *   2. Go to script.google.com, New project, paste this file in, name it
 *      "MalAvi verdict form".
 *   3. Run `createVerdictForm`. Authorize when asked (it needs Forms and Sheets).
 *   4. Read the log (View > Logs). It prints the form's edit URL, its public URL and the
 *      responses spreadsheet id. Put that spreadsheet id in config/project.yml.
 *   5. DO THE MANUAL CHECK IN `verifyByHand()` BELOW. One setting cannot be trusted to
 *      the API and it is the one the whole authorization model rests on.
 *
 * WHAT THIS DELIBERATELY DOES NOT DO
 * ----------------------------------
 * It does not decide anything. A verdict recorded here is read by
 * curation/src/malavi_curation/ledger.py, which re-checks every rule — that the address
 * belongs to an active curator, that a hold blocks, that whoever typed a revision cannot
 * approve it. The form is an input, never an authority. Someone who forwards the link can
 * submit through it; the curator registry is what stops that mattering.
 */

// The routing question's answers. These strings are parsed by the fetch job, so changing
// one here without changing it there breaks the join. Keep them in sync with
// curation/src/malavi_curation/ledger.py's VERDICTS.
var ACTION_VERDICT = 'Record a verdict on a submission';
var ACTION_OVERRIDE = 'Clear another curator’s hold (lead curators only)';
var ACTION_CORRECTION = 'Submit a correction on behalf of a submitter';
var ACTION_RETRACT = 'Withdraw a flag you placed yourself';
var ACTION_APPROVE_CORRECTION = 'Approve a correction (lead curators only)';
var ACTION_CLOSE = 'Close a submission for good (lead curators only)';

// The reasons a submission may be closed, exactly as malavi_curation.verdicts.CLOSE_REASONS
// spells them. The Python side maps each to a code in the committed decision record, so a
// word changed here without changing it there makes the response unparseable.
var CLOSE_REASONS = [
  'It is already in MalAvi',
  'Not avian haemosporidian data',
  'A flag on it was never answered',
  'The records could not be checked against the source',
  'Another submission replaces it'
];

var VERDICT_APPROVE = 'Accept';
var VERDICT_HOLD = 'Flag for further review';
var VERDICT_DECLINE = 'Reject';


function createVerdictForm() {
  var form = FormApp.create('MalAvi — curator verdict');

  form.setDescription(
      'For MalAvi curators. Open the submission’s report first — this form records ' +
      'what you decided, it does not show you the submission.\n\n' +
      'Use the link from your notification email: it fills in the submission id and the ' +
      'revision you were shown, so your decision is recorded against the version you ' +
      'actually read.\n\n' +
      'The site can take up to six hours to catch up after you submit. That is normal; ' +
      'please do not submit again.');

  // Verified, not typed. A typed address is a claim; a verified one is Google confirming
  // the responder controls that mailbox. It still proves nothing about whether they are a
  // curator — config/curators.yml decides that — but without it, anyone with the link
  // could put any colleague's name on a decision.
  setVerifiedEmailCollection(form);

  form.setProgressBar(false);
  form.setAllowResponseEdits(false);   // a change of mind is a new, separately timestamped
                                       // verdict, not a silent edit to the old one
  form.setLimitOneResponsePerUser(false);  // a curator may legitimately revise; the ledger
                                           // keeps every verdict and stands on the latest

  // ---- Page 1: what is this, and about which submission --------------------------
  // Both carried by the prefilled link rather than typed. A mistyped submission id
  // attaches a decision to somebody else's work.
  var submissionId = form.addTextItem()
      .setTitle('Submission id')
      .setHelpText('Filled in by the link in your email (looks like MALAVI-SUB-2026-000123). ' +
                   'If it is empty, go back to the email rather than typing it.')
      .setRequired(true);

  var revision = form.addTextItem()
      .setTitle('Revision')
      .setHelpText('Filled in by the link. This is the version of the submission you were ' +
                   'shown; your decision is recorded against it.')
      .setRequired(true);

  var action = form.addMultipleChoiceItem()
      .setTitle('What are you recording?')
      .setRequired(true);

  // ---- The six branches -----------------------------------------------------------
  // Three of these close paths the curator instructions already promised and no interface
  // could reach: withdrawing your own flag ("this BLOCKS the submission until you withdraw
  // it or a lead curator clears it" — only the second half had a route), a lead approving a
  // correction, which is a real gate in the ledger that nothing could satisfy, so every
  // correction stopped at "proposed", and closing a submission for good.
  //
  // That last one matters most. "Reject" under Record a verdict lands the submission on
  // 'held', deliberately, so a rejection gets a second look instead of being terminal on one
  // person's say-so. Nothing then moved it any further, so a rejected submission stayed live
  // forever: holding its reserved lineage names, and never telling the submitter anything.
  var verdictPage = buildVerdictPage(form);
  var overridePage = buildOverridePage(form);
  var correctionPage = buildCorrectionPage(form);
  var retractPage = buildRetractionPage(form);
  var approveCorrectionPage = buildCorrectionApprovalPage(form);
  var closePage = buildClosePage(form);

  action.setChoices([
    action.createChoice(ACTION_VERDICT, verdictPage),
    action.createChoice(ACTION_RETRACT, retractPage),
    action.createChoice(ACTION_OVERRIDE, overridePage),
    action.createChoice(ACTION_CORRECTION, correctionPage),
    action.createChoice(ACTION_APPROVE_CORRECTION, approveCorrectionPage),
    action.createChoice(ACTION_CLOSE, closePage)
  ]);

  // ---- Responses spreadsheet -----------------------------------------------------
  var sheet = SpreadsheetApp.create('MalAvi — curator verdicts (responses)');

  // GMT WITH NO DAYLIGHT SAVING, set here rather than left to the account's locale.
  //
  // Google stamps each response in the SPREADSHEET's timezone and records no offset, so
  // the zone is the only thing that makes a response time unambiguous -- and that time
  // drives the 24h publish hold and the 60-day timeout. verdicts.parse_row assumes UTC,
  // per `review.verdict_sheet_timezone` in config/project.yml.
  //
  // A new sheet inherits the creating account's locale (US Eastern for malaviadmin), which
  // would read every verdict 4-5 hours off, shifting with DST, with nothing in the data to
  // show for it. This was missed on the 2026-08-10 regeneration and fixed by hand; setting
  // it in code is what stops the next rebuild repeating that.
  //
  // "Etc/GMT" rather than "Europe/London" -- London observes DST.
  sheet.setSpreadsheetTimeZone('Etc/GMT');

  form.setDestination(FormApp.DestinationType.SPREADSHEET, sheet.getId());

  Logger.log('Form (edit)     : ' + form.getEditUrl());
  Logger.log('Form (public)   : ' + form.getPublishedUrl());
  Logger.log('Responses sheet : ' + sheet.getId());
  Logger.log('Sheet timezone  : ' + sheet.getSpreadsheetTimeZone() +
             '  (must be GMT with no daylight saving)');
  Logger.log('');
  Logger.log('Put the responses sheet id in config/project.yml under review.verdict_sheet.');
  Logger.log('Item ids for building prefilled links:');
  Logger.log('  submission id : ' + submissionId.getId());
  Logger.log('  revision      : ' + revision.getId());
  Logger.log('');
  Logger.log('NOW DO THE MANUAL CHECK — see verifyByHand() in this file.');

  return form;
}


/**
 * Ask Google to verify responders' email addresses.
 *
 * Handled defensively because the Apps Script surface for this has changed more than once
 * and differs between consumer and Workspace accounts. If the modern call is unavailable
 * this falls back to the older one and SAYS SO LOUDLY, because the fallback may collect a
 * responder-typed address instead of a verified one — which looks identical in the
 * spreadsheet and is worth much less.
 */
function setVerifiedEmailCollection(form) {
  try {
    form.setEmailCollectionType(FormApp.EmailCollectionType.VERIFIED);
    Logger.log('Email collection: VERIFIED (set through the API).');
  } catch (err) {
    try {
      form.setCollectEmail(true);
      Logger.log('WARNING: could not set VERIFIED collection through the API (' + err + ').');
      Logger.log('WARNING: fell back to setCollectEmail(true). This MAY be responder-typed,');
      Logger.log('WARNING: which is unverified. Check by hand before using this form:');
      Logger.log('WARNING:   Settings > Responses > Collect email addresses > Verified');
    } catch (err2) {
      Logger.log('ERROR: could not enable email collection at all (' + err2 + ').');
      Logger.log('ERROR: DO NOT USE THIS FORM until it is enabled by hand. Without an');
      Logger.log('ERROR: address there is no way to attribute a verdict to a curator.');
    }
  }
}


function buildVerdictPage(form) {
  var page = form.addPageBreakItem().setTitle('Your verdict');

  var verdict = form.addMultipleChoiceItem()
      .setTitle('Your verdict on this revision')
      .setHelpText(
          'Accept — this can go into a release.\n' +
          'Flag for further review — something needs resolving first. This BLOCKS the ' +
          'submission until you withdraw it or a lead curator clears it.\n' +
          'Reject — this should not go into MalAvi.')
      .setChoiceValues([VERDICT_APPROVE, VERDICT_HOLD, VERDICT_DECLINE])
      .setRequired(true);

  // Required for every verdict rather than only for the blocking ones. Forms cannot make
  // one answer's requiredness depend on another answer within a page, and the failure
  // directions are not symmetric: an unexplained "Accept" costs a little redundant typing,
  // while an unexplained "Flag" leaves the submitter unable to answer the objection and the
  // lead unable to weigh it. The ledger enforces the real rule.
  form.addParagraphTextItem()
      .setTitle('Why?')
      .setHelpText('Required. If you flagged or rejected, this is what the submitter will ' +
                   'be asked to address and what a lead curator will weigh — be specific ' +
                   'about which lineage, host or record. If you accepted, one line is fine.')
      .setRequired(true);

  form.addCheckboxItem()
      .setTitle('What did you check?')
      .setHelpText('Optional. Records what was looked at, not what was decided.')
      .setChoiceValues(['Proposed names', 'Sequences', 'Host and locality',
                        'Supporting material'])
      .setRequired(false);

  page.setGoToPage(FormApp.PageNavigationType.SUBMIT);
  return page;
}


function buildOverridePage(form) {
  var page = form.addPageBreakItem()
      .setTitle('Clear another curator’s hold')
      .setHelpText(
          'Lead curators only. Withdrawing a hold YOU placed is not an override — use ' +
          '“Record a verdict” for that.\n\n' +
          'Clearing someone else’s objection sets aside their judgment, so it is ' +
          'recorded permanently, with who you spoke to.');

  form.addTextItem()
      .setTitle('Which hold are you clearing?')
      .setHelpText('The verdict id from the notification (looks like V2).')
      .setRequired(true);

  // The three fields that turn an attestation into something checkable. A bare "yes we
  // discussed it" box has nothing behind it, and an override nobody can see is
  // indistinguishable from there having been no hold at all.
  form.addTextItem()
      .setTitle('Who did you consult?')
      .setHelpText('Name them. At minimum, the curator whose hold this is.')
      .setRequired(true);

  form.addDateItem()
      .setTitle('When did that conversation happen?')
      .setRequired(true);

  form.addMultipleChoiceItem()
      .setTitle('How?')
      .setChoiceValues(['Email', 'Call or video', 'In person', 'Group discussion'])
      .setRequired(true);

  form.addParagraphTextItem()
      .setTitle('What was resolved?')
      .setHelpText('Required. The reasoning, so that someone reading this in five years ' +
                   'can tell why the objection did not stand.')
      .setRequired(true);

  page.setGoToPage(FormApp.PageNavigationType.SUBMIT);
  return page;
}


function buildCorrectionPage(form) {
  var page = form.addPageBreakItem()
      .setTitle('Correction on behalf of a submitter')
      .setHelpText(
          'Use this when you are fixing something in a submission rather than judging it.\n\n' +
          'Flag the submission first. A correction only counts on top of a standing flag — ' +
          'you accept later, once you can see the corrected report.\n\n' +
          'This creates a NEW REVISION, which clears every existing approval — including ' +
          'approvals from curators who were perfectly happy. That is deliberate: they ' +
          'approved a different version. The submitter’s original date and their claim on ' +
          'any reserved lineage names are kept.');

  // The distinction the whole correction path turns on. Another curator can license a
  // judgment fix; only the authors can license a change to what the data claims. Two
  // curators agreeing about somebody else's field data is still a guess.
  form.addMultipleChoiceItem()
      .setTitle('What kind of correction is this?')
      .setHelpText(
          'Judgment — a host synonym, a country spelling, a naming convention. Another ' +
          'curator can confirm these.\n' +
          'Data — which host, which locality, which sequence, a prevalence number. Only ' +
          'the authors can confirm these.')
      .setChoiceValues([
        'Judgment — confirmed with another curator',
        'Data — confirmed with the authors'])
      .setRequired(true);

  form.addTextItem()
      .setTitle('Who confirmed it?')
      .setHelpText('Name the curator, or the author you heard back from.')
      .setRequired(true);

  form.addDateItem()
      .setTitle('When did you hear back?')
      .setRequired(true);

  // NO "have you also flagged this?" QUESTION HERE, removed 2026-08-10.
  //
  // It asked a curator to self-report something the ledger already knows as fact:
  // ledger.record_correction refuses a correction when blocking_holds(entry) is empty, and
  // says why. Asking as well bought nothing and cost a great deal of confusion -- its two
  // answers read as a genuine choice, but "No, I will flag it now" made the parser discard
  // the correction, so an honest answer threw the curator's work away.
  //
  // The rule itself is unchanged and still enforced; it now lives in the page description
  // above, as a statement rather than a question. This is the same reasoning
  // _parse_correction_approval gives for checking nothing: a check here would be a second
  // copy of a rule that has to hold at the write regardless of what reached it.

  // "Be specific" is doing real work here, not being polite. Nothing parses this text --
  // a program that guessed which cell a sentence meant would be inferring data from prose
  // -- so a maintainer reads it and makes the change by hand. A correction that does not
  // say which row and which field cannot be carried out without asking the curator again.
  //
  // The help text used to end "A maintainer applies this — you do not need to touch any
  // files", which was true of the second clause and false of the first: nothing applied it
  // at all, and the uncorrected value was published while every gate reported success.
  form.addParagraphTextItem()
      .setTitle('What should change?')
      .setHelpText('Be specific: which row, which field, from what to what — a maintainer ' +
                   'makes this change by hand, and can only do it if you say exactly ' +
                   'what it is. You do not need to touch any files.')
      .setRequired(true);

  page.setGoToPage(FormApp.PageNavigationType.SUBMIT);
  return page;
}


/**
 * Withdrawing a flag you placed yourself.
 *
 * Open to every curator and restricted to your own flag. Retracting your own objection is
 * not an override — nobody's judgment is being set aside but your own — so it needs no lead
 * and no consultation record. The ledger enforces the "your own" part; this page only has
 * to make it clear which flag is meant.
 */
function buildRetractionPage(form) {
  var page = form.addPageBreakItem().setTitle('Withdraw your flag');

  page.setHelpText(
      'Use this when you flagged a submission and no longer think it should be blocked — ' +
      'you got an answer, or you checked and you were mistaken.\n\n' +
      'This only works on a flag YOU placed. To clear someone else’s, a lead curator ' +
      'uses "Clear another curator’s hold".');

  form.addTextItem()
      .setTitle('Which flag are you withdrawing?')
      .setHelpText('The id from the report, like V1 or V2. If you have flagged this ' +
                   'submission more than once, withdrawing the wrong one leaves it blocked.')
      .setRequired(true);

  form.addParagraphTextItem()
      .setTitle('What changed your mind?')
      .setHelpText('Optional, but worth a line: the next curator to read this submission ' +
                   'will see the objection and will want to know how it was settled.')
      .setRequired(false);

  page.setGoToPage(FormApp.PageNavigationType.SUBMIT);
  return page;
}


/**
 * A lead approving a proposed correction.
 *
 * Lead-only, and a lead may not approve their own — both enforced by the ledger, not here.
 * What this page must not do is let the approval be recorded with nobody named: a
 * correction changes what MalAvi will publish about somebody else's study, and the curator
 * instructions promise the lead approves "after discussing with you and potentially the
 * author who submitted the data". An approval with no one named is that promise unkept.
 */
function buildCorrectionApprovalPage(form) {
  var page = form.addPageBreakItem().setTitle('Approve a correction');

  page.setHelpText(
      'Lead curators only. This agrees that a correction another curator proposed may be ' +
      'applied. The maintainer then applies it, and the corrected submission goes back to ' +
      'all curators as a new revision with a fresh report.\n\n' +
      'You cannot approve a correction you proposed yourself.');

  form.addTextItem()
      .setTitle('Which correction are you approving?')
      .setHelpText('The id from the report, like C1.')
      .setRequired(true);

  form.addTextItem()
      .setTitle('Who did you discuss it with?')
      .setHelpText('The curator who raised it, and the authors where the data itself is ' +
                   'changing. Names or addresses, separated by commas.')
      .setRequired(true);

  // NOT 'What was resolved?'. That is the title of the override page's question, and a
  // question title becomes the response sheet's column header verbatim -- two questions
  // sharing a title produce two columns sharing a name, and csv.DictReader keeps the last.
  // This page's column therefore answered for the override page's, and a lead's required
  // justification for clearing someone else's hold was stored empty. Renamed on the live
  // form 2026-08-14; verdicts.COL_CONCLUDED is the matching constant. Keep every title on
  // this form unique -- test_verdict_form_titles_are_unique enforces it here, and nothing
  // can enforce it in Google.
  form.addParagraphTextItem()
      .setTitle('What did the discussion conclude?')
      .setHelpText('Optional.')
      .setRequired(false);

  page.setGoToPage(FormApp.PageNavigationType.SUBMIT);
  return page;
}


/**
 * Closing a submission for good.
 *
 * The act that finishes a rejection. "Reject" under Record a verdict is deliberately NOT
 * this: it lands the submission on 'held', so that a rejection gets a second look rather
 * than ending the submission on one person's judgment. This is the separate, later act,
 * and it is lead-only for the same reason clearing another curator's hold is.
 *
 * The reason is a fixed list rather than a text box because it is the one field of the
 * review ledger that reaches data/decisions.json — the committed record, whose whole
 * premise is that it contains no unpublished science. A free-text reason there would
 * eventually carry a sentence describing somebody's data, in the one file meant to survive
 * the erasure of that data.
 */
function buildClosePage(form) {
  var page = form.addPageBreakItem().setTitle('Close a submission');

  page.setHelpText(
      'Lead curators only, and it ends the submission. Use it when a flag was never ' +
      'answered, or the submission was never going to be included.\n\n' +
      'Its reserved lineage names are released, it disappears from the public queue, and ' +
      'the submitter is told — after the same 24-hour wait an approval gets, so there is ' +
      'a window to notice a mistake before somebody is told their work was refused.\n\n' +
      'A submission that a curator has already accepted cannot be closed here. Flag it ' +
      'first, so that the objection is on the record and attributed to whoever raised it.');

  form.addMultipleChoiceItem()
      .setTitle('Why is it being closed?')
      .setHelpText('This goes into the public decision record, so it is a fixed list.')
      .setChoiceValues(CLOSE_REASONS)
      .setRequired(true);

  form.addParagraphTextItem()
      .setTitle('Anything to add?')
      .setHelpText('Optional, and kept internal — it is not published. Say what the ' +
                   'reason above cannot.')
      .setRequired(false);

  page.setGoToPage(FormApp.PageNavigationType.SUBMIT);
  return page;
}


/**
 * THE MANUAL CHECK. Do this once, after running createVerdictForm, before any real use.
 *
 * 1. Open the form's edit URL, Settings > Responses.
 *    Confirm "Collect email addresses" is set to **Verified**, not "Responder input".
 *    Responder input is a typed string: anyone could put a curator's address in it, and
 *    the response would look identical in the spreadsheet. This is the single setting the
 *    authorization model rests on.
 *
 * 2. Settings > Responses: confirm "Allow response editing" is OFF.
 *
 * 3. Send yourself the public URL from a different Google account and submit once.
 *    Confirm the response row carries the verified address of the account that submitted,
 *    NOT the account that owns the form.
 *
 * 4. Confirm the branching works: choosing each of the five actions -- record a verdict,
 *    withdraw your own flag, clear another curator's hold, submit a correction, approve a
 *    correction -- should show only that branch's questions and then submit.
 *
 * 5. Delete the test response from the spreadsheet before real use.
 */
function verifyByHand() {
  throw new Error('This is documentation, not a function. Read the comment above it.');
}
