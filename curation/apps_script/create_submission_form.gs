/**
 * Rebuild the MalAvi data submission form under the operational account.
 *
 * WHY THIS EXISTS
 * ---------------
 * The original submission form is owned by vaellis@udel.edu — a university account that
 * disappears when the job does, and whose institution owns the files inside it. Moving it
 * is the single largest remaining piece of the problem malaviadmin@gmail.com was created
 * to solve. Rebuilding rather than transferring is the right call here only because nobody
 * outside the project has been given the form or site link yet, so no external reference
 * breaks when the URL changes. That will not be true again — after this, transfer.
 *
 * READ THIS FIRST: THE SCRIPT CANNOT FINISH THE JOB
 * -------------------------------------------------
 * **Apps Script cannot create file-upload questions.** FormApp has no addFileUploadItem,
 * and neither does the Forms REST API; it is a long-standing gap, not an oversight in this
 * script. The submission form has TWO of them, and they are the whole point of the form.
 *
 * So this script builds everything else and then tells you, precisely, what to add by hand.
 * It is deliberately written to leave a form that is obviously unfinished rather than one
 * that looks complete and silently drops every uploaded file: the form is left NOT
 * accepting responses, and the two missing questions are logged with their exact titles.
 *
 * Do not skip `finishByHand()` at the bottom.
 *
 * QUESTION WORDING IS LOAD-BEARING
 * --------------------------------
 * curation/build_site_feeds.py and form_metadata.py find answers by matching WORDS in the
 * question text — "institution", "leaderboard", "template" — because that survives light
 * rewording better than exact matches. The wording below is reproduced verbatim from the
 * live form so that the three submissions already fetched, which carry the old question
 * text in their metadata, keep parsing. Change a title here and check those matchers.
 *
 * ONE QUESTION IS NEW
 * -------------------
 * `_sending()` in build_site_feeds.py has always looked for a question containing the word
 * "sending", and no question in the live form contains it — so that field has been empty
 * since it was written. The question it expects is added below, with the answer values it
 * parses. This is the only intentional difference from the live form.
 *
 * HOW TO RUN IT
 * -------------
 *   1. Sign in as malaviadmin@gmail.com — ONLY that account. Whatever runs this owns it.
 *   2. script.google.com > New project, paste this in, name it "MalAvi submission form".
 *   3. Run `createSubmissionForm`, authorize.
 *   4. Read View > Logs, then do every step in finishByHand().
 */

function createSubmissionForm() {
  var form = FormApp.create('MalAvi Data Submission');

  form.setDescription(
      'Submitting data to MalAvi for inclusion in the latest updates. You should be able ' +
      'to track the status of your submission on the website.');

  // Verified, so a submission can be tied to a real mailbox — which is what lets a curator
  // come back with a question, and what keys the contributor board across submissions.
  try {
    form.setEmailCollectionType(FormApp.EmailCollectionType.VERIFIED);
    Logger.log('Email collection: VERIFIED.');
  } catch (err) {
    form.setCollectEmail(true);
    Logger.log('WARNING: fell back to setCollectEmail(true) — CHECK BY HAND that');
    Logger.log('WARNING: Settings > Responses > Collect email addresses says "Verified".');
  }

  // Left OFF until the file-upload questions are added by hand. A form that accepts
  // responses while missing the questions that carry the actual data would collect
  // submissions with no files attached, and the submitter would have no idea.
  form.setAcceptingResponses(false);

  form.addTextItem()
      .setTitle('What is your first and last name?')
      .setRequired(true);

  form.addTextItem()
      .setTitle('What institution are you associated with?')
      .setRequired(true);

  form.addTextItem()
      .setTitle('What country are you located in?')
      .setRequired(true);

  // Consent for the contributor board. The default must be exclusion, which is why this is
  // asked rather than assumed — build_site_feeds only lists people who answered Yes.
  form.addMultipleChoiceItem()
      .setTitle('Do you want your name and information added to the website leaderboard?')
      .setChoiceValues(['Yes', 'No'])
      .setRequired(true);

  form.addMultipleChoiceItem()
      .setTitle('Are you submitting published or unpublished data?')
      .setHelpText('Unpublished data are welcome. They are held privately and not ' +
                   'shown on the public queue.')
      .setChoiceValues(['Published', 'Unpublished'])
      .setRequired(true);

  // NEW. Asked of everyone, though it only means anything to a submitter who answered
  // "Unpublished" above. Google Forms can branch on the previous answer, but branching
  // puts the question on its own page and every other question here is flat -- a third
  // "not applicable" choice costs a published submitter one click and keeps one shape.
  //
  // This does not introduce a convention. MalAvi has held unpublished records for years,
  // cited as "<Authors> unpubl" with no row in the reference table: 838 such rows across
  // 62 studies in the seed release. What is new is ASKING, instead of a curator deciding
  // on the submitter's behalf whether their unpublished records should go public.
  //
  // The answer is also what finally sets Entry.embargoed. Until this question existed
  // nothing wrote that field, so a pre-publication submitter who went quiet was timed out
  // at awaiting_submitter_timeout_days and their reserved lineage names were handed to
  // somebody else -- the precise harm the field was added to prevent.
  //
  // build_site_feeds._records_embargo() parses the LEADING WORD of the answer, so these
  // three values must keep starting with "Add", "Hold" and "Not".
  form.addMultipleChoiceItem()
      .setTitle('If your data are unpublished, may we add the records to MalAvi now?')
      .setHelpText('Unpublished records go in credited as, for example, ' +
                   '"Ellis et al unpubl", and are renamed to the real citation once ' +
                   'you tell us the study is published. Your lineage names are ' +
                   'confirmed by a curator either way. This question decides when the ' +
                   'host and geography records become public.')
      .setChoiceValues(['Add them now, credited as unpublished',
                        'Hold them until I confirm the study is accepted',
                        'Not applicable - my data are already published'])
      .setRequired(true);

  form.addMultipleChoiceItem()
      .setTitle('Are you submitting a filled out data template file, a PDF + ' +
                'supplementary materials, or both?')
      .setChoiceValues(['Data template file',
                        'PDF + supplementary materials (if applicable)',
                        'Both'])
      .setRequired(true);

  // NEW. build_site_feeds._sending() parses the leading word of the answer, so these three
  // values must keep starting with "New lineage", "Records" and "Both".
  form.addMultipleChoiceItem()
      .setTitle('What are you sending us?')
      .setHelpText('What the submission contains. This is what the public queue shows ' +
                   'before a curator has screened it.')
      .setChoiceValues(['New lineage names and sequences',
                        'Records of known lineages in hosts or vectors',
                        'Both'])
      .setRequired(true);

  form.addParagraphTextItem()
      .setTitle('Please provide any relevant notes or communication here (if applicable).')
      .setRequired(false);

  var sheet = SpreadsheetApp.create('MalAvi Data Submission (Responses)');
  form.setDestination(FormApp.DestinationType.SPREADSHEET, sheet.getId());

  Logger.log('');
  Logger.log('Form (edit)     : ' + form.getEditUrl());
  Logger.log('Form (public)   : ' + form.getPublishedUrl());
  Logger.log('Responses sheet : ' + sheet.getId());
  Logger.log('');
  Logger.log('NOT YET ACCEPTING RESPONSES — two file-upload questions must be added by');
  Logger.log('hand. See finishByHand() in this file. Paste this log to Claude Code.');

  return form;
}


/**
 * FINISH THE FORM BY HAND. Apps Script cannot do these steps; nothing is wrong with it.
 *
 * 1. Open the form's edit URL. Add a file-upload question, placed AFTER the notes question:
 *
 *      Title:    Submission Template File (fill out the template on the website and
 *                upload here)
 *      Type:     File upload
 *      Required: no
 *      Allow:    specific file types — Spreadsheet, Document, PDF
 *      Max files: 1     Max size: 100 MB
 *
 * 2. Add the second file-upload question:
 *
 *      Title:    Submission PDF and Supplementary Materials (associated with the
 *                submission template file...but if you want you can just upload the PDF,
 *                even old PDFs, and we will try to capture their data)
 *      Type:     File upload
 *      Required: no
 *      Allow:    any file type
 *      Max files: 10    Max size: 100 MB
 *
 *    The titles must match EXACTLY. curation/fetch_submissions.py reads the uploaded file
 *    ids out of the response row by these column names, and the three submissions already
 *    fetched carry this wording in their metadata.
 *
 * 3. Note what adding those questions did: Google created two folders in malaviadmin's
 *    Drive to receive uploads. Find their ids (open each folder; the id is the last path
 *    segment of the URL) — they replace `submissions.template_folder` and
 *    `submissions.materials_folder` in config/project.yml.
 *
 * 4. Adding file upload questions forces respondents to sign in to Google. That is already
 *    true of the current form, so it is not a new barrier — but it IS a barrier, and it is
 *    the reason a submitter without a Google account cannot use this form at all.
 *
 * 5. Settings > Responses: confirm "Collect email addresses" is **Verified**.
 *
 * 6. Only when all of the above is done: Responses tab > turn ON "Accepting responses".
 *
 * 7. Submit one test response with a junk file, confirm it lands in the sheet AND that the
 *    file appears in the right Drive folder, then delete the response from BOTH the sheet
 *    and the form's Responses tab.
 *
 * 8. Decide the sharing question before any real submission arrives: the responses sheet
 *    and both upload folders currently need to be readable by curation/fetch_submissions.py.
 *    The old form's were shared with "anyone with the link", which for a sheet holding
 *    submitter addresses and unpublished sequences is worth not repeating.
 */
function finishByHand() {
  throw new Error('This is documentation, not a function. Read the comment above it.');
}
