# Cross-client acceptance checklist

| Feature / contract | Shared implementation and automated coverage | Physical follow-up |
| --- | --- | --- |
| Manual reading independent from listening | reader JS, Flask progress/listening tests, Swift journal tests | Read separately while audio runs |
| Listen from visible chapter/text | explicit startReader, visible-anchor JS tests, native WKWebView/AVQueuePlayer fixture test | Select another chapter/page and Listen |
| Stop Listening / X | shared shell and reader handlers; browser late-work/offline/restore tests; native actual queue/journal/Now Playing tests | Verified 1.2(4): both actual X taps, locked playback and stopped relaunch |
| Passive WebKit remount | bridge playing/paused regressions and real WKWebView/AVQueuePlayer stale-URL test | Verified 1.2(4): locked chapters150–156, foreground and remount |
| Regular Play/Pause resume | browser player regressions, native bridge contract and integration tests | Pause/resume from library and lock screen |
| Mini-player live navigation | reader/player tests, native live navigation test preserves session/item/cursor | Tap library bar and verify live highlight |
| Back to Read Aloud | JS async/stale-section tests and native highlighting/navigation tests | Return after manual browsing |
| Chapter progression / checkpoint restoration | Flask native contract, Swift journal, native queue chapters150–156 | Lock and reopen; verify actual server checkpoint |
| Seeking, rate, voice, sleep | web player suite and native integration suite | ±15 and lock-screen controls |
| Full-page reading / typography / gestures | existing pagination/reader regressions; native full-text preservation test | Swipe and inspect page/highlight |
| Metadata/cover-only visibility | shared SQL real-text predicate, pending-import and worker regressions | Same library response on both clients |
| Partial/failure/retry/duplicates/restart | disposable database and source-worker tests | Pending importer status/retry UI |
| Source editing / persistence | shared page/API, revisions, tombstone, restart and two-client tests | Change each direction with a disposable test book |
| Cross-provider source replacement | shared importer discovery, expiring preview, ordered title/ID aliases, future-only confirmation; old source never fetched | Validate and save a replacement in the shared native/web source panel |
| Timeout / offline / simultaneous edit | bounded DNS/fetch/catalog checks, stale-worker tests, JS draft/conflict tests | Observe a failed save without losing the draft |
| Settings / bookmarks / preservation | existing HTTP/management/source tests preserve data | Confirm current settings after update |
| Native audio ownership / icon / trust | iOS integration, release plist/icon checks, trust/bridge tests | Actual device sound, icon, HTTPS connectivity |
| SideStore renewal | compare actual profile readback, not the UI counter | Manual and separately scheduled unattended runs |

Both clients share server pages/import/source state; platform media engines differ.
Browser background playback remains subject to its OS/browser lifecycle. iOS uses
AVQueuePlayer, AVAudioSession playback/spokenAudio, and native remote commands.
Word highlighting uses real timing when available and chunk anchors otherwise.
Record test platform and evidence for each release. Never mark a physical or
scheduled acceptance item complete based only on unit tests or simulator results.
