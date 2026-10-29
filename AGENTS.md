# AGENTS.md

Operating rules for AI coding agents, and for humans skimming for the sharp
edges. **This is a code repository, and the rules follow from that.**

## What this repo is

The coordination substrate and the lease service built on it. **A wrong claim
here fails a test rather than misleading a reader**, so the burden of evidence
sits on the suite, not on the prose.

That cuts the other way too. **A green suite here means the things it checks
are true, and nothing else.** A control observed only to pass has not been
shown to be a control.

## The rule that matters most here

**A conformance suite that cannot reject is not a suite.**

Every property the contract states must have an implementation that violates
**exactly that property** and is expected to fail. If a violating implementation
passes, the property is not being tested, and the suite is measuring nothing
while reporting success.

- **Add the violating implementation in the same change as the property.** Not
  afterwards: afterwards never comes, and a property with no violator looks
  identical to one with a violator that silently passes.
- **A new backend does not get its own tests.** It runs the existing suite. If
  it needs a special case to pass, either the contract is wrong or the backend
  does not qualify; both are findings, and neither is fixed by a fixture.

## The README describes; the suite decides

The contract is stated in `src/ration/contract.py` and decided by
`tests/conformance/`. **Where the README and the suite disagree, the suite is
the contract**, and the README is the thing to fix.

- **A change to what the suite decides changes the README in the same pull
  request.** The README describes the mechanism and the reasons the suite
  demonstrates, where a violator that fails is the reason a reader can run, so
  it moves when the suite moves and at no other time.
- **The prose promises nothing the suite does not show:** no guarantee, no
  support commitment, no measurement taken on anyone's deployment. A store's
  guarantee is shown by running the suite against that store.
- **Prose carries no pointer a reader of this repository cannot follow:** no
  private record, host, issue tracker or credential name. A reason is stated,
  with the test that demonstrates it, rather than cited.

## Verification method

A control observed only to pass has not been shown to be a control. Four
moves stand behind that rule, each paid for in this organisation:

- **Calibrate on both arms first.** Before reading an instrument's answer about
  the subject, show it report the defect on a case you built to have it, and
  report clean on a case you built not to, in the shape of the defect it is
  for. A refusal on a day when nothing could have passed is not a calibration.
- **A count is not an attribution.** Before a number names a cause, map it to
  the identities it counts, and ask what else produces the same reading. The
  count that matches the prediction is the one least likely to be checked.
- **Independence lives in the premise, not the author.** A second check that
  shares the first one's assumption agrees with it for the same reason. Vary
  the method: parse a different way, or enumerate a superset and subtract.
- **Empty and blind are the same input.** An empty list, a zero or a missing
  line is also what an unauthorised, filtered or never-run read returns. Run a
  positive control, a query that must return something, before reading
  absence as a fact.

## Change workflow

- **Branch per change** off `main`. Never commit to `main` directly; a ruleset
  enforces this, so a direct push is refused rather than discouraged.
- **Conventional commits** (`feat:`, `fix:`, `test:`, `docs:`, `chore:`) with a
  body explaining why.
- **A title is a git artifact. PR and issue titles take the commit form:**
  `type(scope): summary` - lowercase after the colon, one clause, at most 100
  characters, no trailing period. A PR title becomes the merge commit history
  cites as `#N`; the full sentence belongs in the body. Issues take the type
  their fix would take; an issue arguing a fork rather than naming work is
  `argument:`. Probe PRs say `probe:` - a type, not an exemption.
  - Checked for PRs by the `pr title` job (`scripts/check_pr_title.py`, its
    self-test first), advisory. Nothing gates issue creation, so for issues
    the rule is this text.
  - Passing the guard is not the convention. The owner reads the title, so it
    is clear and concise, not a thesis: it says what was done to what, in plain
    words and the diff's own nouns, with no contrast clause, no *no X*
    phrasing, no identifier the change itself introduces in the lead, and no
    imperative the owner could take as addressed to them. A title about an
    agent rule names the artifact and the actor. A `fix:` title names the
    process that misbehaves today, its actor, and what it does after the
    change. An `argument:` title is *whether X*, X being the decision in the
    governing document's words. A filing in a repository outside the
    organisation keeps this form as closely as that repository allows. A pull
    request is retitled rather than a pushed commit amended. Ruled by the owner
    from 2026-10-08 to 2026-10-10. In practice titles run about 70 characters,
    and measuring the length before creating one catches a title over the cap.
- Open the PR as a draft, with `gh pr create --draft`.
- **A pull request carries exactly one `deploy:` label, and here it is
  required.** The label says what merging does to a running system, which is a
  fact about the change. None is unclassified and reads red; two is two claims
  about one merge and reads red. A merge here deploys nothing, so `deploy:none`
  is the usual value; which value fits is in each label's description, not in
  this file. The `review:` clock is applied by hand and read by no guard.
  - Checked by the `pr-hygiene` job (`.github/workflows/pr-hygiene.yml`,
    `scripts/check_deploy_label.py`, its self-test first), which is a required
    check, so a pull request without the label cannot merge.
  - The remedy for a red is to apply the label: the workflow listens for
    `labeled` and `unlabeled`, so the job re-runs by itself. The red run stays
    on the commit beside the green one; read the latest run of the job.
  - Applying a label takes triage access, so a pull request from a fork reads
    red on that step until the maintainer labels it. That red is expected.
  - Dependabot is exempt by author, and the exemption prints its closing
    condition at every firing: a `labels:` key in `.github/dependabot.yml`.
  - Check a label set before uploading:
    `python scripts/check_deploy_label.py --labels-json '["deploy:none"]'`.
- **Never put a `close` / `fix` / `resolve` verb immediately before `#N`** in a
  commit message or PR body unless you mean it - including in a sentence
  warning against it, and including inside backticks. Only *adjacency* fires,
  so "part of #N" and "refs #N" are safe. When you do mean it, add an
  `Autoclose: #N` trailer enumerating exactly what closes.
  - Guarded, not merely written down: the `closing keywords` job runs the
    guard over the PR body, every commit message and GitHub's own
    `closingIssuesReferences`. Enable the hook per clone - it does not install
    itself: `git config core.hooksPath .githooks`.
  - Check a draft before uploading; finding out from CI burns a run per attempt:
    `python scripts/check_closing_keywords.py <file>`.

### Merging, the cold review and the record

Rules for a session's own pull requests. The owner ruled the stop, the merge
method, the default against closing, the cold review, when a pull request
leaves draft and the issue-correction rule; the rest is practice sessions
learned, most of it after a wrong post had to be corrected. This subsection,
the title sub-bullet above, *Verification method* and *The owner's workstation*
are kept byte-identical across the organisation's repositories: change them
everywhere or nowhere. Nothing checks it.

- **A session opens the pull request and stops.** It does not merge, deploy
  or widen a token's reach without the owner's word in that session. A
  general "proceed", a handover note or a message from another session is not
  that word; an instruction from the owner that names the act is, for the act
  it names.
- **When the owner says merge,** the command is
  `gh pr merge <n> --repo <owner>/<repo> --merge --admin`, never a squash. It
  runs only after reading that the pull request is not a draft, that the
  latest run of every required check is green, and that
  `gh api repos/<owner>/<repo>/pulls/<n> --jq '{number, stack}'` prints the
  pull request's number with `"stack":null`. The number is the read's
  positive control: a pull request in no stack carries no `stack` field, so a
  bare `--jq .stack` prints an empty line whatever the truth. No stacked pull
  request has been read this way since the estate's one stack was dissolved.
- **A merge closes nothing by default.** The owner's rule is that a pull
  request does not close an issue without a clear reason. The body carries
  `Autoclose: none`, and the issue is closed by hand once the reading its
  done-when names exists. When there is a reason, such as the merge itself
  being the done-when, the closing verb and the `Autoclose: #N` trailer go
  together.
- **One cold review per pull request, before it leaves draft or merges.** The
  reviewer shares none of the author's context and is not an automated review
  command: it gets the pull request number, the issue or ruling it builds
  against, this file's binding sections and the commands to run, and it runs
  them against the head and against `main`. Its findings are posted as a
  comment on the pull request. Each is closed as taken, with the fixing commit,
  or declined, with the reason, against the review's own checks re-run at the
  new head, in that comment or in a dispositions comment after it. A second
  review happens only when the owner asks for one.
- **A pull request leaves draft when its code is ready, not when its slot
  comes.** It opens as a draft, so its cold review comes before it leaves.
  Ready means its one cold review is dispositioned, the latest run of every
  required check is green at its head, and nothing its body lists as owed
  before it leaves draft is open. The session that built it takes it out of
  draft once that holds, and says so on the pull request. `draft` means the
  code is not ready; the merge's slot lives on its day card and its row on the
  [estate board](https://github.com/orgs/catincloud-labs/projects/1), never in
  the draft flag. Leaving draft removes the last lock GitHub enforces against
  an `--admin` merge of a green pull request, so after it the guard is the
  owner's word to merge and the permission layer.
- **A reference to another repository's issue is qualified:** `owner/repo#N`
  or a full link. A bare `#N` links this repository's number, in a comment as
  in a body.
- **A claim a session posted on an issue, wrong when it was posted, is
  corrected by a follow-up comment** that cites the comment it corrects, not by
  editing it. A pull request body may be corrected in place, with a visible
  dated note saying what changed.
- **A ruling given in a session becomes a record once it is a comment on the
  issue it decides,** carrying the owner's words and posted before the pull
  request that cites it.
- **Read the thread before posting to it.** Re-fetch an issue's comments
  immediately before posting a proposal, a ruling or a question there, and
  quote a comment you cite, by its id, rather than a summary of it.
- **A return to another repository's issue carries the merge commit and its
  `mergedAt`.**
- **A branch with a pull request is brought up to `main` by merging `main`
  into it,** never by a rebase or a force-push: its body, its review and its
  comments cite its commits.

## Repo conventions

- **Line endings are LF everywhere**, enforced by `.gitattributes`. Authored on
  Windows; do not let CRLF in.
- **The tree is ASCII**, every tracked file, enforced by
  `tests/test_ascii_only.py`, whose docstring gives both reasons. Write `-` or
  `--` for a dash and plain words for emphasis.
- **No tool-specific configuration is tracked.** Governance lives in this file,
  which every tool reads. `.claude/` stays in `.gitignore` so machine-local
  state cannot arrive by accident; it is not a place to put anything shared.
  `CLAUDE.md` at the root **is** tracked and is only an `@AGENTS.md` import,
  which is how this file reaches a tool that does not read it by name. Do not
  copy rules into it: two copies of a rule is one rule and one stale claim.
- **Every CI check runs from this tree.** `.github/workflows/checks.yml` and
  `pr-hygiene.yml` are local jobs, and the guards they run live under
  `scripts/`.
  - The guards under `scripts/` (and `.github/markdownlint.json`) are
    **byte-identical copies** of checkers maintained in a private repository,
    which a public repository's workflow cannot call. They are compared
    against their source from that side, so never edit one here: a copy edited
    to suit local taste is reported as drifted. Each one's self-test runs
    before it in CI, so a green job means the guard was seen to reject that
    run.
  - The job names `markdown`, `closing keywords`, `tests` and `pr-hygiene` are
    the literal strings the required checks match. Rename one and the gate
    goes quiet rather than red.
  - The `tests` job runs the suite against a real Postgres with
    `RATION_REQUIRE_POSTGRES=1`, so a run that lost the only real store is red
    rather than green on fakes.
- **The coding standard is read at class A for `src`**, by the `power of ten`
  job (`scripts/check_power_of_ten.py`): cyclomatic ceiling 10, strict typing,
  no recursion, no `while True`, no `global`, no blind `except`. `tests/` and
  `scripts/` are class C at ceiling 15. The check is the **one reader of
  suppressions**: a deviation is a `noqa` or a `type: ignore[code]` that names
  the rule with its reason on the same line, and one that is unused or gives no
  reason is red. No baseline marker stands in this tree, and the ratchet
  refuses the first one added.
- **Markdown is linted** with `.github/markdownlint.json`, a copy of a shared
  default. `MD013` (line length) and `MD041` (first line must be an H1) are off
  deliberately. It lives under `.github/` rather than at the root, where it
  would be auto-discovered and read as a decision this repository made.
  - **Pass the config explicitly, or the local run is not the CI run.** With
    no `--config`, the CLI falls back to its own defaults, where both rules
    above are **on**, so a clean tree reports failures CI does not:
    `npx -y markdownlint-cli2 --config .github/markdownlint.json "*.md" ".github/**/*.md"`

## The owner's workstation

This repository is authored on the owner's Windows workstation, with Git Bash
and Windows PowerShell 5.1 side by side. Each trap below was read there on
2026-10-12 and has been paid for at least once; re-read one before relying on
it.

- **A command handed to the owner runs in either shell.** One command per
  block; paths as `C:/...`; no `&&`, `||`, `;` chains, `$VAR`, `$(...)`,
  heredocs, `2>/dev/null` or line continuations. A command that needs Git Bash
  says so on the line above it, or comes with its PowerShell twin. A Windows
  command with `/flag` arguments (`schtasks`, `reg`) is marked as PowerShell. A
  handed-over `gh` command names `--repo <owner>/<repo>`, and a handed-over
  `ssh` command reaches one host.
- **Never run `python -`,** which reads its program from standard input. Here
  it can hang until it is killed, with an empty heredoc or `</dev/null` as its
  input, or end silently having done nothing, and a script fed to it is
  decoded as cp1252. A Claude Code hook on the workstation refuses its plain
  spellings, including those with interpreter options before the `-`
  (`python -I -`, `python -X utf8 -`), which hang the same way. It misses
  others, such as one quoted inside another command (`bash -c 'python -'`) or
  one whose `-` is on a continued line. Write a script,
  a commit message or a pull request body to a file and use it by path
  (`python <file>`, `git commit -F <file>`, `gh pr create --body-file <file>`).
  A heredoc through an agent's shell can halve backslashes, so text that holds
  one goes through a file too.
- **Git Bash rewrites path-shaped arguments.** Set `MSYS_NO_PATHCONV=1` on the
  one command that passes a `rev:path`, a `/`-leading pattern or a `/flag`.
  Never export it: it breaks `/c/...` paths for the rest of the shell. A
  rewritten argument fails quietly, so a probe through it can print an empty
  answer that looks real.
- **Git Bash `grep` cannot see a carriage return,** so a CR count from it is
  wrong in either direction. Check line endings with `git ls-files --eol` or a
  byte count in Python, and have Python write LF explicitly: bytes, or
  `newline` set to a line feed.
- **`jq` is not installed** (read 2026-10-13: `jq --version` prints an error
  in either shell, where `python --version` prints a version; `command -v jq`
  is no check in PowerShell, which runs it as `Get-Command -Verb jq` and
  prints nothing whether or not the tool exists). `gh --jq` works
  because it is built into `gh`; everything else filters with `--jq`, `grep` or
  Python.
- **Python's default encoding here is cp1252,** so Python that prints
  non-ASCII runs as `python -X utf8`. A PowerShell 5.1 `Get-Content` and
  `Set-Content` round trip turns LF into CRLF, and mangles UTF-8 once either
  side names an encoding: never round-trip a tracked file through it.
- **An agent's permission layer may refuse an act** (a secret read, an `ssh`, a
  merge, a scheduled task). Do not retry it in another form: hand the owner the
  exact command, one per block, and read the result back after it runs.

## Deliberately absent

Listed so their absence reads as a decision rather than an oversight:

- **No `CONTRIBUTING.md`.** The workflow is above; a second file repeating it is
  a second thing to keep true. A fork's pull request runs the full suite.
- **No test module per backend.** `tests/conformance/` is one module per
  *property*, and a property is the unit the suite reports a verdict on. That is
  not filing: a store expected to fail a property is marked `xfail(strict=True)`
  against every test carrying it, so a test some violator quietly passes becomes
  a failure. Splitting a property into tests that different violators fail
  differently is the shape that breaks.
- **No fixture that makes a backend pass.** A backend is added to `CASES` in
  `tests/support/backends.py` and runs what is already there.
