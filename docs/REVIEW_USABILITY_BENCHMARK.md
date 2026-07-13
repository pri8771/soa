# Review usability benchmark (REV-016)

The scripted study that gates the review experience before pilot. This
document is the runnable protocol: session script, measures, the
manual-entry baseline it is compared against, pilot targets, and the
severity rubric under which **critical UX issues block release**.

> **Status — protocol ready, execution pending humans.** Everything a
> study needs that can be prepared in the repository exists: the
> script below, the seeded fixture documents, and the results
> templates. The study itself requires 5–8 human participants working
> real sessions; it cannot be simulated, and no results are recorded
> here until it runs (planned inside the PIL epic, before pilot
> go-live). Recording fabricated results would defeat the gate.

## 1. What is being measured

| Measure | Definition | Pilot target |
| --- | --- | --- |
| Critical path completion | Participant opens a queued task, resolves every reason, and approves/rejects without facilitator help | ≥ 90% of sessions complete unaided |
| Error rate | Corrected fields whose final value disagrees with the source document (scored against the fixture answer key) | ≤ 2% of corrected fields |
| Evidence comprehension | Participant can point to WHERE a flagged value came from (click the field → find the highlight) within 10 seconds | ≥ 85% of probes |
| Timing | Median task wall-clock vs. the manual-entry baseline for the same document | ≤ 50% of baseline time |
| Perceived confidence | Post-task question: "How confident are you the order is correct?" (1–7) | Median ≥ 6 |

## 2. Baseline: manual entry comparison

Each participant keys ONE fixture document into a spreadsheet template
(the columns of the canonical sales order) before touching the studio.
That session is timed and scored with the same answer key. The baseline
answers two questions: how long does unaided entry take (the timing
denominator), and what error rate does the studio need to beat.

Baseline sessions use a different fixture than studio sessions, and
document/participant pairings rotate (Latin square) so neither order
nor document difficulty biases the comparison.

## 3. Fixtures

Three seeded documents, all derived from the deterministic mock
provider so every participant sees identical data:

1. **Clean-but-flagged** — one low-confidence critical field
   (`po_number`), everything else passing. Exercises: evidence lookup,
   single correction, approve.
2. **Broken totals** — header total disagrees with the lines, one line
   missing its total. Exercises: grid editing, revalidation feedback,
   approve after fix.
3. **Wrong document** — a quote, not a purchase order. Exercises:
   recognizing non-actionable input, reject with a reason (or escalate
   where the participant lacks reject permission).

The answer key for each fixture lives with the study materials, not in
the app.

## 4. Session script (~45 minutes per participant)

1. **Setup (5 min).** Dev environment, seeded organization, participant
   signed in as a `reviewer`; think-aloud instructions; recording
   consent.
2. **Baseline (10 min).** Manual entry of the baseline fixture into the
   spreadsheet template. Timed.
3. **Studio tasks (20 min).** The three fixtures above, in the
   participant's rotated order. For each: claim from the queue, resolve,
   complete. Facilitator records: completion, hesitations > 10s, wrong
   turns, all utterances mapping to confusion.
4. **Evidence probes (5 min).** On the completed document: "Show me
   where the total came from." "Why was this field flagged?" Timed.
5. **Debrief (5 min).** Confidence rating, hardest moment, anything the
   participant expected but could not find.

## 5. Issue severity rubric

| Severity | Definition | Consequence |
| --- | --- | --- |
| **Critical** | Blocks the critical path, causes silent data loss, or produces a wrong approved order without warning | **Blocks release.** Remediation task filed at P0; the study section reruns after the fix |
| Major | Forces a workaround or facilitator help; > 2 participants hit it | Remediation task filed; fix before pilot expansion |
| Minor | Slows or annoys but self-recovers | Backlog task, prioritized by frequency |

## 6. Results and remediation templates

Record per participant: session id, baseline time, per-fixture studio
time, completion (unaided / aided / failed), corrected-field errors,
evidence-probe times, confidence rating, and the issue list with
severity. Aggregate against §1 targets; any missed target or any
critical issue keeps the release gate closed.

Every issue becomes a repository task titled
`REV-016-F<n> — <symptom>` carrying: the observed behavior, the
participant count, severity, and the acceptance check that proves the
fix. Critical issues additionally link the release gate they block.

## 7. What already reduces the risk this study measures

The automated suites cover the mechanical halves of these measures:
keyboard reachability and screen-reader announcements (DSN-009, REV-013
tests), two-step approval safety, conflict resolution without data
loss (REV-014 tests), evidence overlay honesty (REV-005 tests), and
responsive stacking with nothing hidden (REV-015 tests). What only the
study can measure is whether real reviewers UNDERSTAND the flags,
trust the evidence, and finish faster than typing.
