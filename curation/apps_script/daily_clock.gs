/**
 * The daily clock: reminders to whoever a submission is waiting on, and one digest a day
 * to the leads.
 *
 * WHY
 * ---
 * The curators agreed (2026-10-15/16) to one assigned reviewer per submission, a two-week
 * clock, and reminders at 7 and 14 days. Before this, the only reminder in the system was
 * `ledger.stale_live`, which told the maintainer, after 30 days, only when promote.py ran.
 * Submissions sat for weeks with nobody written to. This runs once a day on Google's own
 * timer (triggers.gs), reads the status sheet, and sends what the rules below say. It
 * never changes a verdict or a state: it only reminds, and it logs every reminder.
 *
 * THE RULES (one row = one submission on the Submissions tab)
 * ----------------------------------------------------------
 *   needs a verdict   state ready_for_review / in_review (or not yet synced) with a report
 *                     and no verdict by the assignee since that report:
 *                       >= 7 days  -> remind the assignee
 *                       >= 14 days -> remind every curator (the assignee by name)
 *                       >= 21 days -> REASSIGN to the next available curator; both are
 *                                     told; the first is released, not nagged again
 *                                     (what Kubernetes and Flutter do when an owner's
 *                                     clock runs out; research report item 2)
 *                     Unassigned rows with a report and no verdict are ASSIGNED first
 *                     (the backlog from before assignment existed), and the new assignee
 *                     is emailed.
 *   about to go       state awaiting_submitter for 53 days or more -> digest ("goes
 *   dormant           dormant this week"), so the 60-day step is never a surprise
 *                     (research report item 8: automation must not close silently).
 *   flagged           state held: the flag's message to the submitter was not sent by the
 *                     verdict trigger and nobody has written to the submitter since the
 *                     flag -> after 3 days remind the curator who flagged (else the
 *                     assignee) to write.
 *   waiting on the    state awaiting_submitter: >= 14 days since the submitter was last
 *   submitter         written to -> a short reminder to the submitter, with the update-form
 *                     link; again at >= 30 days. Never after that: the ledger's 60-day
 *                     clock takes over (dormant).
 *   arrival with no   an Arrivals row more than 2 days old with no matching Submissions
 *   report            row -> digest ("the screen has not run").
 *   hold elapsed,     state approved with hold_ends more than a day in the past and the
 *   nothing happened  sheet not synced since -> digest ("the cycle has not run").
 *   sheet stale       the newest synced_at older than 36 hours -> digest, first line.
 *   unfiled mail      Mail rows without an id since the last digest -> digest.
 *
 * Every reminder to a person is throttled by the row's last_reminder (nothing twice within
 * six days); the submitter reminders are additionally keyed by threshold in the Log so each
 * fires once. One email per recipient per day, all their reminders in it.
 *
 * THE DIGEST
 * ----------
 * To the leads (ATTENTION_TO), every day there is anything open: the whole queue as a
 * table, then the alerts. It is the one email the maintainer needs instead of the stream.
 */

var SETTINGS_NEEDED_CLOCK = ['STATUS_SHEET_ID', 'CURATORS', 'ATTENTION_TO', 'UPDATE_FORM_URL'];

var REMIND_ASSIGNEE_DAYS = 7;
var REMIND_EVERYONE_DAYS = 14;
var REASSIGN_AFTER_DAYS = 21;
// The ledger's awaiting-submitter timeout (config/project.yml, 60 days) minus a week.
var DORMANT_WARNING_DAYS = 53;
var REMIND_WRITE_TO_SUBMITTER_DAYS = 3;
var REMIND_SUBMITTER_DAYS = [14, 30];
var REMINDER_THROTTLE_DAYS = 6;
var ARRIVAL_WITHOUT_REPORT_DAYS = 2;
var ARRIVAL_GIVE_UP_DAYS = 14;
var SHEET_STALE_HOURS = 36;

var STATES_NEED_VERDICT = ['ready_for_review', 'in_review'];


/**
 * The trigger. Once a day. Does nothing without a status sheet.
 */
function dailyClock() {
  if (!statusSheetEnabled_()) return;
  withSheetLock_(dailyClockLocked_);
}


function dailyClockLocked_() {
  var now = new Date();
  var today = isoNow_(now);
  var rows = readTable_(TAB_SUBMISSIONS);
  var verdicts = readTable_(TAB_VERDICTS);
  var arrivals = readTable_(TAB_ARRIVALS);
  var mail = readTable_(TAB_MAIL);
  var log = readTable_(TAB_LOG);

  var reminders = {};        // address -> [lines]
  var alerts = [];           // lines for the digest
  var changed = 0;

  function remind(address, line) {
    var key = String(address || '').trim();
    if (!key) return;
    if (!reminders[key]) reminders[key] = [];
    reminders[key].push(line);
  }

  // ---- 0. assign the backlog ------------------------------------------------------------
  for (var b = 0; b < rows.length; b++) {
    var row0 = rows[b];
    var open0 = !row0.state || LIVE_STATES.indexOf(row0.state) !== -1;
    var needs0 = !row0.state || STATES_NEED_VERDICT.indexOf(row0.state) !== -1;
    if (!open0 || !needs0 || row0.assignee || !row0.report_at) continue;
    if (verdictsFor_(verdicts, row0.id, row0.report_at).length) continue;
    try {
      var who = nextAssignee_();
      writeFields_(TAB_SUBMISSIONS, row0._row, { assignee: who, assigned_at: today });
      row0.assignee = who;
      row0.assigned_at = today;
      logEvent_('dailyClock', row0.id + ' was unassigned; assigned to ' + who);
      remind(who, 'ASSIGNED TO YOU: ' + row0.id + ' (report of ' +
             String(row0.report_at).slice(0, 10) + ', no verdict yet). Report: ' +
             (row0.report_url || 'see the status sheet') + '. Please record a verdict ' +
             'within two weeks.');
      changed += 1;
    } catch (err0) {
      alerts.push(row0.id + ' is unassigned and could not be assigned: ' + err0);
    }
  }

  // ---- 1. per-row rules -----------------------------------------------------------------
  var newestSync = '';
  for (var i = 0; i < rows.length; i++) {
    var row = rows[i];
    if (row.synced_at && row.synced_at > newestSync) newestSync = row.synced_at;
    var open = !row.state || LIVE_STATES.indexOf(row.state) !== -1;
    if (!open) continue;
    var throttled = row.last_reminder && daysBetween_(row.last_reminder, now) < REMINDER_THROTTLE_DAYS;

    // needs a verdict
    var needsVerdict = (!row.state || STATES_NEED_VERDICT.indexOf(row.state) !== -1) && row.report_at;
    if (needsVerdict) {
      // Counted from the assignment when there is one: a curator who got it yesterday
      // (backlog or reassignment) is not behind on a report that is three weeks old
      // (review 2026-10-16, finding 5).
      var waitedDays = daysBetween_(row.assignee ? (row.assigned_at || row.report_at)
                                                 : row.report_at, now);
      var byAssignee = row.assignee
          ? verdictsFor_(verdicts, row.id, row.report_at, row.assignee).length : 0;
      if (row.assignee && !byAssignee && waitedDays !== null) {
        var sinceAssigned = daysBetween_(row.assigned_at || row.report_at, now);
        if (sinceAssigned !== null && sinceAssigned >= REASSIGN_AFTER_DAYS) {
          try {
            var relieved = row.assignee;
            var next = nextAssignee_([relieved]);
            if (next.toLowerCase() !== relieved.toLowerCase()) {
              reassignSubmission_(row.id, next, 'no verdict from ' + curatorName_(relieved) +
                                  ' in ' + sinceAssigned + ' days; reassigned by the clock');
              row.assignee = next; row.assigned_at = today; row.last_reminder = '';
              alerts.push(row.id + ': reassigned from ' + curatorName_(relieved) + ' to ' +
                          curatorName_(next) + ' after ' + sinceAssigned + ' days without a verdict');
              changed += 1;
              continue;
            }
          } catch (errR) {
            alerts.push(row.id + ': could not be reassigned: ' + errR);
          }
        }
        if (waitedDays >= REMIND_EVERYONE_DAYS && !throttled) {
          var everyone = curators_();
          for (var c = 0; c < everyone.length; c++) {
            remind(everyone[c], row.id + ' has waited ' + waitedDays + ' days for a verdict ' +
                   '(assigned to ' + curatorName_(row.assignee) + '). Anyone may record ' +
                   'one. Report: ' + (row.report_url || 'see the status sheet'));
          }
          markReminded_(row, today); changed += 1;
          alerts.push(row.id + ': ' + waitedDays + ' days without a verdict; everyone reminded');
        } else if (waitedDays >= REMIND_ASSIGNEE_DAYS && !throttled) {
          remind(row.assignee, row.id + ' has waited ' + waitedDays + ' days for your ' +
                 'verdict. Report: ' + (row.report_url || 'see the status sheet') +
                 '. If you cannot take it, reply to this email and a lead will reassign it.');
          markReminded_(row, today); changed += 1;
        }
      }
    }

    // flagged, submitter not yet written to
    if (row.state === 'held') {
      var flags = verdictsFor_(verdicts, row.id, '', '', 'Flag for further review');
      if (!flags.length && row.waiting_on === 'flag' && row.waiting_since) {
        // A flag recorded before the sheet existed (or on a form the trigger missed): the
        // ledger sync still says the row waits on a flag and since when. Treat that as the
        // flag, with the assignee as the person to write (first live day, 2026-10-16: the
        // three held submissions had no Verdicts row and nobody was nudged).
        flags = [{ recorded_at: row.waiting_since, curator: row.assignee || '',
                   sent_to_submitter_at: '' }];
      }
      if (flags.length) {
        var latest = flags[flags.length - 1];
        var writtenSince = row.last_to_submitter && row.last_to_submitter >= latest.recorded_at;
        // A reply from the submitter after the flag means they were written to, by
        // somebody, somehow; do not ask anyone to write again (finding 1).
        var repliedSince = row.last_from_submitter && row.last_from_submitter >= latest.recorded_at;
        if (!latest.sent_to_submitter_at && !writtenSince && !repliedSince &&
            daysBetween_(latest.recorded_at, now) >= REMIND_WRITE_TO_SUBMITTER_DAYS &&
            !throttled) {
          var writer = onCuratorList(latest.curator) ? latest.curator : (row.assignee || '');
          if (writer) {
            remind(writer, row.id + ' was flagged on ' + String(latest.recorded_at).slice(0, 10) +
                   ' and the submitter has not been written to. Please write to ' +
                   (row.submitter_email || 'them') + ' (copy the MalAvi address so the ' +
                   'message is filed), or reply here and a lead will.');
            markReminded_(row, today); changed += 1;
          }
          alerts.push(row.id + ': flagged ' + daysBetween_(latest.recorded_at, now) +
                      ' days ago, submitter not written to' +
                      (writer ? ' (reminder to ' + curatorName_(writer) + ')' : ''));
        }
      }
    }

    // waiting on the submitter
    if (row.state === 'awaiting_submitter' && row.waiting_since &&
        daysBetween_(row.waiting_since, now) >= DORMANT_WARNING_DAYS) {
      alerts.push(row.id + ': waiting on the submitter for ' + daysBetween_(row.waiting_since, now) +
                  ' days; goes dormant at 60 (names kept). Write to them now if that is wrong.');
    }
    if (row.state === 'awaiting_submitter' && row.submitter_email) {
      // The wait is measured from when the ledger started it, never from our last
      // message (a reminder must not postpone the next one; finding 7). A reply from the
      // submitter since then means the ball is with us: no reminder to them, an alert to
      // the leads instead (finding 1: the ledger stays awaiting_submitter until a curator
      // files what the reply says).
      var since = row.waiting_since || row.received;
      var waiting = daysBetween_(since, now);
      var replied = row.last_from_submitter && row.last_from_submitter >= since;
      var recentlyWritten = row.last_to_submitter && daysBetween_(row.last_to_submitter, now) < REMINDER_THROTTLE_DAYS;
      if (replied) {
        alerts.push(row.id + ': the submitter wrote on ' + String(row.last_from_submitter).slice(0, 10) +
                    ' but the ledger still says awaiting_submitter; a curator should file ' +
                    'their answer (correction or update) or write back');
      } else if (!recentlyWritten) {
        for (var th = REMIND_SUBMITTER_DAYS.length - 1; th >= 0; th--) {
          var threshold = REMIND_SUBMITTER_DAYS[th];
          if (waiting === null || waiting < threshold) continue;
          var key = 'remind:' + row.id + ':' + threshold;
          if (loggedBefore_(log, key)) break;
          if (!sendSubmitterReminder_(row, waiting)) {
            // Held by mail.gs: not logged as sent, so it is tried again tomorrow.
            alerts.push(row.id + ': submitter reminder HELD by the mail guard (' + waiting +
                        ' days); see the Mail tab');
            break;
          }
          logEvent_('dailyClock', key + ' sent to ' + row.submitter_email);
          alerts.push(row.id + ': submitter reminded (' + waiting + ' days)');
          changed += 1;
          break;
        }
      }
    }

    // approved, hold elapsed, nothing moved
    if (row.state === 'approved' && row.hold_ends && row.hold_ends < today &&
        daysBetween_(row.hold_ends, now) >= 1 && (!row.synced_at || row.synced_at < row.hold_ends)) {
      alerts.push(row.id + ': publish hold ended ' + String(row.hold_ends).slice(0, 10) +
                  ' and the ledger has not synced since; the intake cycle has not run');
    }
  }

  // ---- 2. arrivals with no report -------------------------------------------------------
  for (var a = 0; a < arrivals.length; a++) {
    var arrival = arrivals[a];
    if (arrival.matched_id) continue;
    var match = matchArrival_(arrival, rows);
    if (match) {
      writeFields_(TAB_ARRIVALS, arrival._row, { matched_id: match.id });
      continue;
    }
    var age = daysBetween_(arrival.form_timestamp, now);
    if (age !== null && age > ARRIVAL_GIVE_UP_DAYS) {
      // A test or spam response never gets a row; after two weeks stop asking about it
      // (finding 14). Any text in matched_id silences the alert.
      writeFields_(TAB_ARRIVALS, arrival._row, { matched_id: 'no row after ' + age + ' days; ignored' });
      continue;
    }
    if (age !== null && age >= ARRIVAL_WITHOUT_REPORT_DAYS) {
      alerts.push('a submission from ' + (arrival.submitter_name || arrival.submitter_email ||
                  'an unknown sender') + ' arrived ' + String(arrival.form_timestamp).slice(0, 10) +
                  ' and has no report after ' + age + ' days: the screen has not run');
    }
  }

  // ---- 3. the sheet itself ---------------------------------------------------------------
  var staleHours = newestSync ? (now.getTime() - parseStamp_(newestSync).getTime()) / 3600000 : null;
  if (staleHours === null) {
    alerts.unshift('the ledger has NEVER synced to this sheet (no synced_at); states and ' +
                   '"waiting on" below come from Google only');
  } else if (staleHours > SHEET_STALE_HOURS) {
    alerts.unshift('the ledger last synced ' + Math.round(staleHours) + ' hours ago: the ' +
                   'intake cycle is not running (RUNBOOK 4b)');
  }

  // ---- 4. unfiled mail since yesterday ---------------------------------------------------
  var cutoff = isoNow_(new Date(now.getTime() - 25 * 3600000));
  var unfiled = [];
  for (var u = 0; u < mail.length; u++) {
    if (mail[u].direction === 'in' && !mail[u].id && mail[u].at >= cutoff) {
      unfiled.push(mail[u].from + ': ' + (mail[u].subject || '(no subject)') +
                   (mail[u].thread_url ? ' ' + mail[u].thread_url : ''));
    }
  }

  // ---- send -------------------------------------------------------------------------------
  var sent = 0;
  for (var address in reminders) {
    var lines = ['[this is an automatic email]', '',
                 'Reminders from the MalAvi status sheet, ' + today.slice(0, 10) + ':', ''];
    for (var L = 0; L < reminders[address].length; L++) lines.push('- ' + reminders[address][L]);
    lines.push('');
    lines.push('Verdicts are recorded through the link in each report. This message is sent ' +
               'once a day at most; replying to it reaches the MalAvi address and the leads.');
    lines.push('', '--', 'Confidential. A submission may contain unpublished sequences.');
    // One reminder email per person per day: the key is the day, so a clock run twice
    // in one day (a hand run after the trigger) sends it once.
    var result = sendMail_({ to: address,
                             subject: 'MalAvi: ' + reminders[address].length + ' reminder' +
                                      (reminders[address].length === 1 ? '' : 's') + ' for today',
                             body: lines.join('\n'), kind: 'curator',
                             // The day AND the content: a hand run later the same day with
                             // something new to say sends; the same list does not.
                             key: 'reminders|' + today.slice(0, 10) + '|' +
                                  mailSha_(reminders[address].join('\n')),
                             note: 'daily reminders' });
    if (result.sent) sent += 1;
  }

  var digestSent = sendDigest_(rows, alerts, unfiled, today);
  logEvent_('dailyClock', sent + ' reminder email(s), digest ' + (digestSent ? 'sent' : 'not needed') +
            ', ' + alerts.length + ' alert(s), ' + changed + ' row change(s)');
}


/** Verdict rows for an id, optionally after a stamp, by a curator, with a given verdict. */
function verdictsFor_(verdicts, id, after, curator, verdict) {
  var out = [];
  var who = String(curator || '').toLowerCase();
  for (var i = 0; i < verdicts.length; i++) {
    var v = verdicts[i];
    if (v.id !== id || !v.verdict) continue;
    if (after && v.recorded_at < after) continue;
    if (who && String(v.curator || '').toLowerCase() !== who) continue;
    if (verdict && v.verdict !== verdict) continue;
    out.push(v);
  }
  return out;
}


function markReminded_(row, today) {
  writeFields_(TAB_SUBMISSIONS, row._row, { last_reminder: today });
  row.last_reminder = today;
}


/** Has a Log row with this message prefix been written before? */
function loggedBefore_(log, key) {
  for (var i = 0; i < log.length; i++) {
    if (String(log[i].message || '').indexOf(key + ' ') === 0) return true;
  }
  return false;
}


/** The Submissions row an Arrivals row belongs to: same address, received after the form
 *  timestamp minus a day (the fetch records the form's own time). */
function matchArrival_(arrival, rows) {
  var address = (arrival.submitter_email || '').toLowerCase();
  if (!address) return null;
  var floor = parseStamp_(arrival.form_timestamp);
  var best = null;
  for (var i = 0; i < rows.length; i++) {
    if ((rows[i].submitter_email || '').toLowerCase() !== address) continue;
    var received = parseStamp_(rows[i].received);
    if (floor && received && received.getTime() < floor.getTime() - 86400000) continue;
    if (!best || (rows[i].received || '') < (best.received || '')) best = rows[i];
  }
  return best;
}


/** The reminder to a submitter we are waiting on. Recorded as outgoing mail. */
function sendSubmitterReminder_(row, waitingDays) {
  var name = row.submitter_name ? row.submitter_name.split(/\s+/)[0] : 'colleague';
  var updateForm = setting_('UPDATE_FORM_URL');
  var lines = [
    'Dear ' + name + ',',
    '',
    'Your MalAvi submission ' + row.id + ' is waiting for a reply from you (about ' +
      waitingDays + ' days now). The earlier message from the curators says what is needed.',
    '',
    updateForm
      ? 'If the answer is a corrected template, upload it through the update form: ' + updateForm +
        '. Otherwise, reply to this email.'
      : 'Please reply to this email; a corrected template can be attached to the reply.',
    '',
    'Your reserved lineage names are kept while we wait. A submission that stays unanswered ' +
      'for 60 days is set aside until we hear from you; nothing is lost and nothing is ' +
      'given away.',
    '',
    'Best regards,',
    'The MalAvi curators',
    'https://malavi-db.github.io/'
  ];
  var copies = row.assignee ? { cc: row.assignee } : {};
  var result = sendAndRecord_(row.submitter_email, 'MalAvi submission ' + row.id +
                              ': still waiting for your reply', lines.join('\n'), copies,
                              row.id, 'reminder at ' + waitingDays + ' days');
  if (!result.sent) return false;        // held: nothing stamped, tried again tomorrow
  // last_to_submitter records the contact; the next threshold is still measured from
  // waiting_since (see the caller), so this does not postpone it.
  writeFields_(TAB_SUBMISSIONS, row._row, { last_to_submitter: isoNow_(), last_reminder: isoNow_() });
  return true;
  row.last_to_submitter = isoNow_();
}


/** The leads' digest. Returns true if one was sent. */
function sendDigest_(rows, alerts, unfiled, today) {
  var open = [];
  for (var i = 0; i < rows.length; i++) {
    if (!rows[i].state || LIVE_STATES.indexOf(rows[i].state) !== -1) open.push(rows[i]);
  }
  if (!open.length && !alerts.length && !unfiled.length) return false;

  open.sort(function (a, b) {
    return String(a.waiting_since || a.received) < String(b.waiting_since || b.received) ? -1 : 1;
  });
  var lines = ['[this is an automatic email]', '',
               'MalAvi queue, ' + today.slice(0, 10) + ': ' + open.length + ' open submission' +
               (open.length === 1 ? '' : 's') + '.', ''];
  if (alerts.length) {
    lines.push('NEEDS ATTENTION');
    for (var a = 0; a < alerts.length; a++) lines.push('- ' + alerts[a]);
    lines.push('');
  }
  lines.push('THE QUEUE (longest wait first)');
  for (var o = 0; o < open.length; o++) {
    var r = open[o];
    var since = r.waiting_since || r.report_at || r.received;
    var days = daysBetween_(since, new Date());
    lines.push('- ' + r.id + ' | ' + (r.state || 'not synced') + ' | waiting on ' +
               (r.waiting_on || '?') + (days !== null ? ' for ' + days + ' d' : '') +
               ' | assigned: ' + (r.assignee ? curatorName_(r.assignee) : 'NOBODY') +
               (r.verdicts ? ' | ' + r.verdicts : ' | no verdicts') +
               (r.embargoed === 'yes' ? ' | embargoed' : ''));
  }
  lines.push('');
  if (unfiled.length) {
    lines.push('UNFILED MAIL since yesterday (no submission id, sender not a known submitter)');
    for (var u = 0; u < unfiled.length; u++) lines.push('- ' + unfiled[u]);
    lines.push('');
  }
  lines.push('The status sheet has the detail: ' + statusSpreadsheet_().getUrl());
  lines.push(mailStatusLine_());
  lines.push('', '--', 'Confidential. Names of submitters appear above; do not forward.');

  var leads = attentionTo_();
  var delivered = false;
  for (var L = 0; L < leads.length; L++) {
    var result = sendMail_({ to: leads[L],
                             subject: 'MalAvi daily: ' + open.length + ' open' +
                                      (alerts.length ? ', ' + alerts.length + ' need attention' : ''),
                             body: lines.join('\n'), kind: 'lead',
                             key: 'digest|' + today.slice(0, 10), note: 'daily digest' });
    if (result.sent) delivered = true;
  }
  return delivered;
}


/** HAND FUNCTION. Run the clock now. It sends whatever is due, exactly as the trigger would. */
function dailyClockNow() {
  if (!statusSheetEnabled_()) { Logger.log('STATUS_SHEET_ID is not set.'); return; }
  dailyClock();
  Logger.log('Done; see the Log tab for what was sent.');
}
