# One project, two clients

The Flask application, `static/`, and `templates/` keep their existing paths.
`NativeReader/` contains the existing Swift/Xcode application, its custom icon,
native audio adapter, portable checkpoint model, and synthetic integration tests.
The iOS reader loads these same server pages inside WKWebView. JavaScript media
playback is blocked there; AVQueuePlayer remains the only iOS audio owner.

## Change and verification procedure

For each change, identify the affected backend/API, shared web UI, native bridge,
native audio, persistence, and source-import components. Change shared behavior
once on the server; update the native adapter only where its contract changes.
Preserve the existing full-page renderer, pagination, gestures, and typography.

1. Add a practical failing regression for a reported bug.
2. Run `python -m unittest discover -s tests -t .` on **Linux**, with
   `START_TTS_WORKERS=0 AUDIO_CACHE_CLEANUP_ENABLED=0 CHAPTER_UPDATES_ENABLED=0`.
   ResourceGuard uses Linux `/proc`; a Mac failure there is not a product result.
   Use a disposable container with no production volume and `--network none`.
3. Run `node --test tests/js/*.test.cjs` and `swift test --package-path NativeReader`.
4. Start `python NativeReader/Tools/fixture_server.py` using the backend virtualenv.
   This creates a disposable EPUB, database, and synthesized tones on loopback.
   Run `xcodebuild test -project NativeReader/WebnovelReader.xcodeproj -scheme
   WebnovelReader -destination 'platform=iOS Simulator,name=YOUR_SIMULATOR'
   CODE_SIGNING_ALLOWED=NO ONLY_ACTIVE_ARCH=YES`.
5. Run `python deployment/check_tracked_files.py`, review the staged diff, commit,
   and push without rewriting shared history. Check the resulting Actions run.
6. Deploy only after critical tests and the recovery backup verify successfully.

CI runs the full Linux suite, JS tests, tracked-file checks, Docker build, portable
Swift tests, and an unsigned iOS compile. It has read-only GitHub permissions and
no Apple or production credentials. Physical lock-screen tests are separate;
simulator success does not prove hardware background behavior. Synthetic tones
verify queueing and decoding, not a speech provider's voice quality.

## Stable shared contracts

`GET /api/compatibility` reports API contract 1 and the supported capabilities.
The original native v1 remains supported. Additive JSON fields do not change the
listening checkpoint format. A future incompatible release must update the API
contract, both client tests, and deployment compatibility checks together.

Manual reading progress (`/progress`) and native/server listening (`/listening`)
are independent. `Player.startReader(book, position)` is an intentional command
with `section_id` and normalized UTF-16 text offset. Regular Play/Pause never
supplies a manual offset. Existing chunk/text mapping chooses the nearest actual
published anchor; no exact timing is fabricated. Native preparation and seek must
succeed before the candidate listening point enters the durable journal.

The library requires at least one persisted section with nonblank `text_content`.
Metadata, cover, discovery, empty or failed imports remain recoverable through
`/api/books?pending=1`, the importer, job diagnostics, and direct book detail.
Partial downloads remain visible after their first real chapter. No book, audio,
checkpoint, or failed-job diagnostic is deleted by this visibility rule.

Source configuration is server-authoritative. GET/PUT `/api/books/ID/chapter-updates`
returns `config_revision`; clients send it on save. A stale save returns HTTP409
and preserves the saved configuration. Older clients may omit the revision for
compatibility and receive serialized last-write-wins behavior. The shared book UI
sends revisions on both web and iOS. A blank `source_url` explicitly removes the
online source and disables updates, retaining existing chapters and progress.
The tombstone prevents automatic rediscovery of a deliberately removed source.
Both source tables are updated in one transaction. In-flight worker callbacks
must match the saved revision before writing anything.

Source replacement uses POST `/api/books/ID/chapter-updates/preview` followed by
the existing PUT with `preview_id` and `config_revision`. The preview independently
discovers the NEW public index through `discovery.py`, the same backend service
used by initial import browsing, durable discovery, artwork metadata, and chapter
updates. No request to the old source is required. Provider parsing stays in
`scraper.py`/`source_adapters.py`; neither client implements its own detector.

The complete preview has a 60-second transport/catalog budget (75-second client
abort), page/link limits, useful errors, and a 15-minute expiry. Its server-held
token is bound to book, source, configuration revision and existing chapter
metadata. Save rejects stale previews. A confirmed index and chapter overlap
are required for a verified replacement; HTTP 200/title alone is insufficient.

Matching uses stable provider IDs or unique normalized chapter titles in
monotonic existing order, with book metadata and the imported tail establishing
confidence. Chapter numbers alone never establish equivalence. Alias rows point
new provider IDs to existing section IDs; href/text/audio/progress stay unchanged.
Both normal discovery and scheduled updates respect these aliases. If overlap is
ambiguous, the user can explicitly confirm a **future-only baseline after the
latest currently listed chapter**. No uncertain mapping or historical backfill is
applied. Older same-provider clients retain their stable-identity save path, which
does not claim a live check. Auto-update providers remain public FreeWebNovel and
Royal Road; the other manual importer adapters are preserved. Robots rules, TLS
validation, DNS pinning, private-address blocking and source policies remain enforced.

Royal Road's official syndication feed exposes recent releases only (10 entries
in the bounded live check); it cannot establish complete historical overlap.
The public fiction table remains the shared discovery source. Stubbed/removed
historical chapters are never fetched through alternate access or inferred as
new content. A changing catalog that loses the saved tail stops safely for review.

Network bodies and redirects share a 60-second fetch budget; DNS and the polite
source-request queue have bounded waits. Complete catalog walks have a 15-minute
budget and retain page/chapter limits. Expired Checking states become actionable
errors, including after restart. Import failures retain their durable work list;
three consecutive failures end that attempt with backoff. Source UI requests time
out, preserve drafts, and distinguish saved settings from failed validation.

## Docker release and rollback

Keep real Compose files, data, release receipts, and backups out of Git. Use the
existing project, mounts, resource limits, network, and Compose project name.
Create a **new full recovery directory**, run `tools/backup_data.py`, and verify
every checksum. A live recovery copy uses SQLite's backup API; never copy a live
database without its WAL or overwrite a newer database with an old file.

From the Mac, with existing SSH authentication:

```sh
python deployment/publish.py --host USER@EXISTING_SERVER \
  --root /path/to/existing/project --verified-backup /backups/VERIFIED_COPY \
  --compose compose.yaml --compose compose.release.yaml
```

This transfers committed history as a Git bundle, checks ancestry and a clean
checkout, verifies the full backup, builds the image, reruns isolated Linux tests,
and creates a fresh verified SQLite recovery point. Only `webnovel-app` and
`webnovel-platform-worker` are recreated. The existing Piper worker and unrelated
containers are left running. Health, shared contract, library, row counts, and
existing media file metadata are checked. The private release receipt records
previous images and an exact rollback command. Failure switches those two images
back and **retains the current database and media**. No destructive schema change
is supported by this procedure. Private override/receipt directories must remain
available to reproduce or roll back that release.

## Native release through SideStore

Build with the existing Personal Team and private HTTPS origin supplied locally:

```sh
python NativeReader/Tools/build_release.py --backend-url https://YOUR_PRIVATE_ORIGIN \
  --team YOUR_EXISTING_TEAM --output /PRIVATE/OUTSIDE/REPOSITORY \
  --version 1.1 --build 2
```

The tool performs an unsigned Release build, verifies the original bundle ID,
background audio, icon, and backend origin, and packages a plain ZIP IPA without
AppleDouble sidecars, provisioning profiles, or private keys. SideStore owns the
actual signing/install. No Xcode account change, certificate renewal, device
installation, or uninstall occurs in this command. The origin is supplied through
the `NATIVE_BACKEND_URL` build setting; personal deployment configuration stays
outside Git. The original identifier is `com.parampathak.WebnovelReader`.

For an update, back up the app's recoverable journal/settings first. Import the
IPA in the verified SideStore installation, keep the exact original identifier,
and leave **Append Team ID off for Webnovel**. Cancel uninstall, deactivation,
changed-team, or certificate-revocation prompts. Reconnect the private VPN before
opening the reader. Record the installed version/build, actual identity/profile,
and data container; an IPA build alone does not prove phone installation.

## SideStore renewal acceptance

The installed guarded custom SideStore version has a correct-ID normal refresh
readback guard. Review its private source patch and recovery records before any
future SideStore replacement. A green expiry bar alone is insufficient: compare
the actual installed profile identifier, team, UUID, and expiration before/after.
Keep the verified exact-ID IPA re-import fallback and the old Xcode renewal job
paused while SideStore owns signing.

Phone-only unattended status requires a real scheduled run with the phone locked
and USB disconnected, receipt/profile readback, successful launch/data retention,
and restored private-network connectivity. Manual refresh and a configured iOS
background switch do not prove this. LocalDevVPN and Tailscale compete for the
active iOS tunnel: any Shortcut must capture prior connectivity, defer active
audio, use bounded operations, always stop LocalDevVPN, restore prior Tailscale,
and handle OS cancellation with independent cleanup. Do not promise unattended
renewal or silently enable an unverified VPN-switching automation.
