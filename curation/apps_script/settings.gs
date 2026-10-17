/**
 * Live settings for the MalAvi Apps Script project, read from Script Properties.
 *
 * WHY
 * ---
 * Until 2026-10-14 each script carried its live configuration as constants in the code:
 * the curator addresses, the form ids, the reports folder id and the shared secret. The
 * repository copies hold placeholders, because the files are published, so updating a
 * live script by pasting the repository copy over it blanked every one of those values
 * and reports or notices stopped without an error. That trap is why the one redeploy that
 * mattered (RUNBOOK section 1c) was owed for nine weeks.
 *
 * Now the code holds no live value at all. Every one lives in the project's Script
 * Properties, which survive any paste, any clasp push and any new version. Pasting a
 * script is safe again.
 *
 * ONE PROJECT (since 2026-10-16)
 * ------------------------------
 * The four separate projects (report publisher, submission notifier, verdict notifier,
 * update form) are now ONE project, "MalAvi curation": every .gs file in this directory
 * is pasted into it as its own file. One project means one set of Script Properties, one
 * authorization, one place to install triggers (triggers.gs) and one status sheet that
 * every trigger can write to (status_sheet.gs). The report publisher's existing
 * deployment keeps its /exec URL: the merge is done by adding files to THAT project.
 *
 * LOAD ORDER
 * ----------
 * Apps Script runs each file's top-level statements in the order the files appear in
 * the project, so a top-level `var X = settingList_('X')` in a file that loads before
 * this one fails. Nothing in this project reads a setting at top level any more: every
 * file calls the accessor functions below (curators_(), attentionTo_(), ...) at the
 * moment it needs the value. Each file declares the settings it needs as a global named
 * SETTINGS_NEEDED_<SOMETHING>; showSettings() collects all of them after every file has
 * loaded, whatever the order.
 *
 * WHERE THE VALUES ARE SET
 * ------------------------
 * In the Apps Script editor: Project Settings (the gear icon in the left bar) > Script
 * Properties > "Add script property". Property name exactly as listed in SETTING_NAMES
 * below; value as described. Lists are comma-separated. The live values themselves are
 * recorded in CUSTODY_PRIVATE.md, never here.
 *
 * Then run showSettings(): it prints each needed setting as set or MISSING (a secret is
 * shown by its length only) and ends with READY or NOT READY. Nothing is sent, written or
 * deployed by running it.
 */

// Every setting any MalAvi file reads, with what goes in it and who needs it.
var SETTING_NAMES = {
  CURATORS: 'Comma-separated email addresses told about reports, verdicts and updates. ' +
            'Keep in step with config/curators.yml.',
  ATTENTION_TO: 'Comma-separated LEAD curator addresses: told when the intake cycle needs ' +
                'a person, and the recipients of the daily digest (daily_clock.gs).',
  ARRIVAL_TO: 'Optional. Who is told the moment a submission arrives on the form, before ' +
              'the checks have run (notify_on_submission.gs). Empty = ATTENTION_TO. ' +
              'Curators asked (2026-10-16) to receive only the report email, so the ' +
              'default tells the leads only.',
  ASSIGNEES: 'Optional. Comma-separated addresses a submission may be ASSIGNED to in ' +
             'rotation when its report is published (status_sheet.gs). Empty = CURATORS. ' +
             'Take someone out of the rotation without removing them as a curator by ' +
             'listing everyone else here.',
  REPORTS_FOLDER_ID: 'The id of the Drive folder that holds curator reports (publish_report.gs).',
  SHARED_SECRET: 'The report secret, equal to ~/.config/malavi/report_secret.txt on ' +
                 'BIOMIX (openssl rand -hex 32). publish_report.gs. A secret.',
  SUBMISSION_FORM_ID: 'The submission form id (between /forms/d/ and /edit in its edit ' +
                      'URL). notify_on_submission.gs.',
  VERDICT_FORM_ID: 'The verdict form id. notify_on_verdict.gs, the inspector, and ' +
                   'add_submitter_message_question.gs.',
  UPDATE_FORM_ID: 'The update form id. create_update_form.gs.',
  UPDATE_FORM_URL: 'Optional. The public (viewform) URL of the update form, put into ' +
                   'messages that ask a submitter for a corrected file.',
  STATUS_SHEET_ID: 'The id of the private "MalAvi submissions" status spreadsheet ' +
                   '(status_sheet.gs; run createStatusSheet once and copy the id it ' +
                   'prints). Until it is set, nothing is written to a sheet and the ' +
                   'inbox triage and daily clock do nothing.',
  MAIL_MODE: 'REQUIRED. live (send), redirect (send everything to MAIL_REDIRECT_TO with ' +
             'the real recipient in the subject) or dry (send nothing, record and count). ' +
             'UNSET MEANS DRY: nothing is sent until this is set (mail.gs, 2026-10-16).',
  MAIL_REDIRECT_TO: 'Optional. Where every message goes while MAIL_MODE is redirect ' +
                    '(normally the maintainer). Required in redirect mode.',
  MAIL_MAX_PER_RUN: 'Optional. Messages one execution may send; the rest are held ' +
                    '(default 25).',
  MAIL_QUOTA_RESERVE: 'Optional. Sends kept back from the daily quota for alarms and ' +
                      'fallbacks (default 20).',
  MAIL_DEDUPE_DAYS: 'Optional. How long a sent message is remembered so the same one ' +
                    'is not sent to the same person again (default 30).'
};

// Settings whose value is never printed, only its length.
var SECRET_SETTINGS = ['SHARED_SECRET'];

// Settings a project can work without. showSettings lists them as "not set (optional)"
// rather than MISSING, and they never make it NOT READY.
// UPDATE_FORM_ID is optional until the update form exists (RUNBOOK 4a); installAllTriggers
// skips its trigger while it is empty.
var OPTIONAL_SETTINGS = ['ARRIVAL_TO', 'ASSIGNEES', 'UPDATE_FORM_URL', 'STATUS_SHEET_ID',
                         'UPDATE_FORM_ID', 'MAIL_REDIRECT_TO', 'MAIL_MAX_PER_RUN',
                         'MAIL_QUOTA_RESERVE', 'MAIL_DEDUPE_DAYS'];


/** One setting, trimmed; '' when it is not set. Never throws, so a script whose settings
 *  are incomplete still loads and showSettings() can say what is missing. */
function setting_(name) {
  var value = PropertiesService.getScriptProperties().getProperty(name);
  return value ? String(value).trim() : '';
}


/** A list setting: split on commas, semicolons or whitespace, empties dropped. Brackets
 *  and quotes are stripped from each item, so a JavaScript array literal copied out of
 *  the old code (['a@example.edu', 'b@example.org']) still yields the two addresses. */
function settingList_(name) {
  return setting_(name).split(/[\s,;]+/)
      .map(function (item) { return item.replace(/[\[\]'"<>]/g, ''); })
      .filter(function (item) { return item; });
}


// ---------------------------------------------------------------------------------------
// Accessors. Called at the moment a value is needed, never at a file's top level.
// ---------------------------------------------------------------------------------------

/** Every curator who is told about reports, verdicts and updates. */
function curators_() { return settingList_('CURATORS'); }

/** The lead curators: attention notices and the daily digest. Falls back to the first
 *  curator so a lead-only message is never addressed to nobody. */
function attentionTo_() {
  var leads = settingList_('ATTENTION_TO');
  return leads.length ? leads : curators_().slice(0, 1);
}

/** Who hears that a submission arrived, before any check has run. */
function arrivalTo_() {
  var listed = settingList_('ARRIVAL_TO');
  return listed.length ? listed : attentionTo_();
}

/** Who a submission may be assigned to, in rotation. */
function assignees_() {
  var listed = settingList_('ASSIGNEES');
  return listed.length ? listed : curators_();
}


/** Where a failure notice goes: the first lead, or, before any list is set, the account
 *  that owns the script (malaviadmin), so a failure is never addressed to nobody. */
function fallbackAddress_() {
  var leads = attentionTo_();
  return leads.length ? leads[0] : Session.getEffectiveUser().getEmail();
}


/** Stop with a readable sentence when a needed setting is empty. For the functions a
 *  person runs by hand (installing a trigger, sending a sample). */
function requireSetting_(name) {
  var value = setting_(name);
  if (!value) {
    throw new Error(name + ' is not set. Project Settings > Script Properties > Add ' +
                    'script property, then run showSettings.');
  }
  return value;
}


/** Items of an address list that do not look like one address each. */
function malformedAddresses_(name) {
  return settingList_(name).filter(function (item) {
    return !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(item);
  });
}


/**
 * The settings this project needs: the union of every global named SETTINGS_NEEDED or
 * SETTINGS_NEEDED_<FILE>, collected after every file has loaded. A project with none
 * declared needs every known setting.
 */
function settingsNeeded_() {
  var scope = (typeof globalThis !== 'undefined') ? globalThis : this;
  var names = [];
  var keys = Object.keys(scope);
  for (var i = 0; i < keys.length; i++) {
    if (keys[i].indexOf('SETTINGS_NEEDED') !== 0) continue;
    var declared = scope[keys[i]];
    if (!declared || !declared.length) continue;
    for (var j = 0; j < declared.length; j++) {
      if (names.indexOf(declared[j]) === -1) names.push(declared[j]);
    }
  }
  return names.length ? names : Object.keys(SETTING_NAMES);
}


/** The needed, non-optional settings that are empty. */
function missingSettings_() {
  var needed = settingsNeeded_();
  var missing = [];
  for (var i = 0; i < needed.length; i++) {
    if (OPTIONAL_SETTINGS.indexOf(needed[i]) !== -1) continue;
    if (!setting_(needed[i])) missing.push(needed[i]);
  }
  return missing;
}


/**
 * Print each needed setting as set or MISSING, and READY or NOT READY. Run this after
 * pasting a script and after any change to the Script Properties. It sends nothing.
 */
function showSettings() {
  var needed = settingsNeeded_();
  var lines = [];
  var malformed = [];
  var addressLists = ['CURATORS', 'ATTENTION_TO', 'ARRIVAL_TO', 'ASSIGNEES'];
  for (var i = 0; i < needed.length; i++) {
    var name = needed[i];
    var value = setting_(name);
    var shown;
    if (!value) {
      shown = OPTIONAL_SETTINGS.indexOf(name) !== -1 ? 'not set (optional)' : 'MISSING';
    } else if (SECRET_SETTINGS.indexOf(name) !== -1) {
      shown = 'set (' + value.length + ' characters)';
    } else if (addressLists.indexOf(name) !== -1) {
      var bad = malformedAddresses_(name);
      shown = 'set (' + settingList_(name).length + ' address(es)): ' +
              settingList_(name).join(', ') +
              (bad.length ? '\n      MALFORMED: ' + bad.join(', ') +
                            ' (one plain address per item, comma-separated)' : '');
      if (bad.length) malformed.push(name);
    } else {
      shown = 'set: ' + value;
    }
    lines.push(name + ': ' + shown + '\n      ' + (SETTING_NAMES[name] || ''));
  }
  var mode = setting_('MAIL_MODE').toLowerCase();
  if (['live', 'redirect', 'dry'].indexOf(mode) === -1) {
    lines.push('MAIL_MODE is ' + (mode ? 'not one of live/redirect/dry' : 'not set') +
               ': NOTHING IS SENT until it is set (mail.gs).');
    if (missingSettings_().indexOf('MAIL_MODE') === -1) malformed.push('MAIL_MODE');
  } else if (mode !== 'live') {
    lines.push('MAIL_MODE is ' + mode + ': no message reaches its real recipient.');
  }
  var missing = missingSettings_();
  if (missing.length || malformed.length) {
    lines.push('NOT READY: ' +
               (missing.length ? 'set ' + missing.join(', ') + ' ' : '') +
               (malformed.length ? 'fix ' + malformed.join(', ') + ' ' : '') +
               'under Project Settings > Script Properties, then run showSettings again.');
  } else {
    lines.push('READY: every setting this project needs is set.');
  }
  Logger.log(lines.join('\n'));
}
