/**
 * Add the "Message to the submitter" question to the LIVE verdict form, once.
 *
 * WHY
 * ---
 * The verdict form's "Why?" is one field doing two jobs: the curators' reasoning and the
 * words a submitter should read. The curators asked on 2026-10-15/16 (Arif, agreed by
 * Luz and Mélanie) for two separate fields: an internal note, and a note to the submitter
 * that goes out automatically on a flag. "Why?" stays the internal note; this adds the
 * second field right after it. notify_on_verdict.gs reads it by title and sends it when
 * the verdict is a flag; curation/src/malavi_curation/verdicts.py reads the same column
 * into the ledger.
 *
 * WHY NOT REBUILD THE FORM
 * ------------------------
 * create_verdict_form.gs now includes the question for any future rebuild, but rebuilding
 * makes a new form with a new responses sheet and a new URL, which means a new share with
 * the fetcher's service account, new prefill entry ids in config/project.yml and every
 * report link changing. Adding one question to the live form changes nothing else: the
 * responses sheet simply gains a column, which the Python side reads by header.
 *
 * RUNNING IT
 * ----------
 * As malaviadmin, in the "MalAvi curation" project, with VERDICT_FORM_ID set: run
 * addSubmitterMessageQuestion. It refuses to add a second copy, so running it twice is
 * safe. Then open the form once and check the question sits after "Why?" on the "Your
 * verdict" page. Nothing is sent.
 */

var SETTINGS_NEEDED_ADD_QUESTION = ['VERDICT_FORM_ID'];

var SUBMITTER_MESSAGE_TITLE = 'Message to the submitter';
var SUBMITTER_MESSAGE_HELP =
    'Optional. If you FLAG, whatever you write here is emailed to the submitter as ' +
    'written, from MalAvi, with you and the assigned curator in copy, the moment you ' +
    'submit this form. Write it to them ("your sequence CURCAN01 differs..."), not about ' +
    'them. Leave it empty to write to them yourself instead. Nothing is sent on Accept or ' +
    'Reject. "Why?" above stays between the curators.';
var AFTER_TITLE = 'Why?';


function addSubmitterMessageQuestion() {
  var form = FormApp.openById(requireSetting_('VERDICT_FORM_ID'));
  var items = form.getItems();
  var afterIndex = -1;
  for (var i = 0; i < items.length; i++) {
    var title = items[i].getTitle();
    if (title === SUBMITTER_MESSAGE_TITLE) {
      Logger.log('The form already has "' + SUBMITTER_MESSAGE_TITLE + '" (item ' + i +
                 '). Nothing added.');
      return;
    }
    if (title === AFTER_TITLE && afterIndex === -1) afterIndex = i;
  }
  if (afterIndex === -1) {
    throw new Error('No question titled "' + AFTER_TITLE + '" on the form; the form is not ' +
                    'the one create_verdict_form.gs builds. Nothing added.');
  }
  var item = form.addParagraphTextItem()
      .setTitle(SUBMITTER_MESSAGE_TITLE)
      .setHelpText(SUBMITTER_MESSAGE_HELP)
      .setRequired(false);
  form.moveItem(item.getIndex(), afterIndex + 1);
  Logger.log('Added "' + SUBMITTER_MESSAGE_TITLE + '" after "' + AFTER_TITLE + '" (now item ' +
             (afterIndex + 1) + '). Open the form once to check the placement. The ' +
             'responses sheet gains the column on the first response.');
}
