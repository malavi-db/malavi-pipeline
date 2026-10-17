/**
 * Triage the MalAvi inbox: file every incoming message under its submission, forward it to
 * the curator who owns that submission, and record it on the status sheet.
 *
 * WHY THIS EXISTS
 * ---------------
 * Mail to malaviadmin@gmail.com forwards to one person (CUSTODY_PRIVATE.md, set
 * 2026-09-24). A submitter's reply, a corrected workbook, a curator's note all land in
 * that one inbox, mixed with everything else, and nothing records who was written to or
 * when. By 2026-10-16 the maintainer was "overwhelmed by the emails of submissions" and
 * could not tell what was waiting on whom. This script is the answer that needs no new
 * service and no Google Group: the account reads its own inbox on a timer and does the
 * sorting a person was doing by hand.
 *
 * WHAT IT DOES, EVERY 15 MINUTES (triggers.gs)
 * --------------------------------------------
 * For each message in the inbox from the last 14 days that it has not seen before (the
 * Mail tab remembers message ids) and that the account did not send itself:
 *   1. Find the submission: a MALAVI-SUB-YYYY-NNNNNN id anywhere in the subject or body,
 *      else the sender's address matched against the submitter addresses on the
 *      Submissions tab (the most recent open one wins).
 *   2. Matched: label the thread "MalAvi/filed" and "MalAvi/<id>"; write a Mail row;
 *      if the sender is THE SUBMITTER of that submission (the address on the Submissions
 *      row), set last_from_submitter and FORWARD the message to the assignee and the leads
 *      (never to whoever sent it, never to people already on the message), with the id in
 *      the subject and reply-to set to the submitter, so "Reply" answers them and "Reply
 *      all" keeps the MalAvi address in the thread. Mail from anyone else that mentions an
 *      id (a curator's reply-all, a stranger quoting a public id) is filed and logged but
 *      NOT forwarded: curators are already on their own threads, and ids are public
 *      (review 2026-10-16, finding 2).
 *   3. Not matched: label the thread "MalAvi/unfiled" and write a Mail row with no id.
 *      Nothing is forwarded; the daily digest lists these for the leads. (Gmail's own
 *      forwarding to the custodian, if still on, is untouched by this script.)
 * It also records the account's own SENT mail of the last 14 days to a submitter's
 * address (a lead writing from malaviadmin by hand) as an outgoing Mail row and sets
 * last_to_submitter, so the clock knows the submitter was written to.
 *
 * WHAT IT NEVER DOES
 * ------------------
 * It deletes nothing, archives nothing, marks nothing read, and never replies. It never
 * forwards an unmatched message anywhere: a stranger's mail must not be able to make this
 * account relay text to the curators. The body of a message is read only to look for a
 * submission id and is quoted nowhere.
 *
 * QUOTA
 * -----
 * Each forward is one send against the account's daily limit (about 100 recipients a day
 * for a consumer account, shared with every report and notice). At most
 * MAX_MESSAGES_PER_RUN messages are handled and MAX_FORWARDS_PER_RUN forwarded per run,
 * only submitters' mail is forwarded, and nothing is forwarded when the remaining daily
 * quota is below QUOTA_RESERVE, so a burst of mail cannot silence the real notices.
 */

var SETTINGS_NEEDED_TRIAGE = ['STATUS_SHEET_ID', 'ATTENTION_TO'];

var LABEL_FILED = 'MalAvi/filed';
var LABEL_UNFILED = 'MalAvi/unfiled';
var LABEL_PREFIX = 'MalAvi/';

var SUBMISSION_ID_RE = /MALAVI-SUB-\d{4}-\d{6}/;
var MAX_MESSAGES_PER_RUN = 40;
var MAX_FORWARDS_PER_RUN = 10;
var QUOTA_RESERVE = 30;
var TRIAGE_WINDOW_DAYS = 14;

// Senders that are machinery, not people. Their mail is left alone entirely.
var TRIAGE_SKIP_SENDERS = /(noreply|no-reply|do-not-reply|notification|mailer-daemon|postmaster|forms-receipts|drive-shares|calendar-notification)@/i;


/** The address this script runs as (malaviadmin), lower case. Falls back to the active
 *  user; '' when neither is known, which the caller treats as "do not run" rather than
 *  risk forwarding the account's own replies (review 2026-10-16, "could not verify"). */
function ownAddress_() {
  var address = '';
  try { address = Session.getEffectiveUser().getEmail() || ''; } catch (err) { address = ''; }
  if (!address) {
    try { address = Session.getActiveUser().getEmail() || ''; } catch (err2) { address = ''; }
  }
  return String(address).trim().toLowerCase();
}


/** The plain address out of "Name <addr>" or a bare address, lower case. */
function bareAddress_(text) {
  var found = String(text || '').match(/[^<\s,;"']+@[^>\s,;"']+/);
  return found ? found[0].toLowerCase() : '';
}


/** A Gmail label by name, created if missing. */
function label_(name) {
  return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name);
}


/** The message ids already on the Mail tab, as a set. */
function knownMessageIds_() {
  var rows = readTable_(TAB_MAIL);
  var known = {};
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].message_id) known[rows[i].message_id] = true;
  }
  return known;
}


/**
 * Submitter address -> the Submissions row to file their mail under. When one person has
 * several submissions, the open one received most recently wins; a closed one is used
 * only when nothing is open.
 */
function submitterIndex_(rows) {
  var index = {};
  for (var i = 0; i < rows.length; i++) {
    var address = (rows[i].submitter_email || '').toLowerCase();
    if (!address) continue;
    var isOpen = !rows[i].state || LIVE_STATES.indexOf(rows[i].state) !== -1;
    var current = index[address];
    if (!current) { index[address] = rows[i]; index[address]._open = isOpen; continue; }
    if (isOpen && !current._open) { index[address] = rows[i]; rows[i]._open = true; continue; }
    if (isOpen === current._open && (rows[i].received || '') > (current.received || '')) {
      index[address] = rows[i]; rows[i]._open = isOpen;
    }
  }
  return index;
}


/** The submission a message belongs to: by id in the text, else by sender. '' if none. */
function matchSubmission_(message, rows, index) {
  var text = message.getSubject() + '\n' + message.getPlainBody().slice(0, 20000);
  var found = text.match(SUBMISSION_ID_RE);
  if (found) return found[0];
  var sender = bareAddress_(message.getFrom());
  var row = sender ? index[sender] : null;
  return row ? row.id : '';
}


/**
 * The trigger. Runs every 15 minutes; does nothing without a status sheet.
 */
function triageInbox() {
  if (!statusSheetEnabled_()) return;
  withSheetLock_(triageInboxLocked_);
}


function triageInboxLocked_() {
  var me = ownAddress_();
  if (!me) {
    logEvent_('triageInbox', 'SKIPPED: the account\'s own address is not known in this ' +
              'context, so our own replies could not be told from incoming mail');
    return;
  }
  var quotaLeft = MailApp.getRemainingDailyQuota();
  var known = knownMessageIds_();
  var rows = readTable_(TAB_SUBMISSIONS);
  var index = submitterIndex_(rows);
  var handled = 0, filed = 0, unfiled = 0, forwarded = 0, outgoing = 0;

  // ---- incoming ---------------------------------------------------------------------
  var threads = GmailApp.search('in:inbox newer_than:' + TRIAGE_WINDOW_DAYS + 'd -in:spam ' +
                                '-in:trash', 0, 60);
  for (var t = 0; t < threads.length && handled < MAX_MESSAGES_PER_RUN; t++) {
    var messages = threads[t].getMessages();
    for (var m = 0; m < messages.length && handled < MAX_MESSAGES_PER_RUN; m++) {
      var message = messages[m];
      var messageId = message.getId();
      if (known[messageId]) continue;
      var sender = bareAddress_(message.getFrom());
      if (!sender || sender === me) continue;                 // our own sends
      if (TRIAGE_SKIP_SENDERS.test(sender)) continue;         // machinery
      if (daysBetween_(message.getDate(), new Date()) > TRIAGE_WINDOW_DAYS) continue;
      handled += 1;
      known[messageId] = true;

      var threadUrl = 'https://mail.google.com/mail/u/0/#all/' + threads[t].getId();
      var id = '';
      try { id = matchSubmission_(message, rows, index); } catch (err) { id = ''; }

      if (!id) {
        threads[t].addLabel(label_(LABEL_UNFILED));
        recordMail_({ direction: 'in', id: '', from: sender, to: me,
                      subject: message.getSubject(), thread_url: threadUrl,
                      message_id: messageId, note: 'unfiled: no submission id and no ' +
                      'known submitter address' });
        unfiled += 1;
        continue;
      }

      threads[t].addLabel(label_(LABEL_FILED));
      threads[t].addLabel(label_(LABEL_PREFIX + id));
      var row = null;
      for (var r = 0; r < rows.length; r++) if (rows[r].id === id) { row = rows[r]; break; }
      var fromSubmitter = !!(row && row.submitter_email &&
                             row.submitter_email.toLowerCase() === sender);
      var note = fromSubmitter ? 'from the submitter' : 'about the submission (not from its submitter)';

      // The Mail row is written BEFORE the forward: the row is the dedupe record, and a
      // forward that went out without one would go out again every run (finding 6).
      var mailRow = recordMail_({ direction: 'in', id: id, from: sender, to: me,
                                  subject: message.getSubject(), thread_url: threadUrl,
                                  message_id: messageId, note: note });
      if (!mailRow) {
        logEvent_('triageInbox', 'Mail tab write failed for ' + messageId + '; not forwarding');
        filed += 1;
        continue;
      }

      // Only the submitter's own mail is forwarded. Curators are on their own threads;
      // strangers can quote a public id (finding 2).
      var recipients = fromSubmitter ? forwardRecipients_(row, sender, message) : [];
      if (recipients.length && forwarded >= MAX_FORWARDS_PER_RUN) {
        note += '; forward deferred to the next run (per-run cap)';
        known[messageId] = false;        // not handled: picked up next run
        writeFields_(TAB_MAIL, mailRow, { note: note });
        // Leave the row so the message is not re-recorded; clear its id so it is retried.
        writeFields_(TAB_MAIL, mailRow, { message_id: '' });
        continue;
      }
      if (recipients.length) {
        // mail.gs applies the mode, the duplicate check (this message id to this
        // address), the per-run cap and the quota reserve, and records each recipient.
        var outcome = forwardMail_(message, recipients, {
          subject: '[MalAvi ' + id + '] ' + message.getSubject(),
          name: 'MalAvi',
          replyTo: sender,
          htmlBody: forwardPreface_(id, row, sender) + message.getBody()
        }, 'curator', id, 'forward of the submitter\'s mail');
        if (outcome.sent) {
          forwarded += 1;
          quotaLeft -= outcome.sent;
          note += '; forwarded to ' + outcome.sent + ' address(es)';
        }
        var retry = false;
        for (var h = 0; h < outcome.held.length; h++) {
          note += '; NOT forwarded to ' + outcome.held[h].to + ': ' + outcome.held[h].reason;
          if (outcome.held[h].reason !== 'duplicate') retry = true;
        }
        if (retry && !outcome.sent) {
          // Held by the mode, a cap or the quota: clear the id so the next run tries
          // again, the same way the per-run cap above does (code review 2026-10-16, 12).
          known[messageId] = false;
          writeFields_(TAB_MAIL, mailRow, { message_id: '' });
        }
      } else if (fromSubmitter) {
        note += '; not forwarded (everyone to tell is already on the message)';
      }
      writeFields_(TAB_MAIL, mailRow, { note: note });
      if (row && fromSubmitter) {
        var stamp = isoNow_(message.getDate());
        if (!row.last_from_submitter || row.last_from_submitter < stamp) {
          writeFields_(TAB_SUBMISSIONS, row._row, { last_from_submitter: stamp });
          row.last_from_submitter = stamp;
        }
      }
      filed += 1;
    }
  }

  // ---- outgoing, written by hand from this account --------------------------------------
  var sentThreads = GmailApp.search('in:sent newer_than:' + TRIAGE_WINDOW_DAYS + 'd', 0, 40);
  for (var s = 0; s < sentThreads.length; s++) {
    var sentMessages = sentThreads[s].getMessages();
    for (var k = 0; k < sentMessages.length; k++) {
      var out = sentMessages[k];
      var outId = out.getId();
      if (known[outId]) continue;
      if (bareAddress_(out.getFrom()) !== me) continue;
      // Automatic sends are already on the Mail tab (sendAndRecord_) or carry a marker.
      var outBody = out.getPlainBody();
      if (outBody.indexOf('[this is an automatic email]') !== -1 ||
          outBody.indexOf(AUTOMATIC_FOOTER) !== -1 ||
          outBody.indexOf('[forwarded automatically by MalAvi]') !== -1 ||
          out.getSubject().indexOf('[MalAvi MALAVI-SUB-') === 0) continue;   // our own forwards
      var toAddresses = String(out.getTo() + ',' + out.getCc()).toLowerCase()
          .match(/[^<\s,;"']+@[^>\s,;"']+/g) || [];
      var matchedRow = null;
      for (var a = 0; a < toAddresses.length && !matchedRow; a++) {
        matchedRow = index[toAddresses[a]] || null;
      }
      var subjectId = (out.getSubject() + out.getPlainBody().slice(0, 5000)).match(SUBMISSION_ID_RE);
      var outSubmission = subjectId ? subjectId[0] : (matchedRow ? matchedRow.id : '');
      if (!outSubmission) continue;                 // unrelated mail from the account
      known[outId] = true;
      recordMail_({ direction: 'out', id: outSubmission, from: me, to: out.getTo(),
                    subject: out.getSubject(),
                    thread_url: 'https://mail.google.com/mail/u/0/#all/' + sentThreads[s].getId(),
                    message_id: outId, note: 'written by hand from the MalAvi account' });
      outgoing += 1;
      if (matchedRow) {
        var sentStamp = isoNow_(out.getDate());
        if (!matchedRow.last_to_submitter || matchedRow.last_to_submitter < sentStamp) {
          writeFields_(TAB_SUBMISSIONS, matchedRow._row, { last_to_submitter: sentStamp });
          matchedRow.last_to_submitter = sentStamp;
        }
      }
    }
  }

  if (handled || outgoing) {
    logEvent_('triageInbox', handled + ' new incoming (' + filed + ' filed, ' + unfiled +
              ' unfiled, ' + forwarded + ' forwarded); ' + outgoing + ' outgoing recorded');
  }
}


/** Who a submitter's message is forwarded to: the assignee and the leads, never the
 *  sender, never anyone already in the message's To or Cc. */
function forwardRecipients_(row, sender, message) {
  var wanted = [];
  if (row && row.assignee) wanted.push(row.assignee);
  var leads = attentionTo_();
  for (var i = 0; i < leads.length; i++) wanted.push(leads[i]);
  var already = {};
  if (message) {
    var onIt = String(message.getTo() + ',' + message.getCc()).toLowerCase()
        .match(/[^<\s,;"']+@[^>\s,;"']+/g) || [];
    for (var k = 0; k < onIt.length; k++) already[onIt[k]] = true;
  }
  var seen = {};
  var out = [];
  for (var j = 0; j < wanted.length; j++) {
    var address = wanted[j].toLowerCase();
    if (!address || address === sender || seen[address] || already[address]) continue;
    seen[address] = true;
    out.push(wanted[j]);
  }
  return out;
}


/** The lines above a forwarded message: what it is, who owns it, how to answer. */
function forwardPreface_(id, row, sender) {
  var owner = row && row.assignee ? curatorName_(row.assignee) : 'nobody yet';
  var who = row && row.submitter_email && row.submitter_email.toLowerCase() === sender
      ? 'the submitter' + (row.submitter_name ? ' (' + row.submitter_name + ')' : '')
      : sender;
  return '<p style="font-family:sans-serif;font-size:90%;color:#444">' +
         '[forwarded automatically by MalAvi] Mail from ' + who + ' about <b>' + id +
         '</b> (assigned to ' + owner +
         (row && row.state ? '; state ' + row.state : '') + '). ' +
         'Reply answers the sender; Reply all keeps the MalAvi address in the thread, ' +
         'which is how the message is filed. The original follows.</p><hr>';
}


/** HAND FUNCTION. Run one triage pass now and print what it did. */
function triageInboxNow() {
  if (!statusSheetEnabled_()) { Logger.log('STATUS_SHEET_ID is not set.'); return; }
  triageInbox();
  var rows = readTable_(TAB_MAIL);
  Logger.log(rows.length + ' row(s) on the Mail tab. Last five:');
  for (var i = Math.max(0, rows.length - 5); i < rows.length; i++) {
    Logger.log([rows[i].at, rows[i].direction, rows[i].id || '(unfiled)', rows[i].from,
                rows[i].subject].join(' | '));
  }
}
