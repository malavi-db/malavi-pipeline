/**
 * Receive a curator report from BIOMIX, put it in Drive, and tell the curators.
 *
 * WHY THIS EXISTS RATHER THAN A SERVICE ACCOUNT
 * ---------------------------------------------
 * The plan was for a service account to upload the PDF straight to Drive. It was tested
 * before being built and it cannot work:
 *
 *     HTTP 403  storageQuotaExceeded
 *     "Service Accounts do not have storage quota. Leverage shared drives, or use
 *      OAuth delegation instead."
 *
 * A file created by a service account is owned by that service account no matter which
 * folder it goes in, and a service account has no Drive storage. Shared Drives would fix
 * it and need Google Workspace; malaviadmin@gmail.com is a consumer account. So the write
 * has to be performed by a human account, and this script runs as one.
 *
 * It also removes a wart from the original design. That version needed a second script on
 * a timer to notice new files and email about them, so a report could sit unannounced for
 * up to an hour. Here the write and the email are one request.
 *
 * THE SECURITY POSITION, STATED HONESTLY
 * --------------------------------------
 * This must be deployed "Anyone can access", because BIOMIX has no Google credential to
 * present. The endpoint is therefore public, and the shared secret is all that stands in
 * front of it. That is weaker than a key and it is a deliberate trade, so the blast radius
 * is kept small:
 *
 *   - Every request must carry an HMAC-SHA256 signature over the exact body, in ?sig=.
 *     Nothing is parsed or written before that is verified.
 *   - The signed body carries a timestamp; anything older than MAX_AGE_SECONDS is refused,
 *     so a captured request is not a permanent key.
 *   - The script can do exactly one thing: write a PDF into REPORTS_FOLDER_ID and email
 *     the curators. It never reads a submission, never lists another folder, and has no
 *     path that returns Drive contents to the caller.
 *
 * Somebody who steals the secret can put a PDF in the reports folder and cause an email.
 * That is recoverable. They cannot read submitter data, which is the thing worth guarding.
 *
 * INSTALLING IT
 * -------------
 *   1. Sign in as malaviadmin@gmail.com. Create the Drive folder that will hold curator
 *      reports. Share it as Viewer with each curator's address. Do NOT link-share it:
 *      undoing link-sharing was the entire point of the 2026-08-06 change.
 *   2. Copy the folder id out of its URL into REPORTS_FOLDER_ID below.
 *   3. Generate a secret on BIOMIX:  openssl rand -hex 32
 *      Save it there at ~/.config/malavi/report_secret.txt (chmod 600) and paste the same
 *      value into SHARED_SECRET below.
 *   4. Edit CURATORS below to match config/curators.yml.
 *   5. script.google.com > New project, paste this in, name it "MalAvi report publisher".
 *   6. Run `testSetup` once. It checks the folder and the secret without needing a
 *      request, and emails you a sample notification so you can see what a curator gets.
 *      Authorize when asked.
 *   7. Deploy > New deployment > Web app. Execute as: **Me**. Who has access: **Anyone**.
 *      Copy the /exec URL into google.report_endpoint in config/project.yml.
 *   8. From BIOMIX:  curation/publish_report.py --check
 *      then:         curation/publish_report.py <submission_id>
 *
 * THE PERMISSION THAT DOES NOT AUTO-DETECT
 * ----------------------------------------
 * Apps Script normally works out which permissions a script needs by reading it. It gets
 * this one wrong, and the way it fails is nasty: the FIRST publish of any report works,
 * because creating a file uses DriveApp, and every publish AFTERWARDS fails, because
 * replacing a file's contents uses UrlFetchApp -- which needs
 * https://www.googleapis.com/auth/script.external_request, and that scope does not get
 * requested. So the failure appears the first time somebody corrects a report, which could
 * be months later, and the error names a permission rather than a cause:
 *
 *     You do not have permission to call UrlFetchApp.fetch
 *
 * The cure is to stop letting it guess. curation/apps_script/appsscript.json in this
 * repository lists the three scopes this script actually needs; paste it over the manifest
 * in the Apps Script project (Project Settings > "Show appsscript.json manifest file in
 * editor"), then run any function once to re-authorize, then push a new version of the
 * deployment. Found the hard way on 2026-08-08, on the second publish.
 *
 * RE-DEPLOYING
 * ------------
 * Editing this file changes nothing on its own. Deploy > Manage deployments > edit the
 * existing deployment > Version: New version. Creating a *new* deployment instead gives a
 * new URL and quietly leaves the old one running the old code, which is the single easiest
 * way to spend an afternoon debugging a change that was never live.
 */

// ---------------------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------------------

// The Drive folder that holds curator reports. Shared as Viewer with each curator.
// The live value is "MalAvi curator reports" under malaviadmin@gmail.com, created
// 2026-08-08, and it is recorded in CUSTODY_PRIVATE.md rather than here. A folder id is
// not a credential -- without permission on the folder it gets you a 404 -- but this file
// is published, and printing the id would mean that any future mistake in the folder's
// sharing settings is instantly exploitable by anyone who read the repository.
var REPORTS_FOLDER_ID = 'PASTE_THE_REPORTS_FOLDER_ID_HERE';

// Must equal the contents of ~/.config/malavi/report_secret.txt on BIOMIX.
var SHARED_SECRET = 'PASTE_THE_SHARED_SECRET_HERE';

// Who is told when a report is ready. Keep in step with config/curators.yml.
//
// THE LIVE LIST IS LONGER THAN THIS ONE, DELIBERATELY. This file is published to
// malavi-db/malavi-pipeline, so a curator's personal address must not be written here;
// config/curators.yml, which is not published, is the record of who is a curator. Add the
// address in the Apps Script editor, to the live copy of this array, and leave this one
// alone.
//
// Which means: do not paste this whole file over the live script to update it. Editing
// this array in place in the editor is the supported way. Pasting also blanks the folder
// id and the shared secret below — see the warning in RUNBOOK §1c.
var CURATORS = [
  'vaellis@udel.edu'
];

// Must match MAX_AGE_SECONDS in curation/src/malavi_curation/report_delivery.py.
var MAX_AGE_SECONDS = 600;

// Apps Script will happily accept a huge body and then fail obscurely. Refuse early.
var MAX_PDF_BYTES = 8 * 1024 * 1024;

// How long this script remembers that a name confirmation went out, so that the same one
// is not mailed twice. The retry that matters arrives the next day (BIOMIX records the
// send only after the reply, so a request that timed out after the mail had gone is sent
// again by the next run); 30 days covers a run that was not made for weeks.
var CONFIRMATION_MEMORY_DAYS = 30;
var CONFIRMATION_KEY_PREFIX = 'confirmed:';


// ---------------------------------------------------------------------------------------
// The endpoint
// ---------------------------------------------------------------------------------------

/**
 * Handle one publish request.
 *
 * Every failure returns JSON with ok:false and a reason the Python side can print. It
 * never throws out of this function: an uncaught error in a web app returns an HTML error
 * page, which the caller cannot parse and which tells whoever is debugging nothing.
 */
function doPost(e) {
  try {
    if (!e || !e.postData || !e.postData.contents) {
      return reply_({ ok: false, error: 'empty request body' });
    }
    var body = e.postData.contents;

    // 1. Signature, before anything else touches the body.
    var provided = (e.parameter && e.parameter.sig) || '';
    if (!provided) {
      return reply_({ ok: false, error: 'no signature' });
    }
    if (!signatureValid_(body, provided)) {
      return reply_({ ok: false, error: 'bad signature' });
    }

    // 2. Only now is the body worth parsing.
    var payload;
    try {
      payload = JSON.parse(body);
    } catch (err) {
      return reply_({ ok: false, error: 'body is not JSON' });
    }

    // 3. Freshness, so a captured request cannot be replayed.
    var age = Math.abs((Date.now() / 1000) - Number(payload.issued_at || 0));
    if (!payload.issued_at || age > MAX_AGE_SECONDS) {
      return reply_({ ok: false, error: 'stale request (age ' + Math.round(age) + 's)' });
    }

    // 4. Which of the two jobs is this? Routing happens only after the signature and the
    //    freshness check, so an unsigned request never reaches either branch.
    var action = String(payload.action || 'publish_report');
    if (action === 'confirm_names') {
      return reply_(confirmNames_(payload));
    }
    if (action === 'decline_notice') {
      return reply_(declineNotice_(payload));
    }
    if (action === 'verdict_notice') {
      return reply_(verdictNotice_(payload));
    }

    // 5. The file itself.
    var filename = String(payload.filename || '');
    if (!filename || filename.indexOf('/') !== -1 || filename.slice(-4) !== '.pdf') {
      return reply_({ ok: false, error: 'unusable filename' });
    }
    var bytes = Utilities.base64Decode(payload.pdf_b64 || '');
    if (!bytes.length) {
      return reply_({ ok: false, error: 'empty PDF' });
    }
    if (bytes.length > MAX_PDF_BYTES) {
      return reply_({ ok: false, error: 'PDF too large (' + bytes.length + ' bytes)' });
    }

    // Verify the content matches what was signed. The signature covers the base64, so this
    // is belt and braces -- but it is the check that catches a truncated upload, which is
    // the failure that would otherwise put a half-rendered report in front of a curator.
    var digest = hex_(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes));
    if (payload.sha256 && payload.sha256 !== digest) {
      return reply_({ ok: false, error: 'checksum mismatch' });
    }

    var blob = Utilities.newBlob(bytes, 'application/pdf', filename);
    var written = writeOrUpdate_(filename, blob);

    // 5. Tell the curators, unless the caller asked us not to (a re-send during debugging).
    var notified = 0;
    if (payload.notify) {
      notified = notifyCurators_(String(payload.submission_id || ''), written);
    }

    return reply_({
      ok: true,
      fileId: written.id,
      url: written.url,
      action: written.action,
      sha256: digest,
      notified: notified
    });

  } catch (err) {
    // Anything unforeseen still comes back as JSON, with enough to act on.
    return reply_({ ok: false, error: 'unhandled: ' + (err && err.message ? err.message : err) });
  }
}


/**
 * Write the report, replacing the previous version *in place* if there is one.
 *
 * Idempotency is the point. A corrected report must land on the same file id, because the
 * link in a curator's inbox points at that id. Creating a second file would leave the
 * curator reading a superseded report with no indication that it was superseded -- the
 * worst outcome this whole path can produce.
 *
 * DriveApp cannot replace binary content, so the update goes through the Drive REST API
 * with this script's own OAuth token. Creation stays on DriveApp, which is simpler and has
 * no such limitation.
 */
function writeOrUpdate_(filename, blob) {
  var folder = DriveApp.getFolderById(REPORTS_FOLDER_ID);
  var existing = folder.getFilesByName(filename);

  if (existing.hasNext()) {
    var file = existing.next();
    var id = file.getId();
    var response = UrlFetchApp.fetch(
      'https://www.googleapis.com/upload/drive/v3/files/' + id + '?uploadType=media',
      {
        method: 'patch',
        contentType: 'application/pdf',
        payload: blob.getBytes(),
        headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
        muteHttpExceptions: true
      });
    if (response.getResponseCode() !== 200) {
      throw new Error('update failed: HTTP ' + response.getResponseCode() + ' ' +
                      response.getContentText().slice(0, 200));
    }
    return { id: id, url: file.getUrl(), action: 'updated' };
  }

  var created = folder.createFile(blob);
  return { id: created.getId(), url: created.getUrl(), action: 'created' };
}


/**
 * Tell a submitter which lineage names are theirs, once a curator has approved them.
 *
 * WHY THIS IS AUTOMATED AND THE REST OF THE SUBMITTER CORRESPONDENCE IS NOT
 * ------------------------------------------------------------------------
 * The MalAvi site tells every visitor, in the name checker, that a curator will confirm
 * their proposed name and that they should wait for that before depositing in GenBank.
 * That promise was kept by somebody remembering to write an email. A submitter who gives
 * up waiting deposits the wrong name, which is the exact failure the two-stage submission
 * process exists to prevent -- so this one message is worth automating and the rest are
 * not.
 *
 * WHEN IT FIRES, AND WHY NOT SOONER
 * ---------------------------------
 * Only after a curator has approved AND the publish hold has elapsed with no objection
 * standing. Not at screening: the name is derived from the host species, which is the
 * single thing curators most often correct, so a name confirmed by machine could be
 * withdrawn by a person. Not at approval either: the hold exists precisely so a second
 * curator can still object, and a confirmation sent into that window could be retracted.
 * A slow email is recoverable. A retracted lineage name, already in a manuscript, is not.
 *
 * WHAT IT MUST SAY
 * ----------------
 * The granted names, and -- separately and unmissably -- any name that CHANGED from what
 * was proposed. A submitter who proposed TUMIG06 and was granted TUMIG25 has to see that,
 * because the granted name is the one that goes in the paper and in GenBank.
 */
function confirmNames_(payload) {
  var to = String(payload.to || '');
  if (to.indexOf('@') === -1) {
    return { ok: false, error: 'no usable submitter address' };
  }
  var names = payload.names || [];
  if (!names.length) {
    return { ok: false, error: 'no names to confirm' };
  }

  // Sent once. BIOMIX writes its "already told them" record only after this reply
  // arrives, so a request that timed out on its side AFTER the mail had gone is sent
  // again by the next run -- and a second "your names are confirmed" invites the reader
  // to look for a difference that is not there. The key is the submission, the address
  // and the names, which is what makes two requests the same message; see
  // confirmationKey_. Checked before composing anything, and recorded only after the
  // mail has actually gone.
  var key = confirmationKey_(payload);
  if (alreadyConfirmed_(key)) {
    return { ok: true, action: 'already_sent', notified: 0 };
  }

  var corrections = payload.corrections || {};
  var changed = [];
  for (var proposed in corrections) {
    if (corrections.hasOwnProperty(proposed) && corrections[proposed] !== proposed) {
      changed.push('    ' + proposed + '  ->  ' + corrections[proposed]);
    }
  }

  var who = String(payload.submitter_name || '').trim();
  var lines = [
    '[this is an automatic email]',
    '',
    who ? 'Dear ' + who + ',' : 'Hello,',
    '',
    'This is an automatic message from MalAvi, sent once a curator has approved your',
    'submission. Replies go straight to the curators, so if anything below looks wrong,',
    'or you have a question, just reply to this email.',
    '',
    'A MalAvi curator has reviewed your submission and confirmed the lineage names below.',
    'These are the names to use in your manuscript and when you deposit the sequences in',
    'GenBank, so that the same name appears in all three places.',
    '',
    'Confirmed lineage names:'
  ];
  for (var i = 0; i < names.length; i++) {
    lines.push('    ' + names[i]);
  }

  if (changed.length) {
    lines.push('');
    lines.push('PLEASE NOTE -- the following names are NOT the ones you proposed. The name');
    lines.push('you proposed was already in use in MalAvi, so a free one was assigned:');
    lines.push('');
    lines = lines.concat(changed);
    lines.push('');
    lines.push('Use the name on the right.');
  }

  if (payload.reference) {
    lines.push('');
    lines.push('Submission: ' + payload.reference);
  }

  // What happens to the RECORDS, told from what the submitter actually selected rather
  // than as an "if you asked us to..." conditional the reader has to resolve themselves.
  //
  // The two booleans are decided in Python (form_metadata.records_are_held and
  // records_were_included) and arrive already settled. Deciding them here would put an
  // untested branch in charge of whether somebody is told their unpublished records are
  // about to be published.
  var selections = payload.selections || {};
  var stage = String(selections.stage || '').trim();
  var sending = String(selections.sending || '').trim();

  if (stage || sending) {
    lines.push('');
    if (stage && sending) {
      lines.push('You selected: "' + stage + '" data, sending "' + sending + '".');
    } else {
      lines.push('You selected: "' + (stage || sending) + '".');
    }
  }

  lines.push('');
  if (!selections.records_included) {
    // They said they were sending names and sequences only, so the host and geography
    // records are still to come. Saying so is the whole reason this branch exists.
    lines.push('Your host and geography records have not reached MalAvi. Please submit');
    lines.push('those as soon as your study is accepted for publication.');
  } else if (selections.records_held) {
    lines.push('Your host and geography records are held until your study is published,');
    lines.push('as you asked. Let us know when it is accepted and they will go into the');
    lines.push('next MalAvi data release after that.');
  } else {
    lines.push('The records themselves appear in the next MalAvi data release.');
  }

  lines = lines.concat([
    '',
    '--',
    'MalAvi',
    'https://malavi-db.github.io/'
  ]);

  // replyTo is set explicitly rather than left to default to the sending account. The
  // default would work today, but this message tells the reader to reply, and that
  // instruction must not quietly stop being true if the sending alias ever changes.
  GmailApp.sendEmail(to,
                     'MalAvi: your lineage names are confirmed (automatic message)',
                     lines.join('\n'),
                     { name: 'MalAvi', replyTo: 'malaviadmin@gmail.com' });
  rememberConfirmation_(key);
  return { ok: true, action: 'emailed', notified: 1 };
}


/**
 * The identity of one name confirmation, for the sent-once check in confirmNames_.
 *
 * An explicit `idempotency_key` in the payload wins, so that BIOMIX can name the message
 * itself without this script changing again. Absent that, the key is derived from what
 * makes two requests the same message: the submission, the address, and the sorted names.
 * It is deliberately NOT built from `issued_at`, which is stamped fresh on every attempt
 * and would therefore never match the retry this check exists to catch. A re-approval
 * after a revision that changed a name produces a different key and is sent, which is
 * right: it is a different message.
 */
function confirmationKey_(payload) {
  var explicit = String(payload.idempotency_key || '').trim();
  if (explicit) {
    return CONFIRMATION_KEY_PREFIX + explicit;
  }
  var names = (payload.names || []).map(function (name) { return String(name); });
  names.sort();
  return CONFIRMATION_KEY_PREFIX +
    String(payload.submission_id || '') + '|' +
    String(payload.to || '').trim().toLowerCase() + '|' +
    names.join(',');
}


/**
 * Has this confirmation gone out within CONFIRMATION_MEMORY_DAYS?
 *
 * Script properties rather than CacheService: the cache is allowed to evict an entry at
 * any time, and an eviction here means a second email. Properties persist until deleted;
 * rememberConfirmation_ prunes the old ones so they do not accumulate forever.
 */
function alreadyConfirmed_(key) {
  var sentAt = PropertiesService.getScriptProperties().getProperty(key);
  if (!sentAt) {
    return false;
  }
  var ageMs = Date.now() - Number(sentAt);
  return ageMs >= 0 && ageMs < CONFIRMATION_MEMORY_DAYS * 24 * 3600 * 1000;
}


/**
 * Record that a confirmation was mailed, and forget any older than the memory window.
 *
 * Called only after GmailApp.sendEmail returned: a key recorded before the send would,
 * on a failed send, suppress the retry that is supposed to fix it.
 */
function rememberConfirmation_(key) {
  var properties = PropertiesService.getScriptProperties();
  var now = Date.now();
  var cutoff = now - CONFIRMATION_MEMORY_DAYS * 24 * 3600 * 1000;
  var all = properties.getProperties();
  for (var name in all) {
    if (all.hasOwnProperty(name) && name.indexOf(CONFIRMATION_KEY_PREFIX) === 0 &&
        Number(all[name]) < cutoff) {
      properties.deleteProperty(name);
    }
  }
  properties.setProperty(key, String(now));
}


/**
 * Tell a submitter their submission was not accepted, and point them at a person.
 *
 * WHY THIS CARRIES NO REASON
 * --------------------------
 * A curator's written reasoning quotes the submission and is usually a judgment about the
 * data. Put in an automatic message it becomes an assertion nobody can answer: the reader
 * has a question the moment they finish reading, and the sender cannot respond. So this
 * says what happened, says it plainly, and hands the conversation to a human -- which is
 * also the honest description of what a decline actually is at this stage.
 *
 * WHY IT EXISTS AT ALL
 * --------------------
 * The alternative is silence. A submitter who hears nothing does not conclude "declined";
 * they conclude "lost", and they wait, and their reserved names sit claimed while they do.
 * Being told is better than being left to infer, even when the news is unwelcome.
 *
 * TONE IS DELIBERATE
 * ------------------
 * Not accepted "in its current form", and an explicit invitation to write back. Most
 * declines at this stage are fixable -- a workbook that could not be read, records that
 * could not be reconciled with the paper -- and a submitter who reads this as a permanent
 * verdict is a contributor MalAvi has lost for no reason.
 */
/**
 * Tell the other curators that somebody recorded a verdict.
 *
 * Until this existed, a verdict was visible only to whoever next ran fetch_verdicts on
 * BIOMIX. A hold was invisible to the curator it was blocking, and an override was
 * invisible to the curator whose objection had just been set aside -- which is the one
 * person entitled to know.
 *
 * The body is composed on the maintainer's side, because that is where the ledger is: the
 * verdict id, the reason, the resulting state and the link a reader should follow are all
 * things this script has no way to know.
 *
 * BUT THE RECIPIENTS ARE NOT TAKEN FROM THE PAYLOAD. They are CURATORS, minus whoever the
 * payload says performed the act. A signed request that could name its own recipients
 * would turn this endpoint into a mailer for anyone who ever obtained the secret, and the
 * whole argument for the secret being an acceptable risk (see the header) is that its
 * holder can do exactly two harmless things. Sending mail to strangers is not one of them.
 */
function verdictNotice_(payload) {
  var subject = String(payload.subject || '').trim();
  var body = String(payload.body || '').trim();
  if (!subject || !body) {
    return { ok: false, error: 'a verdict notice needs both a subject and a body' };
  }
  var actor = String(payload.actor_email || '').trim().toLowerCase();

  var sent = 0;
  for (var i = 0; i < CURATORS.length; i++) {
    if (CURATORS[i].toLowerCase() === actor) {
      continue;                     // nobody needs mail about their own click
    }
    GmailApp.sendEmail(CURATORS[i], subject, body, { name: 'MalAvi' });
    sent += 1;
  }
  return { ok: true, action: 'emailed', notified: sent };
}


function declineNotice_(payload) {
  var to = String(payload.to || '');
  if (to.indexOf('@') === -1) {
    return { ok: false, error: 'no usable submitter address' };
  }
  var who = String(payload.submitter_name || '').trim();

  var lines = [
    '[this is an automatic email]',
    '',
    who ? 'Dear ' + who + ',' : 'Hello,',
    '',
    'This is an automatic message from MalAvi. Replies go straight to the curators, so',
    'please reply to this email with any questions.',
    '',
    'A curator has reviewed your submission, and it has not been accepted into MalAvi in',
    'its current form.',
    '',
    'The most common reason is new sequences that do not pass quality control checks.',
    'A curator can tell you the specific issues.',
    '',
    'Please write back and we will determine if we can work through this with you.'
  ];

  if (payload.reference) {
    lines.push('');
    lines.push('Submission: ' + payload.reference);
  }

  lines = lines.concat([
    '',
    'Any lineage names you proposed are no longer reserved, so they may be assigned to',
    'another submission. If you resubmit, propose them again and a curator will check',
    'whether they are still free.',
    '',
    '--',
    'MalAvi',
    'https://malavi-db.github.io/'
  ]);

  GmailApp.sendEmail(to,
                     'MalAvi: about your submission (automatic message)',
                     lines.join('\n'),
                     { name: 'MalAvi', replyTo: 'malaviadmin@gmail.com' });
  return { ok: true, action: 'emailed', notified: 1 };
}


/**
 * Email the curators that a report is ready.
 *
 * Deliberately short, and deliberately says nothing about what the report found. The
 * report itself carries the disposition line and the prefilled verdict link; an email that
 * summarized the findings would be a second place for them to be wrong, and would put
 * something about an unpublished submission into an inbox that may not be the curator's
 * only device. The subject line does not name the submitter either.
 */
function notifyCurators_(submissionId, written) {
  var verb = written.action === 'updated' ? 'updated' : 'ready';
  var subject = 'MalAvi: curator report ' + verb + ' (' + submissionId + ')';
  var lines = [
    '[this is an automatic email]',
    '',
    'A curator report is ' + (written.action === 'updated'
      ? 'available in a corrected version.'
      : 'ready for review.'),
    '',
    'Submission: ' + submissionId,
    'Report:     ' + written.url,
    '',
    // One line per paragraph, not hand-wrapped. A mail client re-wraps to its own
    // width, so a sentence split across two source lines comes out broken at whatever
    // word the author happened to stop at -- "...what was / submitted..." is what that
    // looked like in the first real report email.
    'The report opens in the browser. It carries the automated check results, what was '
      + 'submitted, and a link that records your decision.',
    '',
    'Opening it needs the Google account the reports folder was shared with. If the link '
      + 'says you need access, check which account your browser is signed into.',
    '',
    written.action === 'updated'
      ? 'This replaces the earlier version at the same link. If you already read it, the '
        + 'findings may have changed.'
      : 'Nothing is added to MalAvi without a curator decision.',
    '',
    'Confidential: a submission can contain unpublished sequences.'
  ];
  for (var i = 0; i < CURATORS.length; i++) {
    GmailApp.sendEmail(CURATORS[i], subject, lines.join('\n'), { name: 'MalAvi' });
  }
  return CURATORS.length;
}


// ---------------------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------------------

/**
 * Constant-time-ish comparison of the provided signature against the expected one.
 *
 * Apps Script has no constant-time compare. Over HTTPS, against a hex digest, a timing
 * attack is not a practical route in -- but comparing every character rather than
 * returning at the first difference costs nothing, so there is no reason to hand out the
 * information.
 */
function signatureValid_(body, provided) {
  var raw = Utilities.computeHmacSha256Signature(body, SHARED_SECRET);
  var expected = hex_(raw);
  if (provided.length !== expected.length) {
    return false;
  }
  var difference = 0;
  for (var i = 0; i < expected.length; i++) {
    difference |= (expected.charCodeAt(i) ^ provided.charCodeAt(i));
  }
  return difference === 0;
}


/** Byte array to lowercase hex. Apps Script bytes are signed, hence the & 0xFF. */
function hex_(bytes) {
  var out = '';
  for (var i = 0; i < bytes.length; i++) {
    var b = (bytes[i] & 0xFF).toString(16);
    out += (b.length === 1 ? '0' : '') + b;
  }
  return out;
}


/** Every response is JSON, including every failure. See the note on doPost. */
function reply_(object) {
  return ContentService
    .createTextOutput(JSON.stringify(object))
    .setMimeType(ContentService.MimeType.JSON);
}


// ---------------------------------------------------------------------------------------
// Run this once, by hand, before deploying
// ---------------------------------------------------------------------------------------

/**
 * Force Google to ask for the permissions the web app needs, in the editor.
 *
 * Editing appsscript.json is not always enough on its own: Google decides whether to
 * re-prompt, and if it decides not to, the missing permission stays missing and only shows
 * up as a failure inside the deployed web app -- where there is no way to consent, because
 * a web app request has no user sitting in front of it.
 *
 * This function calls UrlFetchApp for real. Running it from the editor either raises the
 * authorization dialog (which is the point) or succeeds, which proves the permission is
 * already there and the problem is elsewhere. Either way it answers the question, which
 * staring at the manifest does not.
 *
 * The request it makes is deliberately trivial and read-only: it asks Drive who this
 * script is running as.
 */
function forceAuthorization() {
  var response = UrlFetchApp.fetch(
    'https://www.googleapis.com/drive/v3/about?fields=user',
    {
      headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
      muteHttpExceptions: true
    });
  Logger.log('UrlFetchApp is authorized. Drive replied: ' +
             response.getContentText().slice(0, 200));
}


/**
 * Check the configuration without needing a request, and send yourself a sample email.
 *
 * Worth running before the first deployment, because the two most likely mistakes -- an
 * unedited folder id and an unedited secret -- both produce failures at the far end of the
 * pipeline, on BIOMIX, where the cause is invisible.
 */
function testSetup() {
  var problems = [];

  if (REPORTS_FOLDER_ID.indexOf('PASTE') === 0) {
    problems.push('REPORTS_FOLDER_ID has not been set.');
  } else {
    try {
      var folder = DriveApp.getFolderById(REPORTS_FOLDER_ID);
      Logger.log('Reports folder: ' + folder.getName());
    } catch (err) {
      problems.push('REPORTS_FOLDER_ID is not a folder this account can open: ' + err);
    }
  }

  if (SHARED_SECRET.indexOf('PASTE') === 0) {
    problems.push('SHARED_SECRET has not been set.');
  } else if (SHARED_SECRET.length < 32) {
    problems.push('SHARED_SECRET is shorter than 32 characters; generate one with ' +
                  '`openssl rand -hex 32`.');
  }

  if (!CURATORS.length) {
    problems.push('CURATORS is empty, so nobody would be told a report exists.');
  }

  if (problems.length) {
    Logger.log('NOT READY:\n  ' + problems.join('\n  '));
    return;
  }

  // Show what a curator will actually receive, using a fake submission id.
  notifyCurators_('20260101T000000_EXAMPLE', {
    id: 'example',
    url: 'https://drive.google.com/file/d/EXAMPLE/view',
    action: 'created'
  });
  Logger.log('Configuration looks right, and a sample notification has been sent to the ' +
             CURATORS.length + ' address(es) in CURATORS.');
}
