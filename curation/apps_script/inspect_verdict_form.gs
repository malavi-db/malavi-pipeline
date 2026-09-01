/**
 * READ-ONLY inspection of the LIVE verdict form.
 *
 * Written 2026-08-13, when a hand edit to the form left it unclear whether page 1 still
 * had its questions. Nothing here writes to the form, submits a response, or changes a
 * setting — every call is a getter, and the one place a FormResponse is built (see
 * showPrefillEntryIds) is built in memory and never submitted.
 *
 * WHY IT EXISTS BEYOND THAT INCIDENT
 *   The form is edited by hand, because regenerating it would mint a new URL and orphan
 *   the responses sheet. So the live form and create_verdict_form.gs can drift, and
 *   nothing in the repository can see the live one. This is how you look.
 *
 * HOW TO RUN
 *   1. https://script.google.com  ->  New project  (signed in as malaviadmin@gmail.com)
 *      Name it: MalAvi verdict form — inspect (read-only)
 *
 *      A NEW project, not one of the existing ones. Those hold createVerdictForm(), which
 *      creates a brand new form — new URL, new responses sheet, orphaned responses, dead
 *      prefill entry ids. Keeping this in its own project means that function is not in
 *      the file you are looking at and cannot be run by accident.
 *
 *   2. Paste this whole file in, replacing the stub.
 *   3. Run `inspectVerdictForm`. Approve the permission prompt the first time — it asks
 *      for access to your forms, which is what reading one needs.
 *   4. Read the output in the Execution log.
 *
 * Before running it, put the form id in below. It is the id inside
 * review.verdict_form_edit in config/project.yml — the run of letters and digits between
 * /forms/d/ and /edit. That file is private and this one is not, which is why the id is
 * not written here: this file ships to the public malavi-db/malavi-pipeline repository,
 * and the live id sat in it from 2026-08-13 until a publish attempt refused on 2026-08-14.
 * Every other script in this directory uses the same PASTE_ convention for the same reason.
 */

var VERDICT_FORM_ID = 'PASTE_THE_VERDICT_FORM_ID_HERE';


/**
 * Print every section and question, in order, with the branching each choice sets.
 *
 * This is the one to run first. It answers "what is actually on the form" without
 * anybody having to scroll through it and trust their eyes.
 */
function inspectVerdictForm() {
  var form = FormApp.openById(VERDICT_FORM_ID);
  var items = form.getItems();

  Logger.log('FORM: %s', form.getTitle());
  Logger.log('collects verified email: %s',
             form.collectsEmail() ? 'yes' : 'NO — this is the setting everything rests on');
  Logger.log('accepting responses: %s', form.isAcceptingResponses() ? 'yes' : 'no');
  Logger.log('%s items across the whole form', items.length);
  Logger.log('');

  var section = 1;
  Logger.log('--- SECTION 1 (the first page; it has no page-break item of its own) ---');

  for (var i = 0; i < items.length; i++) {
    var item = items[i];
    var type = item.getType();

    if (type === FormApp.ItemType.PAGE_BREAK) {
      section += 1;
      var page = item.asPageBreakItem();
      var goTo = page.getGoToPage();
      var nav = page.getPageNavigationType();
      Logger.log('');
      Logger.log('--- SECTION %s: "%s" ---', section, item.getTitle());
      Logger.log('    after this section: %s',
                 goTo ? ('go to "' + goTo.getTitle() + '"') : String(nav));
      continue;
    }

    var required = '';
    if (type === FormApp.ItemType.TEXT) {
      required = item.asTextItem().isRequired() ? '  [required]' : '  [optional]';
    } else if (type === FormApp.ItemType.PARAGRAPH_TEXT) {
      required = item.asParagraphTextItem().isRequired() ? '  [required]' : '  [optional]';
    } else if (type === FormApp.ItemType.MULTIPLE_CHOICE) {
      required = item.asMultipleChoiceItem().isRequired() ? '  [required]' : '  [optional]';
    }

    Logger.log('  %s. %s  (%s)%s', i, item.getTitle(), type, required);

    // For the branching question, the destination of each choice is the whole point.
    if (type === FormApp.ItemType.MULTIPLE_CHOICE) {
      var choices = item.asMultipleChoiceItem().getChoices();
      for (var c = 0; c < choices.length; c++) {
        var target = choices[c].getGotoPage();
        Logger.log('       - "%s"   ->   %s', choices[c].getValue(),
                   target ? ('section "' + target.getTitle() + '"')
                          : String(choices[c].getPageNavigationType()));
      }
    }
  }

  Logger.log('');
  Logger.log('Expected on section 1: "Submission id" (text, required), "Revision" (text,');
  Logger.log('required), "What are you recording?" (multiple choice, required, 6 choices).');
}


/**
 * Print a prefilled URL, from which the two entry ids can be read.
 *
 * WHY THIS MATTERS. Every curator report builds a link that fills in the submission id and
 * the revision, so a curator never types either — a typed id attaches a decision to
 * somebody else's work, and the first live test of the form produced "testing1,2,3".
 * Those links are built from `review.verdict_form_entries` in config/project.yml:
 *
 *     submission_id: 1106066870
 *     revision: 1976575123
 *
 * Those are the values as of 2026-08-13. They replaced 606681712 / 1938633740, which went
 * stale when the two questions were re-created during a hand edit -- exactly the case this
 * function exists to catch.
 *
 * An entry id belongs to the question, not the form. **If either question is deleted and
 * re-created, it gets a new one**, the pinned values go stale, and every prefilled link
 * silently stops filling anything in — the fields just arrive blank and the curator is
 * back to typing.
 *
 * So after any repair to page 1, run this and compare. If the numbers differ, update
 * config/project.yml. Nothing is submitted: toPrefilledUrl() only formats a URL.
 */
function showPrefillEntryIds() {
  var form = FormApp.openById(VERDICT_FORM_ID);
  var response = form.createResponse();
  var found = 0;

  var items = form.getItems(FormApp.ItemType.TEXT);
  for (var i = 0; i < items.length; i++) {
    var title = items[i].getTitle();
    if (title === 'Submission id' || title === 'Revision') {
      response = response.withItemResponse(
          items[i].asTextItem().createResponse(title === 'Revision' ? '1' : 'MARKER'));
      found += 1;
    }
  }

  if (found < 2) {
    Logger.log('Only found %s of the two questions ("Submission id", "Revision").', found);
    Logger.log('Run inspectVerdictForm first — one of them is missing or renamed.');
    return;
  }

  Logger.log('Prefilled URL (nothing was submitted):');
  Logger.log(response.toPrefilledUrl());
  Logger.log('');
  Logger.log('Read the entry.NNNN numbers out of it. The one carrying MARKER is');
  Logger.log('submission_id; the one carrying 1 is revision. Both must match');
  Logger.log('review.verdict_form_entries in config/project.yml.');
}


/**
 * Every text question's entry id at once, each tagged with its own marker.
 *
 * showPrefillEntryIds above answers only for the two ids config/project.yml pins, which
 * is the right check for those two and no help at all for anything else. The override
 * page's "Which hold are you clearing?" is the case that prompted this: a hold
 * notification can hand the next curator a link with the submission, the revision AND
 * the hold id already in it, but only if this number is known, and it is not written
 * down anywhere.
 *
 * Nothing is submitted. Each text item is filled with a marker naming its own position,
 * so the printed URL can be read straight across: title, then the entry.NNNN carrying
 * its marker.
 */
function showAllPrefillEntryIds() {
  var form = FormApp.openById(VERDICT_FORM_ID);
  var response = form.createResponse();
  var items = form.getItems(FormApp.ItemType.TEXT);
  var labels = [];

  for (var i = 0; i < items.length; i++) {
    var marker = 'MARK' + i;
    response = response.withItemResponse(items[i].asTextItem().createResponse(marker));
    labels.push(marker + '  =  ' + items[i].getTitle());
  }

  Logger.log('%s text question(s). Nothing was submitted.', items.length);
  Logger.log('');
  for (var j = 0; j < labels.length; j++) {
    Logger.log(labels[j]);
  }
  Logger.log('');
  Logger.log('Prefilled URL — match each entry.NNNN to the marker it carries:');
  Logger.log(response.toPrefilledUrl());
}
