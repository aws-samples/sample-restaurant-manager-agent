# Reference architecture — retired, see the repository README

> **This document was retired on 2026-08-08. Its content lives in the root
> [`README.md`](../../README.md), which is the Guidance artifact.**

## Why

Authored 2026-06-19 as a lean, pattern-first companion to the (then much shorter) README. The README
was later rebuilt to the AWS Solutions Library **Guidance** template — Overview, Architecture detail,
Prerequisites, Deployment, Validation, Running, Next Steps, Cleanup, FAQ, Notices, References — and in
doing so absorbed this document's material.

By retirement, **59% of this document's substantial paragraphs (32 of 54) were byte-identical to the
README**, nothing in the published surface linked here any more, and the duplication had started
causing real drift: on 2026-08-08 a single review pass had to apply the same edit twice in both files
three separate times (a dead `docs/diagrams.md` link, a superseded "MCP gateway" gloss, and the
correlation-strategy rename), and one instance still slipped because the two copies had diverged in
capitalization.

The two sections that were genuinely unique were folded into the README first:

- **Handling large tool results (the 128 KB limit)** → [README](../../README.md#handling-large-tool-results-the-128-kb-limit)
- **Applicability beyond restaurants** → [README](../../README.md#applicability-beyond-restaurants)

## Where things went

| Looking for | Now at |
|---|---|
| The pattern, principles, and end-to-end walkthrough | [`README.md`](../../README.md) § Architecture detail |
| Diagrams (sources + rendered) | [`diagrams/`](diagrams/) — unchanged, and referenced from the README |
| Large-payload chunking mechanics | [`large-payload-chunking-design.md`](large-payload-chunking-design.md) |
| The voice alternate pattern (Transcribe + Polly cascade, demoted to a fallback) | [`voice-interface-alternate-pattern.md`](voice-interface-alternate-pattern.md) |
| The voice + proactive-alerting decision proposal | [`voice-and-proactive-architecture-proposal.md`](voice-and-proactive-architecture-proposal.md) |

## Why a stub instead of a deletion

A number of dated design records still link here, and they are **point-in-time
records that are deliberately not rewritten**. Deleting this file would break every one of those links
to save one file. A stub keeps them resolving and tells the reader where the content went, and it
cannot drift from the README because it restates none of it.

Authoring record kept with the project's internal design notes.
