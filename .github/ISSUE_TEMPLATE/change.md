---
name: Change or chore
about: Propose work - a property, a backend, a refactor, a CI change
title: ""
---

<!--
You do not need any access to this repository to help here. It is public: fork
it, push a branch to your fork, and open a pull request. The pull request runs
the full test suite. Filing an issue first is welcome but not required.

A bare `#NNN` means this repository.

Do not pair a close/fix/resolve verb directly with an issue reference unless you
mean to close it, since that fires from an issue body too. Write "part of" or
"refs" instead. Break the adjacency rather than hunting for a gentler verb.
-->

## TL;DR

<!-- What should change, and why it is worth doing now rather than later. -->

## Current state

<!--
Verbatim where you can: the code that ships, the output, the error.
-->

## Proposed change

<!-- Concretely what you would do. A checklist is fine and usually better. -->

- [ ]
- [ ]

## How we would know it worked

<!--
The check, and how it could FAIL. A property added without an implementation
that violates it looks identical to a property whose violator silently passes,
so name the violating implementation, not only the passing one. A new backend
gets no tests of its own: it runs the existing suite (AGENTS.md).
-->

## What it costs

<!--
A dependency, a slower suite, a foreclosed option, work that now has to happen
in a particular order. "None" is a valid answer and an unusual one.

Two costs specific to this repository, worth checking before you propose:

- It is ASCII, and a test enforces that over every tracked file. A change
  introducing an em dash or an emoji fails CI rather than being tidied later.
- Its public claims have to be reproducible from a clean checkout. A
  measurement taken on your own deployment cannot ship as a claim, though the
  test that would take it can.
-->

## Explicitly out of scope

<!--
The adjacent thing you are NOT proposing, so the next reader does not assume it
was missed.
-->
