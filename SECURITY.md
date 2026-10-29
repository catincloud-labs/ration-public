# Security policy

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting**, from the *Security* tab of
this repository ("Report a vulnerability"). That opens a private advisory only
the maintainer can see. Please do not open a public issue for a security
problem: an issue is world-readable the moment it is filed, and it cannot be
made private afterwards.

There is deliberately **no email address here.** A published address is
permanent and gets harvested; private reporting gives the same channel without
one.

## What to expect

This is maintained by one person. I aim to acknowledge a report within **a
week**, and I would rather say that plainly than publish a response time I
cannot hold to. If a report is valid I will work with you on a fix and credit
you in the advisory unless you would prefer otherwise.

There is no support commitment beyond that, and no long-term support branch.
The README says what this package offers and what it does not.

## The surface, stated honestly

The most useful thing this file can do is tell you where to look rather than
assert that it is safe.

- **`ration.frame` decodes bytes another node sent.** That is the untrusted
  input surface. The header is checked before the body, in the order the
  module docstring states, and the body is returned to the caller
  uninterpreted: decoding a payload is the caller's code, not this package's.
  `tests/test_frame.py` and `tests/conformance/test_frame_*.py` hold the
  hostile cases the decoder is expected to refuse.
- **`ration.postgres` connects to the DSN you pass it** and runs fixed SQL with
  bound parameters. A DSN can carry a credential; that credential is yours, and
  this package neither stores nor logs it.
- **`ration.duckdb_store` opens the database file you name.** It is a
  candidate the suite judges, and the README says why it cannot serve more
  than one process.
- **No subprocesses, and no network calls of its own** beyond the store
  connection you configure. The runtime dependency list is empty.

If you find any of the above to be untrue, that is itself worth reporting.
