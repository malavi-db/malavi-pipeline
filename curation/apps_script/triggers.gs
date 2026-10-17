/**
 * Install, list and remove every trigger of the "MalAvi curation" project in one place.
 *
 * WHY ONE PLACE
 * -------------
 * Each file used to carry its own installTrigger() and showTriggers(). In a single project
 * those names collide (Apps Script keeps whichever file loads last, silently), and five
 * separate install steps are five chances to forget one. installAllTriggers() is the one
 * step: it removes every trigger this project owns for the handlers below and recreates
 * the ones whose settings exist, so running it twice cannot double an email, and a form
 * whose id is not set is simply reported as skipped.
 *
 * THE TRIGGERS
 * ------------
 *   onSubmissionReceived   form submit  SUBMISSION_FORM_ID   notify_on_submission.gs
 *   onVerdictRecorded      form submit  VERDICT_FORM_ID      notify_on_verdict.gs
 *   onUpdateReceived       form submit  UPDATE_FORM_ID       create_update_form.gs
 *   triageInbox            every 15 min (needs STATUS_SHEET_ID)   inbox_triage.gs
 *   dailyClock             daily, 07:00 in the project's time zone (UTC, appsscript.json)
 *                                       (needs STATUS_SHEET_ID)   daily_clock.gs
 *
 * Do not look for the Triggers screen to add the form triggers: a standalone project is
 * not offered "From form" there. Creating them in code works and is repeatable.
 *
 * AFTER THE MERGE
 * ---------------
 * The old separate projects ("MalAvi submission notifier", and any verdict or update
 * notifier that was installed) keep their own triggers until somebody removes them. Open
 * each old project once and run its removal, or delete the project; otherwise every
 * arrival is announced twice.
 */

var TRIGGER_HANDLERS = ['onSubmissionReceived', 'onVerdictRecorded', 'onUpdateReceived',
                        'triageInbox', 'dailyClock'];

// The hour (project time zone, UTC) the daily clock runs. 07:00 UTC is morning in Europe
// and the previous evening in the Americas, so a reminder is read on the day it is sent.
var DAILY_CLOCK_HOUR = 7;
var TRIAGE_EVERY_MINUTES = 15;


/** Remove every trigger of this project whose handler is one of ours. Returns the count. */
function removeOurTriggers_() {
  var existing = ScriptApp.getProjectTriggers();
  var removed = 0;
  for (var i = 0; i < existing.length; i++) {
    if (TRIGGER_HANDLERS.indexOf(existing[i].getHandlerFunction()) !== -1) {
      ScriptApp.deleteTrigger(existing[i]);
      removed += 1;
    }
  }
  return removed;
}


/**
 * HAND FUNCTION. Install every trigger whose settings exist. Safe to run again at any
 * time: it starts by removing the ones already installed.
 */
function installAllTriggers() {
  var report = [];
  var removed = removeOurTriggers_();
  if (removed) report.push('removed ' + removed + ' existing trigger(s)');

  var forms = [
    ['onSubmissionReceived', 'SUBMISSION_FORM_ID'],
    ['onVerdictRecorded', 'VERDICT_FORM_ID'],
    ['onUpdateReceived', 'UPDATE_FORM_ID']
  ];
  for (var f = 0; f < forms.length; f++) {
    var handler = forms[f][0];
    var formId = setting_(forms[f][1]);
    if (!formId) {
      report.push(handler + ': SKIPPED, ' + forms[f][1] + ' is not set');
      continue;
    }
    ScriptApp.newTrigger(handler).forForm(formId).onFormSubmit().create();
    report.push(handler + ': installed on form ' + formId);
  }

  if (statusSheetEnabled_()) {
    ScriptApp.newTrigger('triageInbox').timeBased().everyMinutes(TRIAGE_EVERY_MINUTES).create();
    report.push('triageInbox: every ' + TRIAGE_EVERY_MINUTES + ' minutes');
    ScriptApp.newTrigger('dailyClock').timeBased().everyDays(1).atHour(DAILY_CLOCK_HOUR).create();
    report.push('dailyClock: daily at ' + DAILY_CLOCK_HOUR + ':00 ' +
                Session.getScriptTimeZone());
  } else {
    report.push('triageInbox and dailyClock: SKIPPED, STATUS_SHEET_ID is not set ' +
                '(run createStatusSheet first, then this again)');
  }

  if (!curators_().length) {
    report.push('WARNING: CURATORS is empty; the form triggers will have nobody to tell');
  }
  logEvent_('installAllTriggers', report.join('; '));
  Logger.log(report.join('\n'));
}


/** HAND FUNCTION. Remove every trigger this project installed. Nothing else is touched. */
function removeAllTriggers() {
  var removed = removeOurTriggers_();
  logEvent_('removeAllTriggers', 'removed ' + removed + ' trigger(s)');
  Logger.log('Removed ' + removed + ' trigger(s).');
}


/** List what is currently attached, so "did that work?" has an answer. */
function showTriggers() {
  var triggers = ScriptApp.getProjectTriggers();
  if (!triggers.length) { Logger.log('No triggers installed.'); return; }
  for (var i = 0; i < triggers.length; i++) {
    var source = '';
    try { source = ' on ' + triggers[i].getTriggerSourceId(); } catch (err) { source = ''; }
    Logger.log(triggers[i].getHandlerFunction() + '  <-  ' + triggers[i].getEventType() + source);
  }
}
