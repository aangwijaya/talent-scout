# Backlog

Ideas the product owner wants after the voice interview. Each becomes an issue (and a `contract-change` issue first when it changes the API) when its turn comes.

## B1: Interviews in the job's language

**Seen on 2026-11-26.** An Indonesian HR Generalist job was created in production without errors. The seed job and every prompt are English, so an Indonesian job description is the first non-English input.

**To check first:** apply to that job and note the language of the opening, the questions, the follow-ups, the closing and the score rationales.

**Likely shape:**
- The planner, interviewer, scorer and closing answer in the language of the job description. The language may be detected from the job text or set per job.
- The applicant pages (apply, interview intro, consent) stay in the app's language unless a per-job language is added. That choice is the product owner's.
- Voice (S-V3, S-V5) transcribes and assesses spoken *English* only. An Indonesian voice interview is out of scope until B1 is decided.

## B2: Rubric suggestion follow-ups

**Noted on 2026-11-26.** "Suggest rubric" already exists (`POST /jobs/rubric-suggestion`). The product owner will try it on the HR job and compare it with the hand-written rubric.

**Next:** from that test, decide what to improve: for example the suggestion's language for an Indonesian job (see B1), or proposing the job's level as well.
