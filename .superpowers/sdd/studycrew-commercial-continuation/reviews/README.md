# Independent review reports

Raw reports as returned by the independent reviewer for each round of Task 2, Round 2.

They are kept in the repository because a review that exists only in an agent's reply is
a review that will be lost. That is not hypothetical: the **fourth** review's ten Minor
findings disappeared exactly that way — only its Important findings were transcribed
into `progress.md`, and the other seven have no durable record, which `progress.md`
records as a standing warning.

Note `.superpowers/sdd/` is gitignored, so these files need `git add -f`.

| round | revision reviewed | verdict | file |
|---|---|---|---|
| 5th | `73d7e33` | FAIL — 0 Critical / 4 Important / 7 Minor | `REVIEW-73d7e33-fifth.md` |
| 6th | `448c4c3` | FAIL — 0 Critical / 2 Important / 2 Minor | `REVIEW-448c4c3-sixth.md` |
| 7th | `8331715` | FAIL — 0 Critical / 1 Important / 1 Minor | `REVIEW-8331715-seventh.md` |
| 8th | `cc94652` | PASS — 0 Critical / 0 Important / 3 Minor | `REVIEW-cc94652-eighth.md` |

Two things to read these with:

- The **eighth** review's PASS is scoped: it was asked to check three specific things and
  explicitly told not to run the full suite, and it finished in one pass. The **seventh** review,
  covering the neighbouring revision, found an Important that the eighth's three checks did not
  reach (the cancellation guards caught only our own exception types while the storage layer
  re-raises raw `sqlite3.*`). A PASS from a narrow review is not a PASS for the stage; only the
  verdict in `TASK2-ROUND2-ACCEPTANCE.md` is.
- Two of the eighth review's three Minor findings do **not** hold against its own frozen revision
  (the comment does list all five providers; the cited ledger line contains no count). That is
  recorded in the report's parent notes rather than silently dropped, and it is a reminder that a
  reviewer's findings need checking in both directions.

Not on disk and not recoverable: the 3rd and 4th reviews. Their Important findings and
the measurements quoted in `progress.md` survive; the reports themselves do not.

The reviewers' experiment scripts are deliberately not committed. They lived in `%TEMP%`
and each report quotes the exact command and the measured output for every claim, so the
evidence is reproducible from the report itself.

An honest note on provenance: the two files above were copied out of `%TEMP%` after the
fact, so they are what the reviewer returned to the parent agent, not a signed artefact.
Where a report states a hash (the sixth review published the sha256 of the file it
restored after mutating it), that hash is the checkable part.
