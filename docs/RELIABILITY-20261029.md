# Playback readiness, durable imports and reading inventory

This release extends the existing native player and shared Docker reader. It
preserves the full-page renderer, pagination, gestures, typography, highlighting,
SideStore identity and the existing causal checkpoint protections.

## Playback failures addressed

* **Read ahead, then Listen From Here:** native preparation treated an unavailable
  text-offset mapping as a terminal failure even when that location's audio was
  still being generated. It now waits with bounded backoff for the selected
  position, then refreshes the manifest if the mapped chunk is newer than the
  previously published prefix. It does not substitute another chapter or voice.
* **Ordinary resume during publication:** a partial manifest could expose chunk 0
  while the saved position required chunk 1. Native preparation rejected the saved
  chunk instead of waiting for it. Readiness now includes the required chunk.
* **Retry after failed preparation:** the last audible checkpoint is preserved,
  and a separate durable `pendingListen` records the failed requested target.
  Error-state Play retries that target. Explicit ordinary Resume uses the last
  valid listening checkpoint; it does not adopt a failed Listen From Here target.
  Stop clears the retry intent. Legacy journals decode without the new field.
* **Repeated waits with usable cached audio:** after a failed authoritative
  progress read, native startup still retried book, chapter and manifest metadata
  sequentially before falling back to their cached copies. A controlled test with
  three two-second metadata stalls took 6.46 seconds before the fix and 0.186
  seconds afterward. Once server unavailability is known, exact cached metadata
  and audio are used directly. Progress itself is never taken from an HTTP cache.
  Missing cached content remains a recoverable error at the intended target.
* **A chapter appended during playback:** the former last item's cached `next_id`
  remained empty, so the player could declare the book completed after the server
  had committed another chapter. The final boundary now rechecks chapter metadata
  once before declaring completion. That refresh neither seeks audio nor moves the
  manual reader. True book completion retains the final native checkpoint.

The historical minute-long private acceptance-test title incident was not retained in the
bounded diagnostic history. The causes above were reproduced in controlled tests;
they must not be presented as a complete reconstruction of that old incident.

## Checkpoint audit contract

The actual `AVPlayerItem`, validated asset, segment and observed media time remain
playback authority. Native state is sampled independently of WKWebView, with a
periodic durable save and immediate saves on meaningful transitions, Pause and
Stop. Journals use atomic replacement, file/directory synchronization and iOS
protection permitting access after the first unlock. Abrupt device or storage
failure can still lose the most recent unsaved interval; zero loss is not claimed.

The native journal retains book/section/asset, chunk offset, chapter time, rate,
voice, playback state, session identity, generation and event sequence. An immutable
in-flight event survives acknowledgement loss. SQLite accepts causal, idempotent
writes and preserves explicit rewinds. Old acknowledgements cannot replace a newer
native event; another owner's progress fences passive writes. Chapter numbers are
never used as a freshness clock. Failed preparation's retry intent is not an
observed listening checkpoint and cannot overwrite manual reading progress.

Existing stale-Listen-URL consumption, passive restoration guards, actual-item
foreground reconciliation, generation fences and delayed-callback protections are
retained. Stop silences first, cancels old work and clears active media controls;
it never disables a subsequent explicit Listen. Pause and sheet dismissal remain
separate actions. The compact X stops; the expanded sheet has neither X nor
Collapse and dismisses through the backdrop or downward handle gesture.

A refresh with no reader frame is also guarded before sending playback updates.

Private bounded transport diagnostics now distinguish checkpoint, chapter,
manifest, generation, text-offset, highlighting and audio delivery phases. They
record elapsed time, HTTP status, byte count, cache use and errors alongside queue
and waiting state, without chapter text, cookies or signing material. No-op full
queue fills no longer flood the event history. HTTPS origin and redirect checks
remain enforced; no timeout or trust protection was relaxed.

## Automatic import corrections

A paginated check could clear its explicit request after page one while retaining
a future scheduled deadline. The following page then waited until that deadline.
An in-progress catalog now remains immediately eligible for its next bounded step.
Pending imports survive worker recreation and resume their durable queue rather
than restarting discovery. Source revision checks and the existing process lock
continue to prevent obsolete or overlapping workers from appending chapters.

A new check could also display the previous run's `new_count`. Counts are reset for
a new run, and discovered/eligible, pending and committed import outcomes are kept
separate. `last_success` advances only after the requested import work completes.
Durable audit events record started, discovered, queued, importing, imported,
already imported, retrying, failed, skipped-with-reason and completed outcomes.
Import and completion events commit in the same transaction as their data changes.

An existing section ID alone no longer proves that a chapter is imported. Empty
metadata placeholders are filled in place before being counted as imported. Their
IDs and positions are retained; existing nonempty chapter text is never replaced.
The UI reports successful additions only for committed readable content.

### Hell Difficulty Tutorial evidence

A permitted live catalog check returned 237 chapters through chapter 909, matching
the imported readable count and tail. Chapters 907–909 were already persisted
before this release's investigation; they were not recovered by this work. No
verified missing eligible chapter was identified.

Retained logs show a failed check at **03:58:47 CDT on October 29** (`ValueError`).
The old logs did not retain its precise source response or discovery/import outcome,
so the reported 3 AM event cannot be fully reconstructed. The later stored status
reported blocked or rate-limited source access. The reproduced paginated-check bug
must not be asserted to be that single-page Royal Road check's exact cause. The
new audit records make future outcomes explicit. No source, alias, historical
chapter ID or listening/reading location is changed to work around source blocking.

## One reading-progress model

Manual reading progress uses equal weighting over the currently readable imported
sections:

`100 × (zero-based readable rank + fraction within that section) / readable count`

Sparse source positions, discovery-only rows and empty placeholders do not inflate
the denominator. Valid image-only EPUB content remains readable. Library, book
summary and reader indicators use the same inventory and rank. Appending content
can legitimately reduce the percentage while preserving the exact section,
fraction, anchor and checkpoint revision. Listening progress remains separate.

Content revisions, foreground refresh and bounded refresh while open update counts,
chapter lists and next/previous navigation after server imports. Native foreground
explicitly forwards a refresh to the shared view; it does not depend on background
WKWebView timers and never issues a playback command. Mutable JSON is not HTTP
cached; immutable audio delivery retains its existing cache policy.

## Book Content and page navigation

An EPUB's authored table of contents remains available, with resolved section IDs,
anchors and current-chapter indication. Books without an embedded TOC use the
ordered, searchable chapter list and bounded 100-row rendering. Reading a selected
chapter does not seek narration. No second reader or redundant contents UI was
introduced.

Real browser checks covered 1,003 sections, bounded Book Content rows, search and
selection, a five-page chapter, last-page-to-next-chapter and first-page-to-previous-
chapter-final-page navigation. The renderer and gestures were preserved. Slow-load,
rapid-input and typography protections remain covered by the reader regressions.

## Release verification

Shipping status, exact commit, Linux/CI results, IPA installation, physical iPhone
acceptance and preservation comparisons are recorded in [progress.md](progress.md).
Simulator results and a successful IPA build do not constitute physical acceptance.
Synthetic tests use separate databases, journals and audio storage. The working
manual same-ID SideStore fallback is retained; unattended scheduled renewal remains
unverified unless actual profile advancement and unattended execution are observed.
