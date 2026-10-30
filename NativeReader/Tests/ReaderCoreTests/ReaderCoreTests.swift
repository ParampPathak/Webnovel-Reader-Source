import XCTest
@testable import ReaderCore

final class ReaderCoreTests: XCTestCase {
    func point() -> Checkpoint { Checkpoint(book: "novel", section: 150, asset: "asset-150", remote: ServerProgress(), session: "native-session-1") }
    func testNativeChapter156SurvivesReopeningWhileManualReadingIs150() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: folder) }
        let url = folder.appendingPathComponent("listening.json"), store = try CheckpointStore(url: url)
        var p = point(); p.section_id = 156; p.asset_id = "asset-156"
        p.observe(seconds: 64.125, segment: 3, offset: 7.125, state: "playing", intent: "chapter-transition")
        try store.update { $0.activeBook = "novel"; $0.checkpoints["novel"] = p; $0.inFlight = p }
        let restored = try CheckpointStore(url: url)
        XCTAssertEqual(restored.journal.checkpoints["novel"]?.section_id, 156)
        XCTAssertEqual(restored.journal.checkpoints["novel"]?.segment_index, 3)
        XCTAssertEqual(restored.journal.checkpoints["novel"]?.segment_seconds, 7.125)
        XCTAssertEqual(restored.journal.inFlight, p)
        // This model has no field capable of taking ownership of manual reading.
        XCTAssertFalse(String(data: try Data(contentsOf: url), encoding: .utf8)!.contains("reading_progress"))
    }
    func testOfflineLocalPositionBeatsOlderAcknowledgedServerPosition() {
        var p = point(); p.claim_session = false; p.event_sequence = 90
        var remote = ServerProgress(); remote.session_id = p.session_id; remote.session_generation = p.session_generation
        remote.version = 20; remote.checkpoint_revision = 20; remote.event_sequence = 70; remote.section_id = 150
        p.section_id = 156
        XCTAssertEqual(CheckpointPolicy.reconcile(p, remote), .local)
        XCTAssertTrue(CheckpointPolicy.canRebase(p, on: remote))
    }
    func testExplicitBackwardSeekIsNewerLogicalEvent() {
        var p = point(); p.section_id = 156; p.event_sequence = 90
        p.observe(seconds: 2, segment: 0, offset: 2, state: "paused", intent: "seek")
        p.section_id = 150
        XCTAssertEqual(p.event_sequence, 91); XCTAssertEqual(p.checkpoint_intent, "seek"); XCTAssertEqual(p.seconds, 2)
    }
    func testAtomicJournalRejectsOlderSameSessionAndPreservesRewindWithNewSequence() throws {
        let url=FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer {try? FileManager.default.removeItem(at:url)}
        let store=try CheckpointStore(url:url);var current=point();current.event_sequence=50;current.section_id=156
        try store.update {$0.checkpoints["novel"]=current}
        var stale=current;stale.event_sequence=49;stale.section_id=151
        XCTAssertThrowsError(try store.update {$0.checkpoints["novel"]=stale})
        XCTAssertEqual(try CheckpointStore(url:url).journal.checkpoints["novel"]?.section_id,156)
        stale=current;stale.section_id=151
        XCTAssertThrowsError(try store.update {$0.checkpoints["novel"]=stale})
        var rewind=current;rewind.section_id=150;rewind.observe(seconds:2,segment:0,offset:2,state:"paused",intent:"seek")
        try store.update {$0.checkpoints["novel"]=rewind}
        XCTAssertEqual(try CheckpointStore(url:url).journal.checkpoints["novel"],rewind)
    }
    func testLegacyJournalDecodePreservesEntriesAndAllowsConflictCandidateRecovery() throws {
        var p=point();p.section_id=155
        let raw=try JSONSerialization.data(withJSONObject:["activeBook":"novel","checkpoints":["novel":JSONSerialization.jsonObject(with:JSONEncoder().encode(p))],"acknowledged":[:]])
        let journal=try JSONDecoder().decode(Journal.self,from:raw)
        XCTAssertEqual(journal.checkpoints["novel"],p);XCTAssertTrue(journal.conflicts.isEmpty)
        let url=FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer {try? FileManager.default.removeItem(at:url)}
        try raw.write(to:url);let store=try CheckpointStore(url:url)
        var remote=ServerProgress();remote.section_id=151;remote.session_id="different-owner"
        try store.update {$0.conflicts["novel"]=remote}
        let restored=try CheckpointStore(url:url)
        XCTAssertEqual(restored.journal.checkpoints["novel"]?.section_id,155)
        XCTAssertEqual(restored.journal.conflicts["novel"]?.section_id,151)
    }
    func testOtherOwnerFencesOfflineCheckpointRatherThanTakingOver() {
        let p = point(); var remote = ServerProgress()
        remote.session_id = "another-player"; remote.session_generation = 2; remote.version = 7
        XCTAssertEqual(CheckpointPolicy.reconcile(p, remote), .conflict)
        XCTAssertFalse(CheckpointPolicy.canRebase(p, on: remote))
    }
    func testUnacknowledgedInitialClaimCanBeRetried() {
        let p = point(); XCTAssertEqual(CheckpointPolicy.reconcile(p, ServerProgress()), .local)
    }
    func testLegacyWriteInvalidatesUnfencedOfflineClaim() {
        let p = point(); var remote = ServerProgress(); remote.version = 1
        XCTAssertEqual(CheckpointPolicy.reconcile(p, remote), .conflict)
    }
    func testNewerRemoteEventWinsEvenIfItsChapterIsLower() {
        var p = point(); p.claim_session = false; p.event_sequence = 10
        var remote = ServerProgress(); remote.session_id = p.session_id; remote.session_generation = p.session_generation
        remote.version = 3; remote.checkpoint_revision = 3; remote.event_sequence = 11; remote.section_id = 2
        XCTAssertEqual(CheckpointPolicy.reconcile(p, remote), .server)
    }
    func testLegacyRevisionCannotMasqueradeAsOwnedCheckpoint() {
        var p = point(); p.claim_session = false
        var remote = ServerProgress(); remote.session_id = p.session_id; remote.session_generation = p.session_generation
        remote.version = 5; remote.checkpoint_revision = 4
        XCTAssertEqual(CheckpointPolicy.reconcile(p, remote), .conflict)
    }
    func testOldSeekAndChapterCallbacksAreRejected() {
        var state = QueueCursor(); let before = state.replace(); let after = state.replace()
        XCTAssertFalse(state.accepts(before)); XCTAssertTrue(state.accepts(after))
    }
    func testRecoveryIsBoundedAndIntentionalPausePreventsIt() {
        var s = QueueCursor(); s.wanted = true
        XCTAssertTrue(s.retry()); XCTAssertTrue(s.retry()); XCTAssertTrue(s.retry()); XCTAssertFalse(s.retry())
        s.replace(); s.pause(interrupted: true); XCTAssertFalse(s.retry()); XCTAssertEqual(s.state, "interrupted")
    }
    func testPublishedChunksCannotBeReorderedOrReplaced() throws {
        let a = Segment(index: 0, duration: 5, url: "/a"), b = Segment(index: 1, duration: 4, url: "/b")
        let m = Manifest(status: "streaming", asset: "asset", segments: [a], complete: false)
        XCTAssertNoThrow(try Manifest(status: "ready", asset: "asset", segments: [a,b], complete: true).validated(after: m))
        XCTAssertThrowsError(try Manifest(status: "streaming", asset: "asset", segments: [b,a], complete: false).validated(after: m))
        XCTAssertThrowsError(try Manifest(status: "streaming", asset: "changed", segments: [a], complete: false).validated(after: m))
        XCTAssertThrowsError(try Manifest(status: "streaming", asset: "asset", segments: [], complete: false).validated(after: m))
    }
    func testTrustRejectsPublicURLsPortsUserInfoAndSchemeChanges() throws {
        let trust = try TrustedOrigin(URL(string: "https://reader.example.ts.net")!)
        XCTAssertTrue(trust.permits(try trust.resolve("/api/audio/asset")))
        for path in ["https://evil.example/track", "//evil.example/track", "http://reader.example.ts.net/", "https://reader.example.ts.net:8443/", "https://user:pass@reader.example.ts.net/"] { XCTAssertThrowsError(try trust.resolve(path)) }
        XCTAssertThrowsError(try TrustedOrigin(URL(string:"http://192.168.255.254:5000")!))
    }
    func testCorruptJournalIsNotSilentlyDiscarded() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: url) }
        try Data("corrupt".utf8).write(to: url)
        XCTAssertThrowsError(try CheckpointStore(url: url))
        XCTAssertEqual(try Data(contentsOf: url), Data("corrupt".utf8))
    }
    func testNonfiniteObservationDoesNotEraseDurableCursor() {
        var p = point(); let old = p
        p.observe(seconds: .nan, segment: 0, offset: 0, state: "playing", intent: "playback")
        XCTAssertEqual(p, old)
    }
    func testWordTimingUsesObservedPlaybackOffsetAndUTF16Mapping() throws {
        let raw = #"{"book_id":"novel","section_id":156,"segment_index":2,"quote":"Hello world","anchor":"narration:asset:2","fraction":0.5,"precision":"word","normalized_offset":200,"cues":[{"at":0,"offset":0,"quote":"Hello"},{"at":0.5,"offset":6,"quote":"world"}]}"#
        let location = try JSONDecoder().decode(Narration.self, from: Data(raw.utf8))
        XCTAssertEqual(location.location(at: 0.7).normalized_offset, 206)
        XCTAssertEqual(location.location(at: 0.7).anchor, "narration:asset:2:word:1")
        XCTAssertEqual(location.location(at: 0.2).quote, "Hello")
        XCTAssertEqual(location.location(at: 0.7).before, "Hello ")
        XCTAssertEqual(location.location(at: 0.2).after, " world")
        let unicode = raw.replacingOccurrences(of: "Hello world", with: "😀 Hi world").replacingOccurrences(of: #""offset":6"#, with: #""offset":6"#)
        let u = try JSONDecoder().decode(Narration.self, from: Data(unicode.utf8))
        XCTAssertEqual(u.location(at: 0.7).before, "😀 Hi ")
        XCTAssertEqual(u.location(at: 0.7).quote, "world")
    }
    func testPendingListenIntentRoundTripsSeparatelyFromAudibleCheckpoint() throws {
        var journal=Journal();let saved=point();journal.checkpoints[saved.book_id]=saved
        journal.pendingListen=ListenTarget(book:saved.book_id,section:saved.section_id+2,offset:90)
        let restored=try JSONDecoder().decode(Journal.self,from:JSONEncoder().encode(journal))
        XCTAssertEqual(restored.checkpoints[saved.book_id],saved)
        XCTAssertEqual(restored.pendingListen?.normalized_offset,90)
        let legacy=try JSONDecoder().decode(Journal.self,from:Data("{}".utf8));XCTAssertNil(legacy.pendingListen)
    }

}
