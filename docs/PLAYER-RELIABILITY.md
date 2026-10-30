# Native playback, independent reading and player-sheet contract

This extends the existing player and checkpoint protections; it does not replace
the native audio engine or the full-page reader.

## Three separate controls

| Action | Effect |
| --- | --- |
| Tap compact title/cover/bar outside the narrated reader | Return to the live book and highlighted narration without restarting audio |
| Compact chevron / in-book expand | Show the temporary player sheet |
| Dismiss backdrop, handle or downward drag | Close the sheet only; retain audio, queue, writer, current chapter and reader |
| Pause | Retain the active queue, writer and exact resumable location |
| Compact X / Stop Listening | Silence immediately, save the final actual point, cancel continuation and release active media controls |
| Back to Read Aloud | Explicitly navigate to actual narration; do not restart audio |
| Headphones / Listen from Here | Explicitly select the current reading book/chapter/text position |

The shared sheet controller contains no audio or checkpoint commands. It excludes
text scrolling, sliders and settings from dragging, releases the backdrop/inert
reader on dismissal, and hides both X and Collapse while expanded. Both native and
browser clients use it. The compact X remains Stop Listening.

Reading restores its own checkpoint. Player snapshots cannot re-enable following
after manual navigation. Natural narration transitions never change the reading
chapter. Word highlighting remains available in the matching chapter; only an
explicit narration return or explicit narration route crosses reading chapters.
The compact player and expanded text preview report actual narration, regardless
of the manually selected reading chapter or book.

## Confirmed native failure paths and corrections

1. An explicit new-book Listen could encounter a local checkpoint owned by an
   older writer and be rejected by the passive ownership-conflict path. Stop also
   retained the old causal writer on a subsequent same-book start. Explicit starts
   now establish a fresh writer from authoritative online server state. Passive
   restore and offline ownership safeguards remain intact. Preparation errors are
   reported to the initiating reader instead of appearing to succeed on the old book.
2. A failed item could be advanced out of AVQueuePlayer and the following audible
   item accepted as a valid coalesced transition, skipping the unheard chunk.
   Synchronous failure markers and item-status observations distinguish genuine
   failure from delayed successful observations; failed future suffixes retry in
   order and a failed current/predecessor recovers its saved point.
3. An exhausted queue could lose automatic continuation after a temporary future
   download failure. Continuation uses bounded 1/2/4-second retries independent of
   the player clock. Exhausting speculative prefetch does not stop valid queued
   audio; further actual transitions can retry. Exhausted unavailable audio ends
   in an actionable error at the last audible point. Stop and another-book Listen
   remain available.
4. Preparing a playable cached/downloaded audio item awaited highlighting metadata.
   A controlled eight-second timing delay blocked the next chunk. Audio preparation
   now uses cached timing immediately or fetches it separately with at most two
   workers. Immutable timing is applied only to matching active/queued assets.
   Missing timing cannot block audio or invent a checkpoint location.

Downloads coalesce by immutable URL with independent waiter cancellation. Obsolete
book requests cannot cancel another live waiter. Truncated WAV responses are
rejected; failed cached files are quarantined rather than silently reused or
removed. Valid cached audio is reused. The existing four-item audio queue bounds
prefetching; no whole-book download or bulk synthesis is introduced.

A native health task detects stalled/failed local playback even when its periodic
clock stops. Recovery is bounded, preserves the actual checkpoint, cancels old
continuations and fences their generation. Lock-screen commands older than a new
session cannot affect it. A stopped session can always be followed by a new
explicit Listen. Slow timing may delay an explicit voice-change mapping; it may
never reset that command to the beginning merely because an offset is unavailable.

## Diagnostics and acceptance

The native journal, ordered acknowledgements, CAS contract, stale-URL protections
and explicit rewind semantics remain in use. A bounded private diagnostics sidecar
records queue/status/waiting reason, generation, book/chunk and failure/retry events;
it contains no chapter text or signing material. Actual native position remains
authoritative. Presentation never becomes a second playback owner.

The disposable fixture supports three synthetic books (150–157, 160–167,
120–161), per-path delay/failure rules, outages and stale-owner scenarios. The
paired-device opt-in `--checkpoint-verification <private-Mac-IP>` uses separate
journals/cache. `--reliability-verification` adds multi-book, independent-reading,
sheet and locked-transition checks; `--interactive-fixture` leaves the same safe
content available for actual finger/lock-screen interaction. Ordinary launches
use the existing private HTTPS origin and real journal.

Record simulator, browser, real iPhone and production results separately. Neither
passing tests nor a successful IPA build proves physical installation or acceptance.
The release report is [progress.md](progress.md).
