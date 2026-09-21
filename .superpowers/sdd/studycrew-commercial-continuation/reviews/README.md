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
| 7th | `8331715` | *(pending at the time this index was written)* | `REVIEW-8331715-seventh.md` |

Not on disk and not recoverable: the 3rd and 4th reviews. Their Important findings and
the measurements quoted in `progress.md` survive; the reports themselves do not.

The reviewers' experiment scripts are deliberately not committed. They lived in `%TEMP%`
and each report quotes the exact command and the measured output for every claim, so the
evidence is reproducible from the report itself.

An honest note on provenance: the two files above were copied out of `%TEMP%` after the
fact, so they are what the reviewer returned to the parent agent, not a signed artefact.
Where a report states a hash (the sixth review published the sha256 of the file it
restored after mutating it), that hash is the checkable part.
