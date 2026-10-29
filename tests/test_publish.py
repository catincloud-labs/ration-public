"""The generation pointer, against every store that is not a violator.

Same standing as `test_lease.py`, `test_pool.py` and `test_addressing.py`, and
for the same reason: conformance decides whether a store implements the
contract, and this file decides whether the publish discipline built on the
contract behaves. Running it against implementations that are wrong on purpose
would re-report findings the suite already owns.

**The negative control at the bottom is a gate, not a probe**, and it opens
the exact window the fencing exists to close: two publishers advancing from
one generation over a store whose comparison and write are not one step. Both
are told their artefact is current, and one publication is silently destroyed.
The identical interleaving against a correct store refuses one of them, because
a control observed only to fire has not been shown to go quiet.
"""

from __future__ import annotations

import threading

import pytest

from ration.addressing import announce, resolve as resolve_role
from ration.contract import Resolution, Unreachable, resolve
from ration.memory import MemoryStore
from ration.publish import UNPUBLISHED, Publication, current, key_for, publish
from tests.support.backends import CASES, REAL_TTL, BackendUnderTest
from tests.support.faults import FaultInjector
from tests.support.gate import Gate
from tests.support.preemption import preemptive
from tests.violators import NaiveStore

#: Filtered rather than listed, so a store added to the registry is exercised
#: here without this file changing.
CORRECT_CASES = [case for case in CASES if not case.violates]

#: Publishers and attempts for the load probe at the bottom. Deliberately the
#: contended shape: every publisher advances the same topic, so most attempts
#: are refused and the assertion is about what the refusals protected.
PUBLISHERS = 8
ATTEMPTS = 25


@pytest.fixture(params=CORRECT_CASES, ids=[case.name for case in CORRECT_CASES])
def correct(request: pytest.FixtureRequest) -> BackendUnderTest:
    case = request.param
    return case.build()


# -- what a publication is ---------------------------------------------------


def test_a_published_topic_reads_back_what_its_publisher_pointed_at(
    correct: BackendUnderTest,
) -> None:
    topic = correct.key()
    made = publish(
        correct.store, topic, "sha256:aaa", "producer", generation=UNPUBLISHED
    )
    assert made is not None

    found = current(correct.store, topic)
    assert found == Publication(
        topic=topic,
        publisher="producer",
        generation=made.generation,
        artefact="sha256:aaa",
    ), "a reader and the publisher hold different facts about one publication"


def test_a_topic_nobody_published_reads_as_nothing(
    correct: BackendUnderTest,
) -> None:
    assert current(correct.store, correct.key()) is None


def test_the_first_publication_is_fenced_like_any_other(
    correct: BackendUnderTest,
) -> None:
    """Absence is a version, so creation gets the same refusal as an advance.

    Two publishers that both believe a topic is unpublished are two
    compare-and-sets from `UNPUBLISHED`, and the second is refused rather than
    layered on top -- a bootstrap special case, dissolved by the contract's
    `ABSENT`.
    """
    topic = correct.key()
    assert publish(
        correct.store, topic, "sha256:aaa", "producer-a", generation=UNPUBLISHED
    )

    assert (
        publish(
            correct.store, topic, "sha256:bbb", "producer-b", generation=UNPUBLISHED
        )
        is None
    ), "two first publishers both applied, so a reader saw whichever came second"

    found = current(correct.store, topic)
    assert found is not None and found.artefact == "sha256:aaa", (
        "the loser's artefact was published anyway"
    )


# -- the fencing token -------------------------------------------------------


def test_advancing_spends_the_generation_it_advanced_from(
    correct: BackendUnderTest,
) -> None:
    """The fencing property, in the self case: a publisher against itself.

    The generation this publisher advanced from a moment ago is not merely old,
    it is spent -- so a stalled copy of the same publisher, resuming with the
    generation it was holding when it stalled, is refused by the store rather
    than trusted to have kept up.
    """
    topic = correct.key()
    first = publish(
        correct.store, topic, "sha256:aaa", "producer", generation=UNPUBLISHED
    )
    assert first is not None

    second = publish(
        correct.store, topic, "sha256:bbb", "producer", generation=first.generation
    )
    assert second is not None
    assert second.generation > first.generation

    assert (
        publish(
            correct.store, topic, "sha256:ccc", "producer", generation=first.generation
        )
        is None
    ), (
        "an advance from a spent generation applied, so the generation is not "
        "fencing anything and a stalled publisher can overwrite its successors"
    )
    found = current(correct.store, topic)
    assert found is not None and found.artefact == "sha256:bbb"


def test_a_stalled_publisher_cannot_overwrite_a_newer_publication(
    correct: BackendUnderTest,
) -> None:
    """The same fence, in the case a generation pointer exists for: a competitor moved it."""
    topic = correct.key()
    held = publish(
        correct.store, topic, "sha256:aaa", "producer-a", generation=UNPUBLISHED
    )
    assert held is not None
    assert publish(
        correct.store, topic, "sha256:bbb", "producer-b", generation=held.generation
    )

    assert (
        publish(
            correct.store, topic, "sha256:stale", "producer-a", generation=held.generation
        )
        is None
    ), "a publisher that had been superseded overwrote the newer publication"

    found = current(correct.store, topic)
    assert found is not None
    assert found.artefact == "sha256:bbb" and found.publisher == "producer-b"


def test_a_reader_can_advance_from_the_generation_it_read(
    correct: BackendUnderTest,
) -> None:
    """The read half and the write half meet in one field, and it is enough.

    `current` hands back the same type a publisher holds, so becoming the next
    publisher is a `publish` from the generation just read -- no other state
    crosses from one side of the protocol to the other.
    """
    topic = correct.key()
    assert publish(
        correct.store, topic, "sha256:aaa", "producer-a", generation=UNPUBLISHED
    )

    seen = current(correct.store, topic)
    assert seen is not None
    advanced = publish(
        correct.store, topic, "sha256:bbb", "producer-b", generation=seen.generation
    )
    assert advanced is not None
    assert advanced.generation > seen.generation


# -- no expiry, with the clock proven to have moved --------------------------


def test_a_publication_outlives_what_an_address_does_not(
    correct: BackendUnderTest,
) -> None:
    """A pointer is a fact about content, not a claim about a live process.

    Asserting that something is *still there* after time passes is trivially
    green against a clock that never moved, so the clock is calibrated inside
    the test: an address announced with a TTL under the same store must expire
    across the same advance. The address vanishing is proof the store's time
    passed; the publication standing through it is then a decision, not a
    default.
    """
    topic = correct.key()
    role = correct.key()
    assert publish(
        correct.store, topic, "sha256:aaa", "producer", generation=UNPUBLISHED
    )
    assert announce(correct.store, role, "node-a.example:8080", "producer", ttl=REAL_TTL)

    correct.advance_store_time(REAL_TTL * 2)

    assert resolve_role(correct.store, role) is None, (
        "the leased address survived the advance, so store time never passed "
        "and the assertion below would be measuring nothing"
    )
    found = current(correct.store, topic)
    assert found is not None and found.artefact == "sha256:aaa", (
        "a publication expired, so a pointer vanished without a successor and "
        "readers see a topic that was never published"
    )


# -- the arguments this refuses ----------------------------------------------


def test_a_topic_without_a_name_or_an_artefact_is_refused(
    correct: BackendUnderTest,
) -> None:
    with pytest.raises(ValueError):
        publish(correct.store, "", "sha256:aaa", "producer", generation=UNPUBLISHED)
    with pytest.raises(ValueError):
        publish(correct.store, correct.key(), "", "producer", generation=UNPUBLISHED)


def test_a_topic_key_is_refused_as_a_topic_name() -> None:
    """`key_for(key_for(t))` would publish somewhere no reader looks."""
    with pytest.raises(ValueError):
        key_for(key_for("config"))


def test_an_unreachable_store_publishes_nothing_and_reads_as_nothing_either(
    correct: BackendUnderTest,
) -> None:
    """Property 3, reaching both halves of this consumer.

    The read half matters as much as the write half here: a `current` that
    swallowed `Unreachable` into `None` would report a partitioned store as a
    topic never published, and a consumer would fall back to defaults while the
    real pointer sits unreadable on the other side.
    """
    topic = correct.key()
    with correct.partition():
        with pytest.raises(Unreachable):
            publish(
                correct.store, topic, "sha256:aaa", "producer", generation=UNPUBLISHED
            )
        with pytest.raises(Unreachable):
            current(correct.store, topic)

    assert correct.store.read(key_for(topic)).record is None, (
        "the refused publish left something behind"
    )


# -- property 4, where being your own predecessor is the routine case --------


def test_an_ambiguous_first_publication_that_landed_is_recovered(
    correct: BackendUnderTest,
) -> None:
    topic = correct.key()
    with correct.ambiguity(applied=True):
        made = publish(
            correct.store, topic, "sha256:aaa", "producer", generation=UNPUBLISHED
        )

    assert made is not None, (
        "the pointer is in the store and the publisher was told it failed, so "
        "it will republish over its own publication or walk away from a "
        "generation it actually holds"
    )
    found = current(correct.store, topic)
    assert found is not None
    assert made.generation == found.generation, (
        "the recovered publication carries the wrong generation, so its next "
        "advance compares against a fence that was never there"
    )


def test_an_ambiguous_first_publication_that_never_landed_is_not_claimed(
    correct: BackendUnderTest,
) -> None:
    topic = correct.key()
    with correct.ambiguity(applied=False):
        made = publish(
            correct.store, topic, "sha256:aaa", "producer", generation=UNPUBLISHED
        )

    assert made is None
    assert current(correct.store, topic) is None


def test_an_ambiguous_advance_that_never_landed_is_not_claimed(
    correct: BackendUnderTest,
) -> None:
    """The lease's regression, met here on every write after the first.

    An advancing publisher's token is already in the store from its previous
    publication, so a `resolve` that trusted the token before the version would
    report this lost advance as applied on the evidence of the *earlier* write
    -- and the publisher would believe the new artefact is current while every
    reader still sees the old one. The lease meets this shape only on renew;
    for a publisher it is the routine case, which is why it is pinned here too.
    """
    topic = correct.key()
    first = publish(
        correct.store, topic, "sha256:aaa", "producer", generation=UNPUBLISHED
    )
    assert first is not None

    with correct.ambiguity(applied=False):
        advanced = publish(
            correct.store, topic, "sha256:bbb", "producer", generation=first.generation
        )

    assert advanced is None, (
        "an advance that never reached the store was reported as applied, so "
        "the publisher believes readers see an artefact the store never held"
    )
    found = current(correct.store, topic)
    assert found is not None and found.artefact == "sha256:aaa"
    assert found.generation == first.generation, (
        "the store moved, so this test is not exercising the case it claims"
    )


def test_an_ambiguous_advance_that_landed_carries_the_new_artefact(
    correct: BackendUnderTest,
) -> None:
    """The recovered handle must name the artefact that is actually standing.

    A `Publication` is acted on through its `artefact` field, so recovery
    checks the payload as well as the token before handing one back -- a handle
    naming the new artefact on the evidence of a record holding the old one
    would be wrong in the one field a reader acts on.
    """
    topic = correct.key()
    first = publish(
        correct.store, topic, "sha256:aaa", "producer", generation=UNPUBLISHED
    )
    assert first is not None

    with correct.ambiguity(applied=True):
        advanced = publish(
            correct.store, topic, "sha256:bbb", "producer", generation=first.generation
        )

    assert advanced is not None
    assert advanced.artefact == "sha256:bbb"
    assert advanced.generation > first.generation
    found = current(correct.store, topic)
    assert found is not None
    assert advanced.generation == found.generation


def test_an_ambiguous_republish_of_the_standing_artefact_is_not_claimed(
    correct: BackendUnderTest,
) -> None:
    """The case only the version can decide.

    A recovery built purely on the follow-up read -- token and artefact both
    matching -- would pass every other test in this file, because in every other
    ambiguous case something in the standing record differs from what the lost
    write would have left. The one case where nothing differs is a republish of
    the artefact already standing: same token, same payload, and the only
    witness that the write never landed is the version not having moved. That
    is `resolve`'s NOT_APPLIED, and this test is what keeps it load-bearing.

    The honest answer is `None` -- the caller's *write* did not apply, whatever
    the store happens to hold -- and the caller's next `current` tells it the
    artefact stands anyway, which is `lease.py`'s division of labour: the
    outcome describes the write, the read describes the world.
    """
    topic = correct.key()
    first = publish(
        correct.store, topic, "sha256:aaa", "producer", generation=UNPUBLISHED
    )
    assert first is not None

    with correct.ambiguity(applied=False):
        again = publish(
            correct.store, topic, "sha256:aaa", "producer", generation=first.generation
        )

    assert again is None, (
        "a republish that never reached the store was reported as applied, on "
        "the evidence of a record an earlier write left -- token and artefact "
        "cannot decide this one, and the version check has stopped deciding it"
    )
    found = current(correct.store, topic)
    assert found is not None and found.generation == first.generation, (
        "the store moved, so this test is not exercising the case it claims"
    )


def test_a_publisher_racing_itself_is_not_handed_an_artefact_it_did_not_publish() -> None:
    """The case the recovery's artefact check exists for, arranged so it fires.

    The token identifies a writer, not a write, and this is the case of that
    which version ordering cannot fix. A publisher identity running two concurrent advances from one generation --
    two replicas of one producer, or a retry racing its original -- can lose
    one write and land the other. `resolve`, asked about the *lost* write,
    answers `APPLIED`: the version moved and the token is the caller's own,
    and both of those facts were put there by the write that survived. Token
    and version together cannot say **which** artefact is standing; only the
    payload can. A recovery that stopped at the token would hand the caller a
    `Publication` naming the lost artefact as current -- wrong in the one field
    a reader acts on.

    The interleaving is arranged with a gate at the store's `read`, which is
    the first thing the ambiguous path does: the lost advance is parked on its
    way into `resolve`, the surviving advance runs to completion, and only
    then is the resolver allowed to look.

    The trap itself is asserted, not assumed: after the run, the shipped
    remedy is asked the same question at the same coordinates and must still
    say `APPLIED` while the standing payload is the other artefact. If it ever
    stops saying so, this test is no longer constructing the case the check
    exists for, and is asserting nothing.
    """
    gate = Gate()
    inner = type(
        "ReadParkingMemoryStore",
        (MemoryStore,),
        {
            "read": lambda self, key: (
                gate.arrive(),
                MemoryStore.read(self, key),
            )[1]
        },
    )()
    store = FaultInjector(inner)
    topic = "control/self-race"
    seeded = publish(store, topic, "sha256:aaa", "producer", generation=UNPUBLISHED)
    assert seeded is not None
    outcome: dict[str, Publication | None] = {}

    def lost_advance() -> None:
        # The write is lost before the store sees it, so this call goes down
        # the ambiguous path and into `resolve`, whose first read parks here.
        with store.ambiguity(applied=False):
            outcome["lost"] = publish(
                store, topic, "sha256:bbb", "producer", generation=seeded.generation
            )

    racer = threading.Thread(target=lost_advance, name="racing-self")
    gate.park_thread(racer)
    racer.start()
    gate.wait_until_parked()

    # The same publisher identity, from the same generation, landing while the
    # lost advance has not yet looked. This is the write whose evidence the
    # resolver is about to read.
    survived = publish(
        store, topic, "sha256:ccc", "producer", generation=seeded.generation
    )
    assert survived is not None

    gate.release()
    racer.join(timeout=30.0)
    assert not racer.is_alive(), "the parked resolver never resumed"

    assert outcome["lost"] is None, (
        "a lost advance was reported as applied on the evidence of a different "
        "write, so the publisher believes readers see an artefact the store "
        "never held -- the artefact check in the recovery has stopped reaching"
    )

    # The trap, demonstrated rather than described: the shipped remedy still
    # attributes the surviving write to the lost one, because token and
    # version are all it consults -- which is exactly why the recovery cannot
    # stop there.
    verdict = resolve(store, key_for(topic), "producer", seeded.generation)
    assert verdict is Resolution.APPLIED, (
        "resolve no longer misattributes the surviving write, so this test is "
        "not constructing the case the artefact check exists for"
    )
    standing = current(store, topic)
    assert standing is not None and standing.artefact == "sha256:ccc"


# -- under load --------------------------------------------------------------


def _duplicate_advances(store, topic: str) -> tuple[int, int]:
    """Contend for one topic; count applied advances and duplicated fences.

    Each publisher loops: read the current generation, attempt to advance from
    it. Under a correct store every applied advance spent a generation no other
    applied advance spent -- two publishers advancing from the same generation
    is exactly the lost update the fence exists to refuse, and it is what the
    gate below arranges deterministically.

    A probe, not the verdict: for `postgres` there is no seam to park in, so
    this is the only instrument there is, and a run that finds nothing has not
    shown there was nothing to find.
    """
    seeded = publish(store, topic, "sha256:genesis", "genesis", generation=UNPUBLISHED)
    assert seeded is not None

    guard = threading.Lock()
    spent: list[int] = []
    errors: list[BaseException] = []

    def publisher(number: int) -> None:
        try:
            for attempt in range(ATTEMPTS):
                seen = current(store, topic)
                assert seen is not None, "a published topic read as nothing"
                made = publish(
                    store,
                    topic,
                    f"sha256:{number}-{attempt}",
                    f"publisher-{number}",
                    generation=seen.generation,
                )
                if made is None:
                    continue
                with guard:
                    spent.append(seen.generation)
        except BaseException as exc:  # noqa: BLE001 -- re-raised on the main thread
            with guard:
                errors.append(exc)

    threads = [
        threading.Thread(target=publisher, args=(n,), name=f"publisher-{n}")
        for n in range(PUBLISHERS)
    ]
    with preemptive():
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120.0)

    assert not any(t.is_alive() for t in threads), "a publisher never finished"
    if errors:
        raise errors[0]

    final = current(store, topic)
    assert final is not None
    # This equality leans on a HARNESS OBSERVATION, not a contract guarantee:
    # conformance pins versions as monotonic, never-reused, with a
    # deterministic first -- it does not pin the step size. Every registered
    # backend happens to advance by exactly one, so the suite passes; a
    # conforming non-unit backend fails HERE first, and that failure is this
    # lean showing, not a store defect. The equality cannot be weakened without
    # losing the ledger meaning (monotonic-only versions cannot count writes);
    # if a non-unit backend ever registers, re-derive the expectation from the
    # versions the applied publishes actually returned instead of arithmetic.
    assert final.generation == seeded.generation + len(spent), (
        "the topic's generation and the count of applied advances disagree, so "
        "either a refused advance wrote or an applied one did not"
    )
    return len(spent), len(spent) - len(set(spent))


def test_no_publication_is_silently_destroyed_under_load(
    correct: BackendUnderTest,
) -> None:
    """The lost update, at the pointer.

    Two applied advances from one generation means the second overwrote a
    publication it had never seen -- readers may have acted on a pointer that
    then vanished without being superseded by anything that knew about it.
    """
    applied, duplicates = _duplicate_advances(correct.store, correct.key())
    assert applied > 0, "no advance applied at all, so nothing here was measured"
    assert duplicates == 0, (
        f"{duplicates} advance(s) applied from a generation another applied "
        f"advance had already spent, and every publisher involved was told its "
        f"artefact was current"
    )


def test_a_non_atomic_store_advances_twice_from_one_generation() -> None:
    """The negative control, and a **gate rather than a probe.**

    `naive` reads and then writes with nothing joining the two, and this parks
    one publisher inside that gap, runs the other to completion, then lets the
    first finish. Both advanced from the same generation; both are told their
    artefact is current; the store holds one of them. The other was destroyed
    by a publisher that never saw it -- which is `_duplicate_advances`' count,
    arranged rather than waited for, and the test asserts the interleaving
    actually happened.

    **Proven to go quiet as well as to fire**, because a control observed only
    to fire has not been shown to discriminate: the sibling assertion at the
    end runs the identical interleaving against a correct store, which refuses
    the parked publisher.
    """
    gate = Gate()
    store = type(
        "ParkingNaiveStore", (NaiveStore,), {"_staged": lambda self: gate.arrive()}
    )()
    topic = "control/topic"
    seeded = publish(store, topic, "sha256:genesis", "genesis", generation=UNPUBLISHED)
    assert seeded is not None
    outcomes: dict[str, Publication | None] = {}

    def advance(name: str) -> None:
        outcomes[name] = publish(
            store, topic, f"sha256:{name}", name, generation=seeded.generation
        )

    first = threading.Thread(target=advance, args=("publisher-a",), name="publisher-a")
    second = threading.Thread(target=advance, args=("publisher-b",), name="publisher-b")

    gate.park_thread(first)
    first.start()
    gate.wait_until_parked()

    second.start()
    second.join(timeout=30.0)
    assert not second.is_alive(), (
        "the second publisher did not finish while the first was parked, so the "
        "interleaving under test never happened and this run decided nothing"
    )
    assert gate.arrivals_by_thread.get("publisher-b", 0) >= 1, (
        f"the second publisher never reached the store's seam: "
        f"{gate.arrivals_by_thread}"
    )

    gate.release()
    first.join(timeout=30.0)
    assert not first.is_alive(), "the parked publisher never resumed"

    told_current = [name for name, made in outcomes.items() if made is not None]
    assert len(told_current) == 2, (
        f"a non-atomic store applied {len(told_current)} advance(s) from one "
        f"generation, so the window this control exists to open was not opened "
        f"and the load check above is not shown to catch anything: {outcomes}"
    )
    standing = current(store, topic)
    assert standing is not None
    destroyed = [name for name in told_current if name != standing.publisher]
    assert len(destroyed) == 1, (
        "both publications somehow survived, which one key cannot do"
    )

    # And the same interleaving against a correct store: the parked publisher
    # resumes into a refusal, not an overwrite.
    quiet_gate = Gate()
    correct_store = type(
        "ParkingMemoryStore",
        (MemoryStore,),
        {"_staged": lambda self: quiet_gate.arrive()},
    )()
    seeded = publish(
        correct_store, topic, "sha256:genesis", "genesis", generation=UNPUBLISHED
    )
    assert seeded is not None
    outcomes.clear()

    def advance_correct(name: str) -> None:
        outcomes[name] = publish(
            correct_store, topic, f"sha256:{name}", name, generation=seeded.generation
        )

    first = threading.Thread(
        target=advance_correct, args=("publisher-a",), name="publisher-a"
    )
    second = threading.Thread(
        target=advance_correct, args=("publisher-b",), name="publisher-b"
    )
    quiet_gate.park_thread(first)
    first.start()
    quiet_gate.wait_until_parked()
    second.start()
    second.join(timeout=30.0)
    assert not second.is_alive()
    quiet_gate.release()
    first.join(timeout=30.0)
    assert not first.is_alive()

    told_current = [name for name, made in outcomes.items() if made is not None]
    assert told_current == ["publisher-b"], (
        f"the identical interleaving against a correct store applied "
        f"{told_current}, so the control above is failing every comer rather "
        f"than discriminating: {outcomes}"
    )
