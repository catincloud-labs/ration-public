<!--
Delete any section that genuinely does not apply. An empty heading is worse
than no heading.
-->

## What changes

<!-- One paragraph. What the code now does that it did not do before. -->

## Why

<!-- The problem this resolves and how you know it is real. Link the issue. -->

## Verification

<!--
CI proves the suite passes. It cannot prove the suite tests anything, which is
this repository's central risk. So:

- Paste output, do not paraphrase it, and run it in the session that writes it.
- A claim about another repository cites the file and says whether it was READ
  or RUN. Those are different claims.
- Where this change moves what the suite decides, say so: the README describes
  what the suite decides, so it changes in the same pull request.
-->

**Dev:**

## Does the new check fire?

<!--
Required for any change touching the conformance suite, and the reason this
section exists at all.

A property added without an implementation that violates it looks identical to
a property whose violator silently passes. So name the violating implementation
and show it FAILING, not just the correct one passing.

If this change adds no check, say "no new check" and move on.
-->

## What this costs

<!--
Every real change has a price: a dependency, a slower suite, a foreclosed
option, work that now has to happen in a particular order. "None" is a valid
answer and an unusual one.
-->

## Issue references

<!--
A closing verb immediately followed by `#N` auto-closes that issue on merge,
whether or not you meant it, and backticks do not protect it - only adjacency
fires. "Part of #N" and "refs #N" are safe. Do not hunt for a gentler verb: the
words for fixing a bug are the reserved set, so a paraphrase lands on another
one. Break the adjacency instead.

When you DO mean to close something, list the exact set in `Autoclose:` so the
effect is stated rather than inferred.
-->

Part of #

Autoclose: none

## Checklist

- [ ] Conventional commit subject (`feat:`, `fix:`, `test:`, `docs:`, `chore:`)
- [ ] Branched off `main`, not committed to it directly
- [ ] The README still describes what the suite decides
- [ ] Any new contract property ships with an implementation that violates it
- [ ] Exactly one `deploy:` label, required; a `review:` clock, applied by hand and read by no guard. Applying a label takes triage access, so from a fork leave this unticked: `pr-hygiene`'s label step reads red until the maintainer labels the pull request, and that red is expected.
