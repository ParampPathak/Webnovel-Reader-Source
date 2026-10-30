# Listening checkpoint and Stop Listening contract

Native audio is owned by AVQueuePlayer. The reader page observes it; passive page
startup, WebKit reload and foreground restore must not issue a new Listen command.
The bridge consumes old `listen`/`section` launch parameters and uses a native
passive-selection guard only when no native session exists. Explicit Listen from
Here keeps its chapter/text-anchor command and ordinary Play keeps resume semantics.

Foreground and lifecycle saves reconcile the actual AVPlayerItem before saving.
Chunk/chapter changes persist immediately, as do pause, seek, rate and lifecycle
changes; ongoing playback persists at most five seconds apart. Old item completion
notifications require current item, chapter, asset and segment identity. Audible
current items are never replaced merely because a removed item's completion clock
is unavailable after coalesced notifications.

The journal is serialized, written to a private protected temporary file, flushed,
and atomically renamed before acknowledging local persistence. Ordered session,
generation and sequence provenance is retained offline. Acknowledgements advance
only matching owners and never replace positions; conflicts preserve both local
and server candidates. Flask's existing CAS/ordered-write contract remains in use.
Manual reading progress remains a separate endpoint and owner.

## Controls

- Pause retains the active session and resumable controls.
- Compact X / Stop Listening pauses immediately, saves the actual final checkpoint,
  cancels pending preparation/continuation, invalidates playback tokens, dismisses
  controls and clears Now Playing. Native also empties its queue and disables
  remote commands. It never waits for a server request before silencing audio.
- Collapse only changes expanded controls; it does not stop playback.
- Closing the book navigates; it does not stop narration.
- Return to narration navigates to the current live position.

The native journal clears activeBook on stop but retains the per-book checkpoint.
The browser retains a stopped local checkpoint. Passive restoration, old launch
URLs, delayed completion/buffering callbacks and remote Play cannot reactivate that
stopped session. A new explicit Listen can resume it. Offline final saves remain
locally durable and retry without reopening audio.

Both compact shell and in-reader X controls use this operation. The expanded
sheet has non-destructive backdrop/drag/handle dismissal; it has no visible Stop X.
See [player reliability](PLAYER-RELIABILITY.md) for the current expanded-sheet and
independent-reading contract. Previously shell X only
collapsed controls and in-reader X only hid the bar; the native stop path also
retained its queue. The corrected behavior changes these handlers, not the working
full-page reader, pagination, gestures, typography, imports or source settings.

## Isolated physical verification

A paired Mac can explicitly launch `--checkpoint-verification <192.168.x.x>` for
a synthetic verification run. This is never selected by an ordinary launch. It
uses a separate journal and audio cache, a localhost-only relay to fixed Mac port
18766, synthetic chapters 150–157, and a disposable Flask database. The production
HTTPS origin and real listening journal are not used. The release ATS exception
is limited to localhost for this opt-in relay; arbitrary HTTP origins remain
untrusted. `--checkpoint-resume` verifies stopped-state relaunch and explicit
resume. Reports stay in the app container and private recovery directory, outside
Git. Physical results must be recorded separately from simulator/unit results.

Fault controls exist only in the fixture: bounded delayed acknowledgements and
HTTP outages. The probe records actual native items, durable checkpoints, queue,
application lifecycle state, UI section and Now Playing cleanup. No production
book is used to manufacture a successful test.
