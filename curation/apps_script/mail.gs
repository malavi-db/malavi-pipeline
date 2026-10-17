/**
 * mail.gs: the ONE place this project sends email.
 *
 * WHY
 * ---
 * On 2026-10-16 the first live intake cycle with the review state in the repository
 * re-applied thirteen old verdict-form responses and the endpoint sent the curators nine
 * duplicate notices. Every send in the project called GmailApp directly, in twenty
 * places, so nothing stood between a program's mistake and a curator's inbox. Vincenzo
 * asked for guard rails: no incorrect or unneeded email, during development or in
 * production (results/GUARDRAILS_PLAN_2026-10-16.md, section 1.1).
 *
 * Now nothing else in the project calls GmailApp.sendEmail, MailApp.sendEmail or
 * GmailMessage.forward; a test (curation/tests/test_apps_script_mail.py) fails if one
 * does. Every message goes through sendMail_ or forwardMail_, which apply, in order:
 *
 *   1. the recipient rules: a message for a curator or a lead goes only to an address on
 *      CURATORS, ATTENTION_TO or ASSIGNEES; a message for a submitter must not mention
 *      another submitter's submission id;
 *   2. MAIL_MODE (Script Property): live sends; dry sends nothing; redirect sends every
 *      message to MAIL_REDIRECT_TO instead, with the real recipient in the subject.
 *      UNSET MEANS DRY: a pasted bundle sends nothing until the property is set, and
 *      showSettings says NOT READY until it is. Properties first, paste second;
 *   3. the duplicate check: the message's key (given by the caller for machine-originated
 *      mail, where it names the RESPONSE or the REPORT the message is about; derived from
 *      the text otherwise), plus the recipient, is remembered in Script Properties for
 *      MAIL_DEDUPE_DAYS. The same message to the same person is sent once;
 *   4. the per-execution cap MAIL_MAX_PER_RUN and the daily quota reserve
 *      MAIL_QUOTA_RESERVE (MailApp.getRemainingDailyQuota, as the inbox triage already
 *      does).
 *
 * A message that is not sent is HELD: recorded on the Mail tab with the reason, counted
 * for the day, reported to the caller as { sent: false, held: <reason> }, and the
 * endpoint tells the Python side the same in its reply so nothing is recorded as sent
 * that was not. A held message is never silently lost: in dry and redirect mode one
 * alarm a day goes to the fallback address (the first lead) saying how many were held
 * and why, and the daily digest carries the count.
 *
 * `force: true` bypasses the mode, the caps and the duplicate check. It exists for the
 * alarm itself and for the "notice FAILED" fallbacks, which are the channel that says
 * something is wrong; it is still recorded.
 *
 * FAIL CLOSED. The duplicate memory is written BEFORE the send and forgotten again if
 * the send throws: a memory that cannot be written holds the message rather than
 * sending it unremembered (review of 2026-10-16, finding 11). The Mail tab is the
 * human record and is never the mechanism; recordMail_ never throws.
 */

var SETTINGS_NEEDED_MAIL = ['MAIL_MODE', 'MAIL_REDIRECT_TO', 'MAIL_MAX_PER_RUN',
                            'MAIL_QUOTA_RESERVE', 'MAIL_DEDUPE_DAYS',
                            'CURATORS', 'ATTENTION_TO', 'ASSIGNEES'];

var MAIL_MODES = ['live', 'redirect', 'dry'];
var MAIL_DEFAULT_MAX_PER_RUN = 25;      // above any legitimate single execution (~15)
var MAIL_DEFAULT_QUOTA_RESERVE = 20;    // sends kept back for alarms and fallbacks
var MAIL_DEFAULT_DEDUPE_DAYS = 30;

// Script Property prefixes. Properties survive every paste and every new version.
var MAIL_MEMORY_PREFIX = 'mail:';       // mail:<key> -> epoch ms of the send
var MAIL_HELD_PREFIX = 'mailheld:';     // mailheld:<UTC day> -> messages held that day
var MAIL_ALARM_PREFIX = 'mailalarm:';   // mailalarm:<UTC day>:<reason> -> alarm sent
var MAIL_MEMORY_PRUNE_AT = 300;         // prune expired memory once this many properties exist
var MAIL_COUNTER_KEEP_DAYS = 14;

// Per execution. Apps Script runs each trigger or request in a fresh execution.
var mailRunCount_ = 0;        // messages actually sent in this execution
var mailRunHeld_ = [];        // [{ to, reason }] held in this execution
var mailCapAlarmed_ = false;  // the per-run cap alarm goes once per execution

var SUBMISSION_ID_RE = /MALAVI-SUB-\d{4}-\d{6}/g;


// ---------------------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------------------

// A request may ask for a MORE restrictive mode than the property for its own execution
// (doPost sets this from the payload's mail_mode, so a maintainer running the machine side
// with MALAVI_MAIL_MODE=redirect receives the mail even while the project is live). Never
// less restrictive: live > redirect > dry, and the property is the ceiling.
var mailRequestedMode_ = '';
var MAIL_RANK = { live: 0, redirect: 1, dry: 2 };

/** The mode in force: the more restrictive of the Script Property and the request's
 *  own wish. Anything else, including unset, is dry. */
function mailMode_() {
  var value = setting_('MAIL_MODE').toLowerCase();
  var mode = MAIL_MODES.indexOf(value) === -1 ? 'dry' : value;
  var wanted = String(mailRequestedMode_ || '').toLowerCase();
  if (MAIL_MODES.indexOf(wanted) !== -1 && MAIL_RANK[wanted] > MAIL_RANK[mode]) mode = wanted;
  return mode;
}

/** Called by doPost with the payload's mail_mode (a downgrade request); ignored unless
 *  it names one of the three modes. */
function requestMailMode_(wanted) {
  mailRequestedMode_ = String(wanted || '').toLowerCase();
}

/** Whether MAIL_MODE is set to one of the three words (unset is dry but not explicit). */
function mailModeIsExplicit_() {
  return MAIL_MODES.indexOf(setting_('MAIL_MODE').toLowerCase()) !== -1;
}

/** A numeric setting with a default; a setting that is not a positive number is ignored. */
function mailNumber_(name, fallback) {
  var number = Number(setting_(name));
  return number > 0 ? number : fallback;
}

/** Every address a curator- or lead-kind message may go to, lowercase, as a set. */
function mailKnownAddresses_() {
  var known = {};
  var lists = [curators_(), attentionTo_(), assignees_()];
  for (var i = 0; i < lists.length; i++) {
    for (var j = 0; j < lists[i].length; j++) known[String(lists[i][j]).toLowerCase()] = true;
  }
  return known;
}


// ---------------------------------------------------------------------------------------
// Keys and memory
// ---------------------------------------------------------------------------------------

/** SHA-256 of a string, as 32 hex characters. */
function mailSha_(text) {
  var bytes = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, String(text),
                                      Utilities.Charset.UTF_8);
  var out = '';
  for (var i = 0; i < bytes.length && out.length < 32; i++) {
    var b = bytes[i] < 0 ? bytes[i] + 256 : bytes[i];
    out += (b < 16 ? '0' : '') + b.toString(16);
  }
  return out;
}

/** Today as YYYY-MM-DD, UTC. */
function mailUtcDay_(date) {
  return (date || new Date()).toISOString().slice(0, 10);
}

/** Whether this key was sent within the duplicate window. */
function mailAlreadySent_(key) {
  var stamp = PropertiesService.getScriptProperties().getProperty(MAIL_MEMORY_PREFIX + key);
  if (!stamp) return false;
  var age = Date.now() - Number(stamp);
  return age >= 0 && age < mailNumber_('MAIL_DEDUPE_DAYS', MAIL_DEFAULT_DEDUPE_DAYS) * 86400000;
}

/** Remember a key as sent now. Returns false when the memory could not be written. */
function mailRemember_(key) {
  try {
    var properties = PropertiesService.getScriptProperties();
    properties.setProperty(MAIL_MEMORY_PREFIX + key, String(Date.now()));
    if (Object.keys(properties.getProperties()).length > MAIL_MEMORY_PRUNE_AT) pruneMailMemory_();
    return true;
  } catch (err) {
    Logger.log('mail memory could not be written: ' + err);
    return false;
  }
}

/** Forget a key (a send that threw after the key was remembered). Best effort. */
function mailForget_(key) {
  try { PropertiesService.getScriptProperties().deleteProperty(MAIL_MEMORY_PREFIX + key); }
  catch (err) { Logger.log('mail memory could not be cleared: ' + err); }
}

/** Delete expired memory, old daily counters and old alarm markers. */
function pruneMailMemory_() {
  var properties = PropertiesService.getScriptProperties();
  var all = properties.getProperties();
  var now = Date.now();
  var window = mailNumber_('MAIL_DEDUPE_DAYS', MAIL_DEFAULT_DEDUPE_DAYS) * 86400000;
  var keepDay = mailUtcDay_(new Date(now - MAIL_COUNTER_KEEP_DAYS * 86400000));
  var removed = 0;
  for (var name in all) {
    if (!all.hasOwnProperty(name)) continue;
    var expired = false;
    if (name.indexOf(MAIL_MEMORY_PREFIX) === 0) {
      expired = now - Number(all[name]) > window;
    } else if (name.indexOf(MAIL_HELD_PREFIX) === 0 || name.indexOf(MAIL_ALARM_PREFIX) === 0) {
      expired = name.split(':')[1] < keepDay;
    }
    if (expired) { properties.deleteProperty(name); removed += 1; }
  }
  return removed;
}

/** Count one held message for today. Never throws. */
function mailCountHeld_() {
  try {
    var properties = PropertiesService.getScriptProperties();
    var name = MAIL_HELD_PREFIX + mailUtcDay_();
    properties.setProperty(name, String(Number(properties.getProperty(name) || 0) + 1));
  } catch (err) {
    Logger.log('held counter could not be written: ' + err);
  }
}

/** How many messages were held today (UTC). */
function mailHeldToday_() {
  return Number(PropertiesService.getScriptProperties()
                    .getProperty(MAIL_HELD_PREFIX + mailUtcDay_()) || 0);
}


// ---------------------------------------------------------------------------------------
// The recipient rules
// ---------------------------------------------------------------------------------------

/** Submission ids mentioned in a text, unique, in order. */
function mailSubmissionIds_(text) {
  var found = String(text || '').match(SUBMISSION_ID_RE) || [];
  var unique = [];
  for (var i = 0; i < found.length; i++) if (unique.indexOf(found[i]) === -1) unique.push(found[i]);
  return unique;
}

/**
 * The first submission id in the text whose Submissions row belongs to a DIFFERENT
 * submitter than `to`, or ''. A submitter may be told about their own other submissions
 * (one person often has two in the queue; review 2026-10-16, finding 12), never about
 * somebody else's. Without a status sheet nothing can be checked, and ids are then
 * allowed through: the sheet is the only source of who owns what.
 */
function mailForeignSubmissionId_(to, text, ownId) {
  if (!statusSheetEnabled_()) return '';
  var ids = mailSubmissionIds_(text);
  var address = String(to || '').toLowerCase();
  for (var i = 0; i < ids.length; i++) {
    if (ids[i] === ownId) continue;
    var row = null;
    try { row = findRow_(TAB_SUBMISSIONS, 'id', ids[i]); } catch (err) { row = null; }
    if (!row) continue;                   // unknown here: nothing to compare with
    var owner = String(row.submitter_email || '').toLowerCase();
    if (owner && owner !== address) return ids[i];
  }
  return '';
}


// ---------------------------------------------------------------------------------------
// Sending
// ---------------------------------------------------------------------------------------

/**
 * Send one email, subject to every rule above.
 *
 *   message = {
 *     to, subject, body,          required
 *     options,                    passed to GmailApp.sendEmail (cc, replyTo, htmlBody, name)
 *     kind,                       'curator' | 'lead' | 'submitter' | 'system'
 *     submissionId,               for the Mail tab and the submitter rule
 *     key,                        the message's identity (the caller's, or derived)
 *     note,                       for the Mail tab
 *     force                       bypass mode, caps and the duplicate check (alarms only)
 *   }
 *
 * Returns { sent: true, to, key } or { sent: false, held: <reason>, to }. Throws only
 * when GmailApp itself threw, after recording the failure, so a caller that sends to
 * several people can catch per address exactly as before.
 */
function sendMail_(message) {
  var to = String(message.to || '').trim();
  var kind = String(message.kind || 'system');
  var subject = String(message.subject || '');
  var body = String(message.body || '');
  var options = {};
  var given = message.options || {};
  for (var name in given) if (given.hasOwnProperty(name)) options[name] = given[name];
  if (!options.name) options.name = 'MalAvi';
  var force = !!message.force;
  var mode = mailMode_();
  var record = { direction: 'out', id: message.submissionId || '', from: 'MalAvi', to: to,
                 subject: subject, kind: kind, mode: mode, key: '',
                 note: message.note || '' };

  function held(reason) {
    record.note = (record.note ? record.note + '; ' : '') + 'NOT SENT: ' + reason;
    recordMail_(record);
    mailCountHeld_();
    mailRunHeld_.push({ to: to, reason: reason });
    return { sent: false, held: reason, to: to };
  }

  if (!to || to.indexOf('@') === -1) return held('no usable address');
  if (!force && (kind === 'curator' || kind === 'lead') &&
      !mailKnownAddresses_()[to.toLowerCase()]) {
    return held('address not on CURATORS, ATTENTION_TO or ASSIGNEES');
  }
  if (!force && kind === 'submitter') {
    var foreign = mailForeignSubmissionId_(to, subject + '\n' + body, message.submissionId);
    if (foreign) return held('the text mentions ' + foreign + ', another submitter\'s submission');
  }

  var base = String(message.key || '') ||
             mailSha_([kind, message.submissionId || '', to.toLowerCase(), subject, body].join('|'));
  var key = mailSha_(base + '|' + to.toLowerCase());
  record.key = key;

  if (!force) {
    if (mode === 'dry') {
      mailAlarm_('dry');
      return held('MAIL_MODE=' + (mailModeIsExplicit_() ? 'dry' : 'unset (dry)'));
    }
    if (mode === 'live' && mailAlreadySent_(key)) return held('duplicate');
    if (mailRunCount_ >= mailNumber_('MAIL_MAX_PER_RUN', MAIL_DEFAULT_MAX_PER_RUN)) {
      if (!mailCapAlarmed_) { mailCapAlarmed_ = true; mailAlarm_('cap'); }
      return held('per-run cap');
    }
    var quota = MailApp.getRemainingDailyQuota();
    if (quota <= mailNumber_('MAIL_QUOTA_RESERVE', MAIL_DEFAULT_QUOTA_RESERVE)) {
      return held('daily quota reserve (' + quota + ' left)');
    }
  }

  var actualTo = to;
  var actualSubject = subject;
  var redirected = false;
  if (!force && mode === 'redirect') {
    var redirectTo = setting_('MAIL_REDIRECT_TO');
    if (redirectTo.indexOf('@') === -1) {
      return held('MAIL_MODE=redirect but MAIL_REDIRECT_TO is not set');
    }
    actualTo = redirectTo;
    actualSubject = '[REDIRECT to ' + to + '] ' + subject;
    // A redirected message must not carry its real cc list either.
    delete options.cc;
    delete options.bcc;
    redirected = true;
    mailAlarm_('redirect');
  }

  var remembered = false;
  if (!force && mode === 'live') {
    if (!mailRemember_(key)) return held('the send memory could not be written');
    remembered = true;
  }
  try {
    GmailApp.sendEmail(actualTo, actualSubject, body, options);
  } catch (err) {
    if (remembered) mailForget_(key);
    record.note = (record.note ? record.note + '; ' : '') + 'SEND FAILED: ' + err;
    recordMail_(record);
    throw err;
  }
  mailRunCount_ += 1;
  if (redirected) {
    record.mode = 'redirect';
    record.note = (record.note ? record.note + '; ' : '') + 'redirected to ' + actualTo;
    recordMail_(record);
    mailCountHeld_();
    mailRunHeld_.push({ to: to, reason: 'redirected' });
    return { sent: false, held: 'redirected', to: to, key: key };
  }
  if (force) record.mode = mode + ' (forced)';
  recordMail_(record);
  return { sent: true, to: to, key: key };
}


/**
 * Forward a Gmail message to several recipients under the same rules, as ONE forward.
 * Each recipient is checked (kind rule, duplicate of this message id to this address,
 * cap, quota); those that pass receive one forward. Returns { sent: <count>, held: [...] }.
 */
function forwardMail_(gmailMessage, recipients, options, kind, submissionId, note) {
  var mode = mailMode_();
  var messageId = gmailMessage.getId();
  var subject = String((options && options.subject) || gmailMessage.getSubject() || '');
  var known = mailKnownAddresses_();
  var send = [];
  var result = { sent: 0, held: [] };

  function held(to, reason) {
    recordMail_({ direction: 'out', id: submissionId || '', from: 'MalAvi', to: to,
                  subject: subject, kind: kind, mode: mode, key: '',
                  note: (note ? note + '; ' : '') + 'NOT SENT (forward): ' + reason });
    mailCountHeld_();
    mailRunHeld_.push({ to: to, reason: reason });
    result.held.push({ to: to, reason: reason });
  }

  for (var i = 0; i < recipients.length; i++) {
    var to = String(recipients[i] || '').trim();
    if (!to || to.indexOf('@') === -1) { held(to, 'no usable address'); continue; }
    if ((kind === 'curator' || kind === 'lead') && !known[to.toLowerCase()]) {
      held(to, 'address not on CURATORS, ATTENTION_TO or ASSIGNEES'); continue;
    }
    var key = mailSha_('forward|' + messageId + '|' + to.toLowerCase());
    if (mode === 'dry') { mailAlarm_('dry'); held(to, 'MAIL_MODE=' + (mailModeIsExplicit_() ? 'dry' : 'unset (dry)')); continue; }
    if (mode === 'live' && mailAlreadySent_(key)) { held(to, 'duplicate'); continue; }
    if (mailRunCount_ + send.length >= mailNumber_('MAIL_MAX_PER_RUN', MAIL_DEFAULT_MAX_PER_RUN)) {
      if (!mailCapAlarmed_) { mailCapAlarmed_ = true; mailAlarm_('cap'); }
      held(to, 'per-run cap'); continue;
    }
    send.push({ to: to, key: key });
  }
  if (!send.length) return result;
  var quota = MailApp.getRemainingDailyQuota();
  if (quota - send.length <= mailNumber_('MAIL_QUOTA_RESERVE', MAIL_DEFAULT_QUOTA_RESERVE)) {
    for (var q = 0; q < send.length; q++) held(send[q].to, 'daily quota reserve (' + quota + ' left)');
    return result;
  }

  var settings = {};
  for (var name in (options || {})) if (options.hasOwnProperty(name)) settings[name] = options[name];
  if (!settings.name) settings.name = 'MalAvi';
  var addresses = send.map(function (s) { return s.to; });
  var target = addresses.join(',');
  if (mode === 'redirect') {
    var redirectTo = setting_('MAIL_REDIRECT_TO');
    if (redirectTo.indexOf('@') === -1) {
      for (var r = 0; r < send.length; r++) held(send[r].to, 'MAIL_MODE=redirect but MAIL_REDIRECT_TO is not set');
      return result;
    }
    target = redirectTo;
    settings.subject = '[REDIRECT to ' + addresses.join(', ') + '] ' + subject;
    delete settings.cc;
    delete settings.bcc;
    mailAlarm_('redirect');
  } else {
    for (var m = 0; m < send.length; m++) {
      if (!mailRemember_(send[m].key)) {
        for (var h = 0; h < send.length; h++) held(send[h].to, 'the send memory could not be written');
        return result;
      }
    }
  }
  try {
    gmailMessage.forward(target, settings);
  } catch (err) {
    for (var f = 0; f < send.length; f++) {
      if (mode === 'live') mailForget_(send[f].key);
      held(send[f].to, 'FORWARD FAILED: ' + err);
    }
    return result;
  }
  mailRunCount_ += send.length;
  for (var s = 0; s < send.length; s++) {
    if (mode === 'redirect') {
      recordMail_({ direction: 'out', id: submissionId || '', from: 'MalAvi', to: send[s].to,
                    subject: subject, kind: kind, mode: 'redirect', key: send[s].key,
                    note: (note ? note + '; ' : '') + 'forward redirected to ' + target });
      mailCountHeld_();
      mailRunHeld_.push({ to: send[s].to, reason: 'redirected' });
      result.held.push({ to: send[s].to, reason: 'redirected' });
    } else {
      recordMail_({ direction: 'out', id: submissionId || '', from: 'MalAvi', to: send[s].to,
                    subject: subject, kind: kind, mode: mode, key: send[s].key,
                    note: (note ? note + '; ' : '') + 'forwarded' });
      result.sent += 1;
    }
  }
  return result;
}


// ---------------------------------------------------------------------------------------
// The alarm: a held message is never silent for more than a day
// ---------------------------------------------------------------------------------------

/**
 * Once per UTC day per reason, tell the fallback address that mail is being held. A
 * forced send: it bypasses the mode, the caps and the duplicate check, and is recorded.
 */
function mailAlarm_(reason) {
  var properties;
  var marker = MAIL_ALARM_PREFIX + mailUtcDay_() + ':' + reason;
  try {
    properties = PropertiesService.getScriptProperties();
    if (properties.getProperty(marker)) return false;
    properties.setProperty(marker, String(Date.now()));
  } catch (err) {
    Logger.log('alarm marker could not be written: ' + err);
    return false;
  }
  var mode = mailMode_();
  var explicit = mailModeIsExplicit_();
  var subject, lines;
  if (reason === 'cap') {
    subject = 'MalAvi: the per-run mail cap was reached';
    lines = ['[this is an automatic email from the MalAvi curation project]', '',
             'One execution tried to send more than MAIL_MAX_PER_RUN (' +
             mailNumber_('MAIL_MAX_PER_RUN', MAIL_DEFAULT_MAX_PER_RUN) + ') messages. The ' +
             'rest were held and recorded on the Mail tab with the reason "per-run cap".',
             '', 'A legitimate execution never sends that many. Look at the Mail tab before ' +
             'raising the cap.'];
  } else {
    subject = 'MalAvi: mail is being held (MAIL_MODE=' +
              (explicit ? mode : 'unset') + ')';
    lines = ['[this is an automatic email from the MalAvi curation project]', '',
             'MAIL_MODE is ' + (explicit ? mode : 'NOT SET, which means dry') + '. ' +
             (mode === 'redirect'
               ? 'Every message is being sent to ' + setting_('MAIL_REDIRECT_TO') +
                 ' instead of its real recipient.'
               : 'Nothing the project is asked to send reaches anyone.'),
             '', 'Messages held so far today (UTC): ' + mailHeldToday_() + '. Each one is on ' +
             'the Mail tab of the status sheet with its reason.',
             '', 'If this is a development window, nothing to do; this reminder comes once ' +
             'a day. If it is not, set MAIL_MODE to live under Project Settings > Script ' +
             'Properties. Messages from the intake programs are retried by their next ' +
             'run; the form triggers\' messages (arrival, verdict, submitter message) are ' +
             'not, and the Mail tab says which ones they were.'];
  }
  // In redirect mode the alarm goes to the person reading the redirected mail, not to
  // the first lead: during development nobody else hears from the project.
  var redirectTo = setting_('MAIL_REDIRECT_TO');
  var to = (mode === 'redirect' && redirectTo.indexOf('@') !== -1) ? redirectTo
                                                                   : fallbackAddress_();
  try {
    sendMail_({ to: to, subject: subject, body: lines.join('\n'),
                kind: 'system', force: true, note: 'mail guard alarm: ' + reason });
    return true;
  } catch (err) {
    Logger.log('the mail alarm itself could not be sent: ' + err);
    // The day's marker is forgotten so the next held message tries the alarm again.
    try { properties.deleteProperty(marker); } catch (err2) { Logger.log('' + err2); }
    return false;
  }
}


/** One line for the daily digest and for describe_. */
function mailStatusLine_() {
  var mode = mailMode_();
  return 'Mail guard: MAIL_MODE ' + (mailModeIsExplicit_() ? mode : 'unset (dry)') +
         '; ' + mailHeldToday_() + ' message(s) held today; quota left ' +
         MailApp.getRemainingDailyQuota();
}


// ---------------------------------------------------------------------------------------
// Hand functions
// ---------------------------------------------------------------------------------------

/** HAND FUNCTION. Print the mode, today's held count, the memory size and the quota. */
function showMailStatus() {
  var all = PropertiesService.getScriptProperties().getProperties();
  var remembered = 0;
  for (var name in all) if (name.indexOf(MAIL_MEMORY_PREFIX) === 0) remembered += 1;
  Logger.log(mailStatusLine_() + '\n' + remembered + ' message key(s) in the duplicate ' +
             'memory (window ' + mailNumber_('MAIL_DEDUPE_DAYS', MAIL_DEFAULT_DEDUPE_DAYS) +
             ' days); per-run cap ' + mailNumber_('MAIL_MAX_PER_RUN', MAIL_DEFAULT_MAX_PER_RUN) +
             '; quota reserve ' + mailNumber_('MAIL_QUOTA_RESERVE', MAIL_DEFAULT_QUOTA_RESERVE) +
             (mailModeIsExplicit_() ? '' : '\nNOT READY: set MAIL_MODE (live, redirect or dry).'));
}

/** HAND FUNCTION. Send one test message to the first lead, under the rules in force:
 *  in dry mode it is held and says so; in redirect mode it goes to MAIL_REDIRECT_TO. */
function sendMailTest() {
  var result = sendMail_({ to: attentionTo_()[0] || fallbackAddress_(),
                           subject: 'MalAvi: mail guard test',
                           body: '[this is an automatic email]\n\nsendMailTest in the MalAvi ' +
                                 'curation project. ' + mailStatusLine_(),
                           kind: 'lead', key: 'mailtest|' + Date.now(),
                           note: 'sendMailTest' });
  Logger.log(result.sent ? 'Sent to ' + result.to + '.' : 'HELD (' + result.held + ') for ' +
             result.to + '. ' + mailStatusLine_());
}

/** HAND FUNCTION. Prune expired memory now and say how much went. */
function pruneMailMemoryNow() {
  Logger.log('removed ' + pruneMailMemory_() + ' expired propert(y/ies)');
}

/**
 * HAND FUNCTION, dangerous. Forget every sent message: the next request for any of them
 * would be sent again. Only for a rebuilt project whose memory is known to be wrong.
 */
function clearMailMemory() {
  var properties = PropertiesService.getScriptProperties();
  var all = properties.getProperties();
  var removed = 0;
  for (var name in all) {
    if (name.indexOf(MAIL_MEMORY_PREFIX) === 0) { properties.deleteProperty(name); removed += 1; }
  }
  logEvent_('clearMailMemory', 'cleared ' + removed + ' message key(s)');
  Logger.log('cleared ' + removed + ' message key(s)');
}
