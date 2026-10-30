# ration

> A ration is a bounded claim on a finite shared pool, granted for a period, and
> **refused rather than overdrawn.**

A coordination substrate, and the lease, pool, addressing and publication
services built on it, with the conformance suite that decides whether a store
can carry them.

What is here: the contract in `src/ration/contract.py`; a suite that
discriminates between a store that keeps it and stores that are wrong on
purpose, one per property; a Postgres backend that passes every property, and
a naive Postgres one that is rejected; `src/ration/lease.py`, which grants one
holder per key; `src/ration/pool.py`, which grants a bounded number of holders
over a set of keys; `src/ration/addressing.py`, which answers *where a role is*
by reading the lease that says who holds it; `src/ration/publish.py`, which
advances a versioned pointer to published state under the same
compare-and-set; and `src/ration/frame.py`, the checked carrier beneath claims
of authority and new payloads between nodes, with its own suite of frames built
wrong on purpose. Treat every statement below as intent unless it points at a
passing test.

Three of the services, built after the suite, each found something every green
run before it had agreed with: the lease, the pool and the publication pointer.
The lease found a defect in the contract's own remedy for an
ambiguous write. The pool found that a retrying holder takes a second slot
unless the scan looks for its own grant first, and that the obvious test for it
**cannot fail**, which only a deliberately wrong implementation revealed. See
*What the first consumer found* and `src/ration/pool.py`.

## What this offers, and what it does not

**The mechanism, and the suite that judges it.** Nothing here is a guarantee,
and nothing is supported beyond what `SECURITY.md` says. The suite is the
contract: where this README and the suite disagree, the suite is right and this
file is the defect.

The suite is evidence about `ration`'s own code and about the stores it was run
against, and about nothing else. A store's guarantee is shown by running the
suite against that store; if you put `ration` in front of a store of your own,
that run is yours to make. A store's own vendor test suite is evidence about
the vendor, not about your deployment, and the same is true of this one.

**Postgres passing is not Postgres being chosen.** The contract makes the store
a conformance result, and a conformance result is evidence, not a decision.
Nothing in `pyproject.toml`'s runtime dependencies names a driver, and in this
repository `ration.postgres` is imported only by the suite. Which store a
deployment uses is that deployment's decision.

## What this is

Two things, in one repository because they own the same state:

1. **The coordination substrate**: an atomic compare-and-set against a
   monotonically increasing version, on a single key, plus the backends that
   implement it and the suite that decides whether a backend qualifies.
2. **The services built on exactly that primitive**: `lease.py`, a bounded,
   expiring claim on one key; `pool.py`, a bounded number of simultaneous
   claims over a set of them; `addressing.py`, which answers *where a role is*
   by reading the lease that says who holds it; and `publish.py`, which
   advances a generation pointer to published state. **They are one thing
   rather than four**: a pool is N keys, each one of these leases; an address
   is a lease's payload; and a publication is the same key discipline with the
   expiry refused. None of them adds an operation of its own.

And a third that owns **no state at all**: `frame.py`, the carrier a claim
of authority or a new payload rides in between nodes, checked before the
payload is looked at. It is here because it leans on the contract exactly once,
for the epoch a restarted sender needs, and a lease version already is one. See
*The frame, and the suite of frames built wrong on purpose*.

The repository is decomposed by state ownership: the substrate and the
services share it because they own the same state. The contract they all rest
on is stated once, in `src/ration/contract.py`, and decided by
`tests/conformance/`:

| Property | What it fixes |
| --- | --- |
| The operation | `compare_and_set` on a single key applies only at the expected version, the comparison and the write are one step, and a conflict is reported without writing |
| 1 | The version is monotonic per key and never reused, not even after the key is deleted |
| 2 | Expiry is evaluated by the store, when it is asked; a TTL is a duration, never a caller's deadline |
| 3 | A caller that cannot reach the store has acquired nothing: unreachable means refuse |
| 4 | A write whose answer was lost is resolvable by reading, because the caller's token is in the store |

## Addressing a role, and why it is not a name record

A role must not be pinned to a host by accident, and a caller that reaches a
service by hostname has done exactly that. So services address each other by
**role**, and something resolves the role to a host.

**That something introduces no new state, which is why it is a module here
rather than a service of its own.** *Who holds this role right now* is already
lease state. The address rides as the lease's own payload, so a role is
announced and held in one operation and there is nothing to keep in step.

```python
announce(store, "producer", "node-a.example:8080", holder="node-a", ttl=30.0)
resolve(store, "producer")
# Address(role='producer', endpoint='node-a.example:8080',
#         holder='node-a', version=7)
```

**The alternative was to publish the address into a name service and let
callers resolve by DNS. It loses on a lesson that is not new.** The lease
already decides who holds a role, so a name record is a *second copy of that
fact*, and it goes stale in exactly the moment that matters: a failover. **Two
copies of a fact is one fact and one stale claim.**

The concrete form of the same objection is the `version` field above. It is
the lease generation the answer was read at, so a caller can be refused by a
callee that has seen a newer one. **A cached name has no generation**, so a
stale holder keeps being reachable and nobody can tell. An address without a
generation is a guess with good manners.

The network still carries the packets. It is the **transport**; it is not the
register of who holds what, and those are different jobs.

**What it costs.** Service addressing now depends on the coordination store
being reachable. A store that refuses under partition already meant a peer
could take no lease while it was unreachable; it now also means the peer can
resolve no role. That **widens the blast radius of a failure a deployment has
already accepted** rather than introducing a new one, which is a smaller claim
than "no new risk" and is the honest one.

**`ttl` is required here where the lease layer allows `None`.** A claim with no
expiry keeps answering for a holder that stopped existing, which is the
stale-name failure arriving through the door this module was built to close.

**The expiry assertions are backed by a control that reproduces the defect.**
Most of `tests/test_addressing.py` asserts an address *stops* answering, and a
test asserting an absence can pass while looking at nothing. So a deliberately
caching resolver is **required to keep answering** for an expired role; if it
ever stops, the tests above it are measuring the passage of time rather than
the store.

## Publishing a generation pointer, and why it is not a lease

A producer publishes an artefact, consumers act on it, and a reader needs to
tell a stale artefact from a current one. `publish.py` gives the pointer a
**generation**: a compare-and-set on a key holding a generation and a pointer,
where the generation is the store's own version, the same number
`addressing.py` carries as `Address.version`.

```python
made = publish(store, "config", "sha256:9f2c...", "producer", generation=UNPUBLISHED)
current(store, "config")
# Publication(topic='config', publisher='producer',
#             generation=1, artefact='sha256:9f2c...')
```

**Only the pointer lives here, because the artefact half has nothing to
coordinate.** Once published bytes are named by content they are immutable,
and immutable data needs no lock: lockfiles and atomic swaps exist only for an
artefact that mutates in place.

**The differences from a lease are the design, and both run in the refused
direction.** A publication takes **no TTL, structurally**: there is no
parameter to pass, where `announce` *requires* one. An address is a claim
about a live process, a publication is a fact about content, and an expiring
pointer would vanish without a successor and read as a topic never published.
And there is **no retract**: a publication is superseded, never deleted, which
also keeps dormant the resolve-after-deletion edge `lease.py` records as *"a
real edge for any consumer that does need the distinction."* The test for
non-expiry calibrates its own clock:
a leased address under the same store must expire across the same advance, so
the publication standing through it is a decision rather than a default.

**What building it found: recovery from an ambiguous advance needs two checks,
and each has a case the other cannot decide.** A publisher is its own
predecessor as the *routine* case, since every advance rewrites a key already
holding its token, and it rewrites with **different payloads**, which the lease
never did. So `resolve`'s APPLIED is no longer enough to hand back a handle: a
publisher identity racing itself (two replicas, or a retry racing its original)
can have its lost write attributed to the write that survived, and only the
**payload** distinguishes them. The other case: a recovery built purely on the
follow-up read, token and artefact both matching, decides one case wrongly, an
ambiguous **republish of the standing artefact**, where nothing in the store
distinguishes the lost write and only the unmoved version says it never
landed. Both cases are pinned in `tests/test_publish.py`. The contract is
untouched by both: the information was in the store the whole time.

**Consumers poll.** The contract offers no watch, so "has the pointer moved?"
is a read and a comparison, repeated. Staleness is *detectable*, not *pushed*;
a cache added to hide the cost would be the defect the addressing section
above documents refusing.

**The limit, stated before it is needed:** a pointer that needs two keys to
move together, an artefact and its index published atomically, needs a
guarantee the contract deliberately refuses. That would be a change to the
contract, not a quiet widening here.

## The frame, and the suite of frames built wrong on purpose

The rule the frame serves: a payload that carries a claim of authority from one
node to another (a fencing claim, a vote, a command one node sends another)
rides inside a frame, and so does every new payload between nodes; a payload
already framed stays framed. The rule says where a payload belongs, not where
every one is today: a consumer moves a route onto the frame in its own change,
so a claim can still cross outside a frame while that move is under way. The
frame is checked before any payload is decoded, and this repository owns the
frame. `src/ration/frame.py` is that and nothing beyond it: no route, no
transport, no consumer.

Two kinds of existing payload are outside the rule on purpose. **A record in
the coordination store** can carry a claim, since its version is a fencing
token, but it crosses between nodes through the store rather than as a
message, and property 1 already orders it (`tests/conformance/test_version.py`);
a frame's sequence beside it would be a second ordering of one record. **The
telemetry an existing shipper sends** carries no claim, and the frame's
sequence rule would break it: a receiver accepts exactly the next count
(`tests/conformance/test_frame_sequence.py`), so one lost batch would leave
every later batch refused as a gap until the sender started a new epoch or the
receiver a fresh ledger.

A frame is an eighteen-byte big-endian header (the frame's own version, the
payload kind, a 64-bit per-sender sequence, the body length, and a CRC-32 over
the header and the body) around a body the module never looks inside. The
checks run in a stated order, and the order is the contract a v2 leans on: a
frame of a version this reader does not speak refuses as exactly that, before
anything assumes v1's layout. The layout table and the order are in the module
docstring, once.

**A frame can also travel as text**, in a request header, for a route that
takes no HTTP body. The frame inside is an unchanged v1 frame, and its seven
checks run on its bytes as they would on a body's; only the carrier differs.
The text form is base64url (RFC 4648, section 5) with no padding, so a frame
of `n` bytes is `ceil(4n / 3)` characters and the header-only frame is 24. The
decode is strict, and strict means canonical: a frame has exactly one text,
the one `encode_text` writes, and any other string is refused as
`malformed-text`, the ninth control, before any frame check runs. Which header
carries the text is the consumer's transport choice and is named on its side,
not here.

The strictness is the property, because the obvious decoders are not strict.
Python's default `base64.b64decode` drops a character outside its alphabet and
returns the bytes the text held without it, and even its strict settings
(`validate=True`, and `binascii.a2b_base64` with `strict_mode=True`) read a
last character whose unused bits are set as the same bytes as the canonical
one, so two texts decode to one frame. The reader therefore decodes and then
re-encodes, and refuses any text that does not come back as itself.

**The sequence is the check with a live failure mode**, because HTTP frames one
request and neither orders nor de-duplicates requests from a sender: a retried
`POST` is a duplicate the transport delivers happily. The rule for a sender
that restarts is written into the sequence itself. The high word is the
sender's **epoch**, the low word its count within it, and a receiver accepts
exactly the next count or a higher epoch at count zero. A lease version is an
epoch already: property 1 makes it monotonic and never reused, so a sender
that re-announces its role after a restart holds an epoch strictly above any it
held before, without persisting anything. An older epoch is refused as
replayed, which is the stalled holder a fencing token exists to refuse,
arriving on the wire. And **the ledger moves on acceptance only**, so the retry
of a frame that was refused carries the same sequence and is taken for the
first time, while the retry of a frame that was accepted is refused as the
duplicate it is. The two retries are byte-identical; the ledger is what tells
them apart.

The suite is the price of the word *checked*: every hostile case has a
known-negative that fails without the check beside a known-positive that passes
with it. The known-negative is not a bad frame; it is a **reader without the
check**, one per check, each the shape a shortcut takes:

| Property | Hostile cases | The codec that must fail it |
| --- | --- | --- |
| The bytes present honour the declared length, exactly | truncated mid-header, truncated mid-body, length larger than present, length smaller than present | `unchecked-length` - trusts the transport, so a short header is an exception and a short body is misnamed; `ignores-trailing` - takes the declared body and discards the rest |
| A flipped bit anywhere is refused as corruption | a bit flipped in the body, in the sequence, in the epoch, in the kind byte | `unchecked-crc` - carries the CRC and never compares it; `body-only-crc` - a CRC over the body alone, which leaves the header bare |
| An unknown version is refused as that, before its layout is assumed | version ahead of the reader, version ahead with the CRC moved, version zero | `unchecked-version`; `crc-before-version` - every check present, only the order wrong |
| An unregistered kind is refused | kind unknown to the registry, kind zero | `unchecked-kind` |
| Exactly the next sequence, or a fresh epoch at zero | duplicated, skipped, behind, restart at a fresh epoch, an older epoch replayed, a new epoch not at zero, retry of a refused frame | `unchecked-sequence`; `counter-only` - 32 bits and no restart rule, so it turns the honest restart away; `sequences-before-checking` - the right rule, applied before the CRC has spoken |
| Only a frame's canonical text is read, and any other string is refused as text | a character outside the alphabet, whitespace, a character outside ASCII, padding, the standard alphabet's `+` and `/`, a length one past a multiple of four, a second text of the same frame; and a well-formed text of bad bytes, refused by the frame rather than the text | `lenient-text` - the standard library's default decode, which drops what it does not recognise; `skips-text-decode` - hands the header to the frame reader undecoded; `accepts-non-canonical` - every character checked and the round trip skipped, which is as far as the library's strict settings go |

Same mechanism as the store suite, not a copy of it: one marker, one strict
xfail, one set of bookkeeping assertions in
`tests/test_the_suite_discriminates.py`, parametrised over two registries. A
run reports one `xfailed` line per codec per property it drops, which is one per
class in `tests/frame_violators.py`
(`grep -c '^class .*(FrameCodec)' tests/frame_violators.py`); the run is the
count, not this sentence.

**What building it found.** The first run of the suite had
`sequences-before-checking` failing the **four** properties it does not claim,
beside the one it does. Every property test ends each refusal with the healthy
frame accepted, and a reader that consumes a sequence on refusal breaks the
retry after *every* refusal, so it failed length, integrity, version and kind
for a defect that is entirely about the ledger. A violator that fails
everything proves nothing about which check caught it. The fix is in
`tests/support/codecs.py`: the four properties that are not about sequence are
decided against a ledger that has seen nothing, so a sequence refusal cannot
masquerade as theirs, and the one property that is about the ledger names its
sender on every decode. The same run had `unchecked-crc` red on the
**sequence** property, because that test's "refused frame" was refused by the
CRC; it is now a truncation, which every reader refuses whichever check it is
missing, so the only thing decided there is what a refusal does to the ledger.
Both are the same lesson the store suite records about `conflict-writes`,
arriving from the other direction: the property, not the assertion, is the
unit, and a property test must hold every other property still.

The text property's first run taught it a third time. Its arm for a fault in
the bytes under a well-formed text asserted `crc-mismatch`, so `unchecked-crc`
and `body-only-crc` failed a text property for a CRC defect. That arm now
asserts only that the refusal is a frame control rather than the text's, and
`tests/test_frame.py` pins the exact control on the reference reader. The
second run had `body-only-crc` red on the standard-alphabet arm: the frame's
only `-` had been in the CRC's characters, which that reader writes
differently, so the arm went vacuous for it and its own guard said so. The
frame's epoch is now chosen to put a `_` in the sequence field, in bytes every
reader writes alike.

**Three costs stated rather than hidden.** A ledger that has seen nothing takes
the first frame as its baseline, so a replay against a fresh receiver is not
caught; the alternative would turn every receiver restart into a forced restart
of every sender. The header carries **no sender field**: which sender a frame
belongs to is the receiver's knowledge, and a sender identity in the header
would be a claim the frame cannot verify. And the kind registry is **closed and
short**: five kinds, a consumer's fingerprint envelope by the envelope's own
name, one per body-carrying route of a consumer's service, and `role-claim`.
Their codes are on the wire and their names label them, so they do not change.

`role-claim` is a claim-only body, the fencing claim a sender presents and
nothing beside it: the kind a route with no HTTP body takes, its frame carried
as text. It is one code for every such route rather than one per route. A code
means one body shape and this is one shape, and the frame carries a CRC, not a
MAC, so anyone who can build a frame can build it with any registered kind: a
code per route would catch only a sender wired to the wrong route. The claim
rides in the body, where the CRC covers it and the sequence orders it, for the
same reason the header carries no sender: in the header it would be a claim
the frame cannot verify. In the body the frame still says only that it arrived
as sent, once; whether the sender may present it is the consumer's check.

A sixth kind lands here before anything sends it. A code nobody sends is inert,
while a decoder selecting on a code this registry does not name has every
framed request refused as `unknown-payload-kind`; so the row comes first, and a
consumer that installs this package at a commit sees it when it moves to that
commit. `src/ration/frame.py` carries the argument beside the registry.

## The name

A **ration** is a bounded portion of a finite pool, issued for a period. The
word already carries the property the system needs most: **rationing
over-refuses rather than overdraws.** The error direction is fail-closed, and
here it is in the name rather than in a comment.

## The first piece of work, and why it is not the service

The conformance suite comes before any backend, and before the lease service.
The contract's central claim is unproven by construction: that a backend can be
*held* to the contract, and that a suite can **reject** one.

So the suite's job is not to validate a backend. **It is to discriminate between
backends**, and the design follows from that: a deliberately violating
implementation per property, each breaking as little as it can:

| Property | The implementation that must fail it |
| --- | --- |
| Compare-and-set applies only at the expected version | `create-always` - absence is not a version, so creation is an upsert |
| ...and a conflict does not write | `conflict-writes` - reports the conflict honestly, writes anyway |
| The comparison and the write are one step | `naive` - read-then-write, exactly what the operation must not be |
| Version monotonic, never reused, not even after deletion | `recycling` - drops the key's counter when the value goes |
| ...and per key, not per store | `global-counter` - monotonic, never reused, and a statement about unrelated traffic |
| Expiry evaluated by the store | `client-expiry` - asks the caller what time it is, so skew is a lease held twice |
| ...when it is asked, not when it last looked | `stale-clock` - the store's own clock, fetched and then acted on later |
| Unreachable means refuse | `fail-open` - a layer in front of a correct store that treats silence as "nothing there" |
| An ambiguous write is resolvable by reading | `token-dropping` - keeps the payload, substitutes the token |
| ...and all of the above, in a store that is not a fake | `postgres-naive` - `SELECT` then `UPDATE`, exactly as the operation reads in English |

**If a violating implementation passes, that property is not being tested.**
The suite states that as a mechanism rather than a hope: a violator is marked
`xfail(strict=True)` against its own property, so a violator that passes is a
**failing run**. Same standard as the guards under `scripts/`, each of which
runs its own self-test before it runs in CI: proof it can fire and proof it can
go quiet.

**Two of those rows are one implementation short of the ideal, and it is worth
knowing which.** `conflict-writes` is declared against two properties rather
than one, because a store that writes when it should not destroys the winner's
value in any race it loses: there is no interleaving in which it is atomic. A
violator that broke only the reply would be better and does not exist.

**The version property is still short its most likely violator.** Restoring a
store from a backup can replay or invalidate lease versions, so a restore is
not an ordinary database restore. Both violators above are one process
misbehaving; reuse-after-restore is an ordinary recovery, run by someone who
believes they are restoring a database. It is harder to reach, a lifecycle
event *between store instances* rather than a defect inside one, so no
in-process implementation expresses it, and the suite does not cover it.
Stated here so the gap is a known absence rather than a discovery made during
a recovery.

**A claim the suite did not cover was false for as long as nobody looked.**
`Conflict` and the Postgres `compare_and_set` both stated that a conflict
reports the version the key had moved to. It does not. Under concurrency
`PostgresStore` reports the version the caller came **from**: two writers from
version 1 leave the loser holding `Conflict(1)` while the key is at 2; two
creators leave the loser holding `Conflict(0)` while the key is at 1. The
statement that decides the outcome and the statement that reports the version
share one snapshot, and only the `UPDATE` half gets `EvalPlanQual`.

**Not a contract defect.** The contract requires a conflict to be reported
*without writing*, which holds, and says nothing about the version it carries.
Making it exact costs either a second round trip or an
`ON CONFLICT DO UPDATE` that **writes in order to read**, which is precisely
what `conflict-writes` exists to fail. So the claims were corrected and the
behaviour pinned by `tests/test_postgres_conflict_version.py`,
deterministically, with a held row lock rather than a race. A caller needing
the current version must read.

**The lesson is about the suite, not the store.** Two docstrings agreed with
each other and disagreed with the server, and every test passed throughout,
because nothing asserted in that area at all. **A property the suite does not
decide is a property the prose is free to be wrong about**, and prose is what a
reader trusts.

**The hard part is concurrency.** A lost update only appears under concurrent
access, and a concurrency test that has never been run against an
implementation known to lose updates can run green while not looking. So the
instrument here is calibrated against a known-bad implementation *before* it is
trusted. The calibration is what turned "the hammer passes" from a result into
a question: at the interpreter's default switch interval the probe ran green
against `naive`, the one implementation that must never pass, at the rate
recorded under **The first calibration** below. A suite that had merely *run*
it would have shipped that green as evidence.

Threads or a deterministic interleaving? **Both, doing different jobs.** The
interleaving is the gate: it parks one operation at the last moment before its
atomic step, runs another **to completion**, and, the part that matters,
asserts that the interleaving happened. It is needed by two properties rather
than one, and the second is `stale-clock`: a store that reads its own clock and
then evaluates expiry somewhere else defeats clock injection entirely, because
the reading it used was never the caller's.

The seam sits *outside* every lock, and that placement is the reason the stepper
is deterministic rather than lucky. A parked operation holds nothing, so the
second one cannot be blocked by it and is waited for rather than given a window
and assumed. Giving it a fixed fraction of a second and carrying on either way
would reintroduce the failure this section argues against: a run that missed
its own interleaving, reporting green.

**A hammer can tell that it raced, and not whether it raced inside the
window.** A writer refused because another moved the version between its read
and its write was refused by a genuine overlap, so the threaded test counts
refusals and reports *"the probe never raced"* instead of a verdict when there
were none. What it cannot do is land inside the read-to-write window on
demand: a violating implementation that is never made to race inside it has
not been shown to survive one. So threads are a **probe, not a gate**: they
reach non-atomicity inside a real store that no client-side stepper can, and
they carry a false-pass rate the suite states rather than implies. The order of
the two checks is in `tests/conformance/test_atomicity.py`'s docstring.

**The first calibration is the finding worth keeping.** On the interpreter's
default switch interval, the hammer **never once** caught the naive backend: it
ran green against the implementation that must never pass. The window between a
naive read and its write is about a microsecond and the interpreter was
switching every five milliseconds, so the probe was not unlucky; it was not
looking. Forcing preemption to a microsecond, the same counts caught it **every
run**. The thread count, the iteration count and both rates live in
`tests/conformance/test_atomicity.py`, beside the probe they describe, and are
deliberately not repeated here. That rate is a **measurement, not a
guarantee**: no false pass was observed, which is not the same as none being
possible, and it is precisely why the property does not rest on it. The naive
backend stays in the hammer run: if the hammer ever passes it, the hammer is
measuring nothing and fails on that basis.

Neither of these is written into the contract as a requirement on future
backends. A seam invented before any backend exists would be a contract clause
with no caller, and it does not need to be a clause, because `AGENTS.md`
already covers the case: a backend that needs a special case to pass is a
finding about the backend or the contract, not a fixture to be written.

## Every service is one compare-and-set

- **The lease**: `acquire`, `renew` and `release`, each of them one
  compare-and-set, and **no new method on `Store`**. A contract is tested by
  what its first real consumer needs that it lacks, and the answer was
  **nothing**: the refusals held, at a cost of one extra read on the ambiguous
  path.
- **The pool**, which is what the name promises. The contract forced the design
  rather than leaving it open: expiry is evaluated by the store, and a store
  expires *keys* rather than elements inside a value, so a pool of N grants on
  one key would have its deadlines evaluated by whoever read it, which is
  `client-expiry`, a violator the suite already rejects. So **a pool is N keys,
  each one of these leases**, and capacity is the number of keys rather than a
  number written down anywhere. The evidence is what `pool.py` does not
  contain: no `renew` and no `release`, because a pool grant *is* a lease.
- **The generation pointer**: `publish.py`, versioned published state as a
  compare-and-set on one key, the same primitive as a lease rather than a
  second one.
- **The frame** adds no key and no operation, and leans on the contract exactly
  once, for the epoch a restarted sender needs.

## What the real backend cost, and what it found

Two things came out of running the suite against something that is not a fake,
and both were about the *suite* rather than about Postgres:

- **A fake clock is not a clock, and an in-process seam does not cross a
  socket.** The harness had been written when only fakes existed. Time is now
  passed by `advance_store_time()`, moving a fake clock or actually waiting, and
  the deterministic stepper *skips* where a store's atomic step is one SQL
  statement. That skip removes an instrument, never a verdict: `postgres-naive`
  has no seam either and is still expected to fail, on the threaded probe.
- **A store that passed every property still could not survive being asked.**
  The first backend opened a connection per operation, which is the obvious
  shape and reads as the careful one. The threaded probe put about 1,600
  connections through it, and the *next* run failed on
  `Address already in use`: every one of them held an ephemeral port in
  `TIME_WAIT`. Nothing in the contract catches that; there is no property about
  exhausting the machine. It was found by running the real thing under real
  load, which is the whole argument for deciding a backend by conformance
  result rather than by comparison. `PostgresStore` reuses one connection per
  thread.

## What a second real backend found, and why it does not qualify

`duckdb` is registered as a case alongside `postgres`, with an empty `violates`
set. **It passes every conformance test.** That is the result, and on its own it
would qualify the backend.

**It cannot be a coordination store for more than one process, and the suite is
structurally unable to say so.** DuckDB is embedded: the database is a file and
one process owns it. A second process cannot open it. That is measured from a
real interpreter under `spawn` and asserted, not described, in
`tests/test_duckdb_single_process.py`, with a control proving the same child
succeeds once the holder lets go. A coordination store exists so that other
processes, and other hosts, can reach it, and there is nothing here to reach. It
does not fail at the second host. It fails at the second **process**, which is
where a pool's contenders live.

**This is not a defect the contract can name, and the contract should not grow
one.** Every violator in `tests/violators.py` is wrong *logically*: it computes
an answer the contract forbids, in one process, where the suite is looking.
DuckDB is wrong about **deployment topology**, which the contract deliberately
does not govern. Adding a property for it would be inventing a clause with no
caller. So the suite's verdict on DuckDB is correct and incomplete, and the
single-process test is the part that completes it.

**What it was actually worth: it found a hole in the suite, and a consumer found
it first.** DuckDB's creation path raised `Duplicate key ... violates primary
key constraint` under load, from `tests/test_lease.py`, after the conformance
run had pronounced the same path correct. The cause was in the suite's own
instruments: **every concurrent test seeded its key single-threaded and then
raced from that version**, so the suite had only ever raced *updaters*. The
`INSERT` branch had never been entered by two callers at once.
`test_two_creators_racing_from_absence_do_not_both_apply` closes it, and the
control is the usual one: with the defect reintroduced the new test goes red
while the entire pre-existing suite passes green.

**The second-order lesson is about probes, not about DuckDB.** The first draft
of that test was threaded only. It caught `naive` every run in isolation and then
let it through inside the full suite, because a creation can be raced only once
per key and a loaded machine serialises the racers. A single shot per round is
not something a strict xfail can stand on, so the test uses the deterministic
stepper wherever a seam exists and the probe only where none does, the split
the rest of the atomicity file already uses.

## What the first consumer found

Both findings are the **same lesson as the `Conflict` correction above**,
arriving from a new direction: a property nothing asserts on is a property the
code is free to be wrong about, and the prose will agree with it the whole time.

**`resolve` decided by token before version, and that is wrong for a caller
that is its own predecessor.** Property 4's remedy recognises the caller's own
write by finding its token in the store. A renewing lease already has its token
there, from the grant before, so a renew whose answer was lost resolved as
`APPLIED` on the strength of the *earlier* write, and the holder was told it had
a TTL the store had never been given. Fixed by testing the version first, which
property 1 makes decisive: every write advances the version, so a key still
standing at the expected one has not been written by anybody, and no token can
argue otherwise.

- **The suite as it then stood could not have caught it.** `test_ambiguity.py`
  created and never rewrote, and the token stops identifying a *write* only
  once a caller writes the same key twice. Nothing did until the lease did;
  `test_a_rewriters_lost_answer_is_resolved` now reaches that case.
- **The contract is untouched.** The information needed was in the store the
  whole time and the procedure was reading it in the wrong order. This is a
  defect in the remedy, not in the contract.
- **Calibrated before it was trusted**: the regression test was run against
  the *old* ordering and fails there, returning a lease at the unchanged
  version. A test that has only ever been seen to pass has not been shown to
  test anything.

**The exclusivity check was green while never racing, and the negative
control is the only reason that is known.** Starting eight threads in a loop is
not a race: an in-memory acquire finishes in microseconds, so the first claimant
was done before the last was started. Exclusivity passed, on a window it never
entered. The control, the same contention against `naive`, which *must*
double-grant, failed, which is what a control failing is for.

**It then took two more shapes to get right, and the second failure is the
instructive one:**

- A `threading.Barrier` fixed it on a machine with many cores and **failed on a
  smaller one**. A barrier synchronises *arrival*, and arrival is not overlap:
  on a smaller machine the winner finishes its whole compare-and-set before the
  loser is scheduled, so sampling has to catch the overlap in the instant it
  exists.
- Counting a violation **per operation** rather than sampling a moment does
  work, catching `naive` in roughly half the rounds.

So **it was replaced by a deterministic gate anyway, and not because it
failed.** `naive`'s seam sits exactly in its gap, so one claimant can be
*parked* there while another runs to completion. **Where a store has a seam, a
gate beats any per-round rate**: a probe that catches a known-bad store half
the time is evidence about the probe; a parked thread is a verdict. The
threaded probe keeps its place because **`postgres` has no seam**, and there it
is the only instrument there is. The gate is proven both ways: the identical
interleaving against the correct store grants **one** holder.

### Threads or processes, answered with a number instead of a paragraph

The threaded probe is eight threads in **one** interpreter, and a lease
service's real population is processes, often on more than one host. Whether
that difference matters is a question the suite answers rather than assumes.

`tests/test_postgres_cross_process.py` is the same arithmetic, eight contenders
with one hundred increments each, run as eight **operating-system processes**
under `spawn`, each building its own store from nothing but a DSN.

**For detection it makes no difference, and that is a measurement.** Both
populations were pointed at `postgres-naive`, which has no version predicate in
its write and must lose increments. One set of runs on a development machine:

| Population | Increments lost of 800 | Runs |
| --- | --- | --- |
| 8 threads, one interpreter | 673, 687, 689 | 3 |
| 8 processes, `spawn` | 662, 668, 669, 671, 673, 686 | 6 |

The ranges overlap. Against the real store, `postgres` returned **800 of 800 in
every run of both**. **The thread count was never the interesting variable
here**: every operation is a round trip, so the writers already interleaved at
the socket without the interpreter needing to be persuaded to switch, which is
the reason the threaded hammer catches this store so much more reliably than it
catches the in-memory `naive`.

**What the process version does buy is structural, and it is not the
contention.** The threaded hammer shares **one `PostgresStore` instance** across
its eight workers; that instance holds a `threading.local()` connection
registry, so the workers are eight clients of one store object. A deployment is
not that. Any defect living in state accidentally shared *inside* a store
instance is invisible where there is only one instance to share it, and this
probe has one store per process, so it cannot inherit that blindness. The claim
is narrow on purpose: **it removes a caveat, it does not raise the catch
rate.**

**The harness is calibrated before either verdict is read**, because a `spawn`
pool that quietly ran every task in the parent would produce a perfect counter
and prove nothing: it would be the threaded result wearing this test's name.
Each worker reports its PID and the test asserts it saw eight distinct ones.

Still a probe. It has no seam to park in either, so a run that finds nothing
has not shown there was nothing to find. What keeps it honest is that the
identical harness is **required to catch `postgres-naive`**, as a plain
assertion rather than an xfail, because there the subject is the harness.

### A refusal that arrives late

`PostgresStore` reuses a thread's connection while `conn.closed` is false, and a
connection the server killed still reads `closed == False` until the client next
talks to it. A store restart therefore cost each idle reader one refusal
**whenever it next called**, which can be hours later, reported as a store
failure about a store that had been back the whole time.

- **`read()` redials once** when the statement fails on a *reused*
  connection. A store that is really down still refuses, because the redial is
  a real connect: property 3 is not traded for it.
- **`compare_and_set()` never does**, including on a connection that was
  dead before the statement left. The client cannot tell that from a write that
  applied and lost its answer, and repeating the second is a double write,
  which property 4 exists to prevent. The caller resolves it by reading, and
  the read is what redials.
- **Pinned by the event itself.** `tests/test_postgres_killed_connection.py`
  kills the store's backend with `pg_terminate_backend` against the server CI
  already runs, so the sequence is replayed on every pull request. Its fakes
  cover only what no server can stage.
- **And every one of its tests runs against four stores built wrong on
  purpose**, under the same strict xfail the contract's violators get: the
  store as it stood before, one that redials until it works, one that forgets
  which connections it opened itself, and one that gives a write the read's
  courtesy. A test that stops rejecting is a failure on the next run.

## Versions and releases

**No release has been cut**, and the workflow that would build one is not in
this tree: install from a checkout, as below. A release is meant to be a signed
`v*` tag, with a GitHub release built from it only after the tag's signature
and the suite have been checked. The version in `pyproject.toml` at a commit no
tag names is not a release. While
the major version is 0, a change to what the suite decides moves the minor
version and may break a caller; a patch release never changes what the suite
decides.

**Do not install by the bare name.** `ration` on PyPI is an unrelated project.
This package is not published to a registry: the wheel would carry
`src/ration` and not the suite that judges it, and the suite is the point.

## Running it

```sh
pip install -e ".[dev]"
pytest                                   # the fakes only
```

The Postgres cases are registered only when a server is named, so a run without
one silently drops the only store in the suite that more than one process can
share. That is fine
locally and unacceptable in CI, where `RATION_REQUIRE_POSTGRES=1` makes its
absence a failure:

```sh
docker run -d --name ration-pg -e POSTGRES_PASSWORD=ration \
  -e POSTGRES_USER=ration -e POSTGRES_DB=ration -p 55432:5432 postgres:17-alpine
until docker exec ration-pg pg_isready -h 127.0.0.1 -U ration -d ration; do sleep 1; done
RATION_POSTGRES_DSN=postgresql://ration:ration@localhost:55432/ration pytest
```

**The wait is load-bearing, and `-h 127.0.0.1` is what makes it one.** On a
new container the image's entrypoint first runs a temporary server for its init
with `listen_addresses=''`: a unix socket and no TCP (`docker-entrypoint.sh`,
`docker_temp_server_start`). It then stops that server and starts the real one.
Over TCP, `pg_isready` answers only once the real server listens. Over the
socket it can answer during the temporary one; that is read from the
entrypoint, not observed. Without the wait, a run started against a new
container can fail with `server closed the connection unexpectedly`, while the
same suite against a server that has finished starting is green.

A run reports `xfailed` once per violator per property it is expected to break.
Those are the suite working. A **passing** violator is reported as a failure,
which is the only outcome that means the suite has stopped measuring anything.

## Licence

Apache-2.0. See `LICENSE`.
