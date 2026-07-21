# Release Notes

<!--
Two variants, pick the one that fits this repo's actual unit of change:

1. No version tags yet (pre-1.0, nothing published) — track by PR instead, same way
   AISF does it: one entry per merged PR against main, reverse chronological, each
   linking to its PR and (where one exists) to the doc that covers the change in full
   detail. Use "## PR #N — <summary>" headers.

2. Actual version tags exist — use "## vX.Y.Z - YYYY-MM-DD" headers instead, each
   linking to the PRs it shipped and a compare link to the previous tag. Add an
   "### Upgrade notes" subsection under any entry with a breaking change.

Either way, keep the tone AISF's file uses: bolded category tags inline in the
bullet (**Added:** / **Changed:** / **Fixed:**), not separate subheaders per
category — and state known limitations or deliberate scope cuts plainly instead of
leaving them implied.
-->

<One-line description of what this file tracks and how entries are ordered.>

---

## PR #N — <short imperative summary of what changed>
**YYYY-MM-DD** · [#N](<PR link>)

- **Added/Changed/Fixed:** <what changed, and why — not just the diff but the
  reasoning a reader would otherwise have to dig for>
- <Known limitation or deliberate scope cut, if any, stated plainly>
- <Test count, if applicable: "N new/updated unit tests; X passed, Y ignored.">

## PR #N-1 — ...
**YYYY-MM-DD** · [#N-1](<PR link>)

- ...
