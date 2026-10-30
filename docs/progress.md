# October 30 reliability release

Runtime `dd9f41050975bef7b81054c7e6cdb98d0f7bd4b7` is committed and pushed to
main and codex/unified-native-reader. GitHub Actions Source checks run 39 passed
for that exact commit. See [fixes and limitations](RELIABILITY-20261029.md).

The guarded Docker release is healthy on that runtime. It verified the full
recovery backup, passed 287 isolated Linux tests (one skip), saved a fresh SQLite
snapshot and preserved all 37,936 preexisting media files. App and worker are healthy.
Compared table hashes match the fresh snapshot except reading/listening progress,
which advanced during the authorized user acceptance test. Books, sections, audio
references, bookmarks, preferences and source tables matched.

Native 1.4 (6) was built once, transferred with matching read-back SHA-256 and
installed in place through SideStore. Device query confirmed the original bundle
identifier and actual version/build. Nine checkpoints were exactly preserved
across installation. Fresh backup includes nine journal entries and 17 WebKit
files; SQLite quick checks passed. No uninstall or signing identity change occurred.

Final simulator suite: 40 tests, 39 passed, one physical-only skip, zero failures.
JavaScript: 248 comprehensive passes plus the final bridge regression (8 focused
bridge tests passed). Portable Swift: 18 passed. Real browser checks covered
1,003 chapters, bounded Book Content, search, five-page navigation and shared progress.
The unsupported macOS backend run was superseded by the passing Linux suite.

The user confirmed actual 1.4 (6) private acceptance-test title startup, ordinary Resume,
read-ahead Listen From Here, locked playback, lock-screen Pause/Play, foreground
highlight restoration and Library audio-bar return all passed. Final read-only
audit matched native/server section, asset, session and exact seconds for all nine
books. No checkpoint was reset or forcibly reconciled.

Appending a chapter during playback and genuine book completion passed native
integration tests; a new physical appended-chapter scenario was not separately
performed in this release. Naturally scheduled production import execution remains
to be observed; deterministic scheduler regressions passed. The historical minute
stall and 3 AM incident cannot be fully reconstructed from retained old logs.

SideStore unattended scheduled renewal remains **unverified**. The working manual
same-ID refresh fallback is preserved; no signing or renewal ownership was changed.

## Previous deployed release (historical)

# Deployed reliability release; startup-delay investigation remains open

Runtime `c0a7c73c6be9c9c750cf2b40bd539ca717dc6734` and native changes
`e37f698287390265904002e06f2c491dc0941d6a` are committed and pushed to
`codex/unified-native-reader`. Both jobs of [final CI run 40641874954](https://github.com/ParampPathak/Webnovel-Reader/actions/runs/40641874954)
passed. Final documentation and main-branch publication remain pending.

Docker deployment is healthy on the final runtime commit. The guarded release
verified the recovery backup, passed 277 isolated Linux tests (one skip), took a
fresh SQLite recovery snapshot and preserved all 33,561 preexisting media files.
All 13 compared table hashes match the fresh snapshot, including books, chapters,
audio references, sources, bookmarks, preferences and reading/listening progress.

## Installed iPhone and controls

SideStore installed **1.3 (5)** in place with the original bundle identifier.
The later shared UI correction requires no further IPA. The Library audio bar
returns to the live highlighted book page without restarting native audio.
The expanded sheet has neither Collapse nor X; backdrop and downward dragging
dismiss it while playback continues. The compact X intentionally remains Stop.

The actual installed app passed fixture-only automatic foreground checks for
natural narration 156→157 while manual reading remained at 160; manual reading
at 120; backdrop/drag dismissal; Library bar live return; headphones Listen from
120; compact Stop; and Stop→Book B. Real native item/session reports were retained.
Locked native samples record continuous progression through chapters 151–158,
including chapter 156, an eight-second save-ack delay and a ten-second outage.

The user additionally confirmed actual finger navigation, absence of expanded
Collapse/X, both dismiss gestures, 1.25× speed, locked playback and lock-screen
Pause/Play and ±15. These are user-observed checks, distinct from automated reports.
Local verification retained 244 comprehensive JavaScript passes, five final sheet
checks, 30 cumulative native simulator passes with one physical-only skip, and a
further focused native WKWebView navigation pass. No unchanged native release
component was rebuilt for the final shared UI correction.

## Preservation and unresolved verification

The fresh pre-install journal contained nine entries and exactly matched the prior
backup; reader local storage was backed up and its SQLite files checked. Eight
inactive positions remained identical after installation. The active book advanced
through normal user listening and adopted its newer server writer; the older
snapshot was retained without forcing either side backward. Synthetic checks use
separate journals, audio storage and backend. The final post-normal-launch native/
server comparison is pending access restoration and must not be claimed complete.

The user reported a real minute-long startup/buffering delay for the horror game
developer book, with matching native/web voice and speed while Docker played
immediately. This incident remains unresolved. Completed synthetic checks do not
prove it fixed. Native startup currently awaits metadata and downloads the selected
audio file before playback; request timings, selected asset/chunk, cache state and
TTS/generation status from the actual incident are still required. Do not change
voice selection, progress ownership or certificate trust based on a hypothesis.

Automatic approval review reached its usage limit before the final phone/server
commands. Further device diagnostics, final normal-mode/data verification and
repository publication require access to resume; this was not a safety rejection.
The user was instructed to force-quit the test process and reopen the Home Screen
app to restore normal production mode.

SideStore correct-ID manual installation/renewal remains the working fallback.
Scheduled unattended renewal and automatic VPN restoration remain **unverified**.
The Xcode renewal schedule was previously paused and has not been changed.

## Previous completed release (historical)


# Verified release — background checkpoints and Stop Listening

Runtime commit: `eeb2b6f70ccc32dd0c6164d776637f3f5cd17270`. Both jobs of
[CI run 40436934703](https://github.com/ParampPathak/Webnovel-Reader/actions/runs/40436934703)
passed. The implementation is documented in [Listening checkpoints](LISTENING-CHECKPOINTS.md).

The rollback was reproduced with a real WKWebView and AVQueuePlayer: passive reload
replayed an old Listen URL and replaced the later native item. Startup now consumes
that URL without commanding an existing native session. Saves reconcile the actual
item; ordered durable checkpoints and callback/session fences protect transitions.
X previously collapsed shell controls or hid reader controls, while native stop
retained its queue. Both X controls now stop and save immediately, invalidate pending
playback, dismiss the bar and clear the media session. Collapse remains separate.

## Actual iPhone verification

SideStore installed **Webnovel 1.2 (4)** in place, with the original bundle ID,
custom icon, private HTTPS backend and app data. No Xcode install, uninstall,
certificate revocation or signing migration was performed.

The isolated physical run used synthetic audio, a separate journal/cache and a
disposable backend. Native reports recorded the phone backgrounded through
chapters **150–156**, with an eight-second acknowledgement delay and ten-second
backend outage. Actual item, durable checkpoint and live reader matched after
foreground and stale-URL remount without restarting the player.

Both library and in-book X passed on the actual phone, including the user's finger
taps. Reports confirm an empty queue, playback rate zero, cleared Now Playing,
disabled remote Play and a durable stopped checkpoint. Stopped-state process
relaunch did not reopen audio; an explicit resume restored the same chapter/chunk
and played successfully. The final synthetic native/server checkpoint matched in
position, asset, session, generation and event sequence.

The app was returned to normal mode. All **six real-book positions**, audio
identities, playback settings and active book selection matched the pre-update
recovery journal. All six native positions also matched production server rows.
Reports and journals remain in ignored private recovery storage.

## Docker, browser and preservation

The guarded release deployed the tested image, updating only the Webnovel app and
platform worker. Health passed. All **30,253 inventoried published media files**
and table counts were preserved. A fresh consistent SQLite backup and rollback
image mapping were retained; the existing full recovery backup was verified.

Production hashes against the immediate pre-deployment SQLite backup matched for
books, sections, chapters, audio variants/chunk references, bookmarks, source
aliases, manual reading progress and listening positions. Normal paused foreground
saves may advance listening sequence/revision without moving the position.
Royal Road fiction `92144` remains the configured replacement source.

A real browser exercised a disposable container of the **exact deployed image**,
with synthetic data and no production mounts. Library X and reader X paused the
actual HTMLMediaElement, removed its source, hid controls and retained the current
route. Reload remained stopped; explicit Listen worked afterward. The temporary
container, fixtures and tunnel were stopped after verification.

## Tests and limits

- JavaScript: **237 passed**, plus **10 targeted final checks** after the final
  event fence. CI also passed the complete final source suite.
- Portable Swift: **17 passed**.
- Native simulator: **20 meaningful integration cases passed cumulatively**;
  one physical-only case was skipped. The navigation test's obsolete X-as-collapse
  expectation was updated to Collapse and passed on its targeted rerun. Xcode's
  post-test diagnostic collection stalled; the completed test logs were retained.
- Deployed Docker image: **277 backend tests, one skip**, otherwise successful.
- Physical iPhone: locked transitions, outage/delay recovery, remount, stop,
  relaunch/resume, native/server comparison and both actual X taps passed above.

The existing full-page reader, highlighting, gestures and typography were retained;
reader/navigation regressions passed. No production test book or synthetic audio
was added, and no real listening position was advanced for these tests.

**Scheduled unattended SideStore renewal remains unverified.** Correct-ID manual
installation/renewal is the working fallback; the Xcode renewal schedule remains
paused. This release does not claim to establish unattended renewal.

## Previous completed release

# Release verification — 2026-10-26

## Current release: independent source replacement

The removed-source failure was a backend constraint: saving required existing
chapter URLs to belong to the new provider before any discovery. A FreeWebNovel
import therefore could never accept Royal Road. The fix uses shared backend
discovery for imports, previews and updates, plus revision-bound previews and
non-destructive chapter aliases. Uncertain overlap requires explicit future-only
confirmation; the old source is never fetched. The shared server page supplies
both native and browser clients, so no Swift/IPA change is required.

Bounded live checks from Docker reached the legitimate Royal Road fiction
92144 index for The Legend of William Oh: 142 public chapters, 141 ordered
metadata matches to the existing import, including the imported tail. The extra
summary entry is skipped; no historical text is imported or overwritten. The
official RSS feed exposed only 10 recent items and is insufficient for overlap.

Deployed runtime commit: `6f0f0f1`. The committed image passed **277 backend
tests (one skip)**; all **227 JavaScript tests** passed. The final source CI
[run 40250991065](https://github.com/ParampPathak/Webnovel-Reader/actions/runs/40250991065)
passed both jobs. The existing stale-job fence checks the job's original source,
preserving pagination while refusing writes from replaced-source jobs.

Production is healthy on `webnovel-unified:6f0f0f1edad9`, contract 1/release
`20261025-source1`. The fresh recovery copy verified 27,298 files. Deployment
preserved 27,263 published media files and all inventoried table counts; only the
app/platform-worker services were recreated. The real web source panel validated
and saved Royal Road without restoring FreeWebNovel. The background scheduler
completed Check now against that source: 142 available, checkpoint 4235067,
zero new imports, no failures, Up to date.

Direct comparison with the recovery database and saved hashes confirms this
book's 337 sections, 311 audio variants, 9,194 audio-chunk references, bookmarks,
book metadata, manual reading checkpoint and listening checkpoint are unchanged.
After the physical phone reopen, the reading save revision/timestamp advanced
once; its section, fraction, anchor and typography stayed identical to the backup.
The listening row stayed identical. The favorite flag is unchanged; normal book
visits advanced opened_at. Earlier source-only comparisons matched all hashes.
141 alias rows preserve original section/audio identities across providers.
Auto-import has been restored at the existing 12-hour interval, using Royal Road;
the scheduler is running. The completed real background check found no new
chapters. Source verification never generated audio or wrote reading/listening
progress. The next scheduled check is a future event, not a claimed test run.

The user confirmed on the physical iPhone that the Royal Road URL appears,
validation/save works, and the setting survives closing/reopening Webnovel.
The server revision advanced from 2 to 3 after that phone save, confirming the
native-to-server path; the web-saved URL had already appeared on the phone.
The existing app loads this shared server page; no rebuild, reinstall or signing
change was needed. Fresh device inventory confirms Webnovel 1.1 (3) and guarded
SideStore local2. Scheduled unattended SideStore renewal remains separately
unverified; see [SideStore status](sidestore-status.md). Background Refresh is on,
but no installed or successfully executed Personal Automation has been verified.
Source follow-up private checkpoint:
`docs/SOURCE-REPLACEMENT-20261025.private.md` (excluded from Git).

## Historical release record — 2026-10-25

The following records the preceding release. The current Docker/source status is
above; its runtime supersedes the image listed in this historical record.

The existing native project is integrated into this repository without replacing
its AVQueuePlayer, icon, or full-page reader. Shared Listen, import visibility,
and source configuration fixes have regression coverage. Main and production
branch histories are merged without rewriting either branch.

Implemented: manual-position Listen with ordinary audiobook resume preserved;
content-backed library visibility with recoverable pending imports; shared,
revisioned source settings with bounded checking and stale-worker protection.

Validation completed:
- 223 JavaScript tests passed before the final race guard; the 72 affected reader
  and bridge tests then passed with that guard.
- 15 portable Swift core tests passed.
- 14 native simulator tests: 13 passed, one physical-only test skipped.
- The final active-audio Listen and failed-transition tests both passed again.
- Clean production-image Linux suite: 260 tests, OK with one skip.
- Final source GitHub Actions workflow passed, including backend/JavaScript,
  native core tests and unsigned iOS compile:
  https://github.com/ParampPathak/Webnovel-Reader/actions/runs/40216488377
  Four deterministic resource-guard tests were added after the production-image
  run; worker tests no longer depend on shared-runner host resource pressure.
- Full production recovery copy: 25,788 persistent files checksum-verified,
  SQLite integrity OK; only the worker's live notification socket is excluded.
- Tracked-file security checks exclude runtime data, books, audio, private config,
  profiles, signing keys, recovery material, and release packages.

Repository: runtime fixes are committed in b57cf71, deployment corrections in
ccabcb9, and deterministic test corrections in 8ac5bed. Histories are merged
without rewriting existing commits. This is the documentation-only completion
record; all runtime changes are already shipped.

Docker: production is healthy on webnovel-unified:ccabcb9324ee, built from a
clean Git archive. Later commits affect tests/docs only and need no runtime
rebuild. The private HTTPS compatibility endpoint returned contract 1/release
20261025-parity1 with TLS verification. The successful deployment receipt records
unchanged counts for every inventoried table and 26,839 published media files
preserved by size/mtime. A fresh online SQLite backup preceded migration.
Only app/platform-worker containers were recreated. An earlier preservation
guard exercised successful image rollback while retaining the current database.
Unfinished temporary sidecars are excluded from published-media inventory.

Physical iPhone: Webnovel 1.1 (3) is installed through a SideStore in-place update
with the original com.parampathak.WebnovelReader identifier and existing team.
Actual provisioning inventory shows a new correct-ID profile expiring
2026-11-01 17:22:24 UTC, rather than relying on the displayed counter.
All six native journal entries survived. Inactive entries retained playback
fields; the active entry advanced through normal user playback. The container
UUID changed, so preservation was verified from journal contents.

The user confirmed manual-position Listen/highlighting, Pause/Play resume, live
Library audio-bar navigation, locked playback, lock-screen Pause/Play and +/-15,
and reopen/resume. These are user-observed physical checks, separate from tests.

Read-only native/server comparison after physical acceptance matches book,
section, audio asset, segment, session, rate, voice and paused state. Timestamps
differ by approximately 0.00177 seconds; the server has two later revisions.
Manual reading position/preferences remain separately stored and the book API
opens successfully. Verification did not write production progress. Private
comparison evidence, IPA, checksums, backup and deployment receipts stay out of Git.

SideStore: actual correct-ID signing/manual renewal is verified.
Scheduled unattended renewal and safe automatic VPN restoration remain unverified.
The existing Xcode renewal schedule remains paused. Keep the exact-ID IPA reimport
fallback; do not infer unattended success from a green expiry counter.

For private device/recovery evidence and exact host-specific next commands, use
`docs/SESSION_CHECKPOINT.private.md` and the private development recovery folder.
Unattended renewal is the remaining limitation: it needs a controlled scheduled
phone-only test with actual profile-expiry advancement and safe VPN restoration.
No reinstall, certificate revocation or signing-owner replacement was performed
during completion verification. No runtime rebuild or repeated physical test is
required for this report.
