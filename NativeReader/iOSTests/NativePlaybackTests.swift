import XCTest
import AVFoundation
import MediaPlayer
import WebKit
import Network
import CryptoKit
@testable import WebnovelReader

@MainActor
final class NativePlaybackTests: XCTestCase {
    struct FixtureBook:Decodable {let book_id:String;let sections:[Int]}
    struct Fixture: Decodable { let book_id: String; let sections: [Int]; let books:[FixtureBook] }
    struct Reading: Decodable, Equatable { let section_id: Int?; let version: Int; let fraction: Double; let anchor: String }
    var backend: Backend!
    var fixture: Fixture!
    var folder: URL!
    var services: [PlaybackService] = []
    override func setUp() async throws {
        #if !targetEnvironment(simulator)
        guard let host = ProcessInfo.processInfo.environment["WEBNOVEL_FIXTURE_HOST"] else { throw XCTSkip("Physical tests require an isolated fixture host.") }
        try await PhoneFixtureRelay.shared.start(host: host)
        #endif
        backend = Backend(origin: try TrustedOrigin(URL(string:"http://localhost:18765")!, allowLoopback:true))
        let _: IgnoredResponse = try await backend.request("/fixture/reset", method:"POST", body: Data("{}".utf8))
        fixture = try await backend.request("/fixture/info")
        folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories:true)
    }
    override func tearDown() async throws {
        for service in services { service.pause(); service.sync.task?.cancel(); service.commandTask?.cancel() }
        services.removeAll(); if let folder { try? FileManager.default.removeItem(at: folder) }
    }
    func service(store: CheckpointStore? = nil) throws -> PlaybackService {
        let service = try PlaybackService(backend:backend, store:store ?? CheckpointStore(url:folder.appendingPathComponent("journal.json")), cache:folder.appendingPathComponent("audio"))
        services.append(service);return service
    }
    func wait(_ condition: @escaping () -> Bool, timeout: Double = 15) async throws {
        let end = Date().addingTimeInterval(timeout)
        while !condition() && Date() < end { try await Task.sleep(for:.milliseconds(100)) }
        if !condition() { XCTFail("Condition timed out"); throw PlaybackError.unavailable }
    }
    func testReadAheadListenWaitsForItsRequestedTextOffsetToBecomeAvailable() async throws {
        let s=try service(),book=fixture.books[0]
        await s.open(book.book_id,section:book.sections[0],autoplay:false);s.stopListening();await s.sync.task?.value
        let target=book.sections[4],p=Checkpoint(book:book.book_id,section:target,asset:"",remote:ServerProgress())
        let manifest=try await backend.manifest(p,section:target),asset=try XCTUnwrap(manifest.asset_id)
        let pending:[String:Any]=["available":false,"seconds":0,"segment_index":0,"segment_seconds":0]
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:try JSONSerialization.data(withJSONObject:["rules":[["path":"/api/audio/\(asset)/start","remaining":2,"json":pending]]]))
        await s.open(book.book_id,section:target,autoplay:true,textOffset:50)
        XCTAssertNotEqual(s.cursor.state,"error",s.status);XCTAssertEqual(s.point?.section_id,target)
        try await wait({s.isPlaying})
    }
    func testOrdinaryStoppedResumeWaitsForItsExistingChunkInsteadOfRejectingPartialManifest() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[4],autoplay:false);await s.seek(to:1.7)
        s.stopListening();await s.sync.task?.value
        let point=try XCTUnwrap(s.point),manifest=try await backend.manifest(point,section:point.section_id)
        let partial=Manifest(status:"streaming",asset:try XCTUnwrap(manifest.asset_id),segments:Array((manifest.segments ?? []).prefix(1)),complete:false)
        let body=try JSONSerialization.jsonObject(with:JSONEncoder().encode(partial))
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:try JSONSerialization.data(withJSONObject:["rules":[["path":"/api/books/\(fixture.book_id)/audio","remaining":1,"json":body]]]))
        await s.open(fixture.book_id,section:nil,autoplay:true)
        XCTAssertNotEqual(s.cursor.state,"error",s.status);XCTAssertEqual(s.point?.section_id,point.section_id)
        XCTAssertEqual(s.point?.segment_index,point.segment_index);XCTAssertEqual(s.point?.seconds ?? -1,point.seconds,accuracy:0.2)
        try await wait({s.isPlaying})
    }
    func testFailedReaderStartRetriesTheSelectedTargetAndStopClearsThatIntent() async throws {
        let s=try service(),book=fixture.books[0]
        await s.open(book.book_id,section:book.sections[0],autoplay:false);s.stopListening();await s.sync.task?.value
        let before=s.point,target=book.sections[3]
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:try JSONSerialization.data(withJSONObject:["rules":[["path":"/api/books/\(book.book_id)/sections/\(target)","remaining":1,"status":404]]]))
        await s.open(book.book_id,section:target,autoplay:true,textOffset:30)
        XCTAssertEqual(s.cursor.state,"error");XCTAssertEqual(s.point?.section_id,before?.section_id)
        XCTAssertEqual(s.store.journal.pendingListen?.section_id,target)
        s.play();await s.commandTask?.value;try await wait({s.isPlaying})
        XCTAssertEqual(s.point?.section_id,target);XCTAssertNil(s.store.journal.pendingListen)
        s.stopListening();XCTAssertNil(s.store.journal.pendingListen)
    }
    func testCachedOfflineResumeAndTransportDiagnosticsRetainTheActualChunk() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[3],autoplay:false)
        await s.seek(to:0.4);s.stopListening();await s.sync.task?.value
        let saved=try XCTUnwrap(s.point)
        XCTAssertTrue(s.events.contains {($0["phase"] as? String)=="audio-download"&&($0["ready"] as? Bool)==true&&($0["bytes"] as? Int ?? 0)>0})
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:Data("{\"outage_seconds\":10}".utf8))
        let began=Date();await s.open(fixture.book_id,section:nil,autoplay:true)
        XCTAssertNotEqual(s.cursor.state,"error",s.status);try await wait({s.isPlaying},timeout:4)
        XCTAssertLessThan(Date().timeIntervalSince(began),4)
        XCTAssertEqual(s.point?.section_id,saved.section_id);XCTAssertEqual(s.point?.segment_index,saved.segment_index)
        XCTAssertTrue(s.events.contains {($0["phase"] as? String)=="audio-cache"&&($0["cache_hit"] as? Bool)==true})
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:Data("{\"outage_seconds\":0}".utf8))
        s.pause();await s.sync.task?.value
    }
    func testFailedReadingTargetSurvivesRelaunchWithoutAutoplayingOrReplacingTheAudibleCheckpoint() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[0],autoplay:false)
        s.stopListening();await s.sync.task?.value
        let target=fixture.sections[4],old=s.point
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:try JSONSerialization.data(withJSONObject:["rules":[["path":"/api/books/\(fixture.book_id)/sections/\(target)","remaining":1,"status":404]]]))
        await s.open(fixture.book_id,section:target,autoplay:true,textOffset:20)
        let restored=try service(store:CheckpointStore(url:s.store.url));await restored.restore()
        XCTAssertNil(restored.player.currentItem);XCTAssertFalse(restored.cursor.wanted)
        XCTAssertEqual(restored.store.journal.checkpoints[fixture.book_id]?.section_id,old?.section_id)
        restored.play();await restored.commandTask?.value;try await wait({restored.isPlaying})
        XCTAssertEqual(restored.point?.section_id,target);XCTAssertNil(restored.store.journal.pendingListen)
        restored.stopListening()
    }
    func testKnownOfflineStartupDoesNotSeriallyRetryCachedBookChapterAndManifest() async throws {
        let s=try service(),id=fixture.book_id,section=fixture.sections[3]
        await s.open(id,section:section,autoplay:false);s.stopListening();await s.sync.task?.value
        let rules:[[String:Any]]=[
            ["path":"/api/books/\(id)/listening","remaining":1,"status":503],
            ["path":"/api/books/\(id)","remaining":1,"delay":2],
            ["path":"/api/books/\(id)/sections/\(section)","remaining":1,"delay":2],
            ["path":"/api/books/\(id)/audio","remaining":1,"delay":2]]
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:try JSONSerialization.data(withJSONObject:["rules":rules]))
        let began=Date();await s.open(id,section:nil,autoplay:true)
        XCTAssertNotEqual(s.cursor.state,"error",s.status);XCTAssertLessThan(Date().timeIntervalSince(began),3,"Known offline startup serially waited on metadata that is already cached")
        s.stopListening()
    }
    func testBookContentSelectsResolvedChapterWithoutReplacingPausedNativeAudio() async throws {
        let s=try service(),c=ReaderController(service:s)
        let scene=try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene),window=UIWindow(windowScene:scene),host=UIViewController()
        window.rootViewController=host;window.makeKeyAndVisible();c.webView.frame=host.view.bounds;host.view.addSubview(c.webView);c.load()
        defer {window.isHidden=true}
        func js(_ value:String) async throws -> Any {try await c.webView.evaluateJavaScript(value)}
        func ready(_ condition:String) async throws {
            for _ in 0..<150 {if (try? await js(condition)) as? Bool==true {return};try await Task.sleep(for:.milliseconds(100))}
            XCTFail(condition);throw PlaybackError.unavailable
        }
        try await ready("!!window.Player")
        await s.open(fixture.book_id,section:fixture.sections[0],autoplay:false);await s.seek(to:0.6);c.publish()
        let item=s.player.currentItem,owner=s.point?.session_id,epoch=s.cursor.generation
        _=try await js("document.getElementById('miniOpen').click()")
        try await ready("document.getElementById('workspace').contentWindow.ReaderNarration?.page()?.ready===true")
        _=try await js("(()=>{const d=document.getElementById('workspace').contentDocument;d.getElementById('contentsToggle').click();d.getElementById('tocTab').click();return true})()")
        try await ready("!!document.getElementById('workspace').contentDocument.querySelector('#sectionList button[aria-current]')")
        _=try await js("document.getElementById('workspace').contentDocument.querySelectorAll('#sectionList button')[4].click()")
        try await ready("document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id===\(fixture.sections[4]) && document.getElementById('workspace').contentWindow.ReaderNarration.page().ready")
        c.sceneActive=false;c.sceneActive=true;try await Task.sleep(for:.milliseconds(300))
        XCTAssertTrue(s.player.currentItem===item);XCTAssertEqual(s.point?.session_id,owner);XCTAssertEqual(s.cursor.generation,epoch)
        XCTAssertEqual(s.point?.section_id,fixture.sections[0]);XCTAssertFalse(s.cursor.wanted)
        s.stopListening();await s.sync.task?.value
        let target=fixture.sections[4]
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:try JSONSerialization.data(withJSONObject:["rules":[["path":"/api/books/\(fixture.book_id)/sections/\(target)","remaining":1,"status":404]]]))
        await s.open(fixture.book_id,section:target,autoplay:true,textOffset:50);XCTAssertEqual(s.cursor.state,"error");c.publish()
        _=try await js("window.__listenResult=null;window.Player.listen('\(fixture.book_id)').then(()=>window.__listenResult='ok',e=>window.__listenResult=e.message);true")
        try await ready("window.__listenResult!==null");let resumeResult=try await js("window.__listenResult") as? String;XCTAssertEqual(resumeResult,"ok")
        await s.commandTask?.value;try await wait({s.isPlaying});s.pause()
        XCTAssertEqual(s.point?.section_id,fixture.sections[0],"Ordinary Resume must use the audible checkpoint, not a failed Listen From Here target")
        let manualSection=try await js("document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id") as? Int
        XCTAssertEqual(manualSection,target)
        _=try await js("window.__listenResult=null;window.Player.startReader('\(fixture.book_id)',{section_id:\(target),normalized_offset:50}).then(()=>window.__listenResult='ok',e=>window.__listenResult=e.message);true")
        try await ready("window.__listenResult!==null");let explicitResult=try await js("window.__listenResult") as? String;XCTAssertEqual(explicitResult,"ok")
        await s.commandTask?.value;try await wait({s.isPlaying})
        XCTAssertEqual(s.point?.section_id,target);s.stopListening()
    }
    func testChapterImportedDuringPlaybackIsDiscoveredAtTheOldBookBoundary() async throws {
        let s=try service(),last=try XCTUnwrap(fixture.sections.last)
        await s.open(fixture.book_id,section:last,autoplay:false);await s.seek(to:2.2)
        struct Appended:Decodable {let section_id:Int}
        let next:Appended=try await backend.request("/fixture/append",method:"POST",body:try JSONSerialization.data(withJSONObject:["book_id":fixture.book_id]))
        s.play();try await wait({s.point?.section_id==next.section_id && s.isPlaying},timeout:8)
        XCTAssertEqual(s.point?.section_id,next.section_id)
        s.stopListening();await s.sync.task?.value
        let remote=try await backend.progress(fixture.book_id)
        XCTAssertEqual(remote.section_id,s.point?.section_id);XCTAssertEqual(remote.asset_id,s.point?.asset_id)
    }
    func testVerifiedBookEndCompletesOnceAndPreservesTheFinalNativeCheckpoint() async throws {
        let s=try service(),last=try XCTUnwrap(fixture.sections.last)
        await s.open(fixture.book_id,section:last,autoplay:false);await s.seek(to:2.3)
        s.play();try await wait({s.cursor.state=="completed"},timeout:8)
        XCTAssertFalse(s.cursor.wanted);XCTAssertNil(s.player.currentItem)
        XCTAssertEqual(s.point?.section_id,last);XCTAssertEqual(s.point?.seconds ?? 0,2.4,accuracy:0.15)
        XCTAssertEqual(s.events.filter{($0["event"] as? String)=="chapter-boundary-refresh"}.count,1)
        await s.sync.task?.value
        let remote=try await backend.progress(fixture.book_id);XCTAssertEqual(remote.section_id,last)
    }
    func testStopClearsActualQueueAndNowPlayingPersistsAndCannotRestorePassively() async throws {
        for playing in [true,false] {
            let s=try service();await s.open(fixture.book_id,section:fixture.sections[4],autoplay:playing)
            await s.seek(to:0.6)
            let old=s.player.currentItem, oldNode=s.currentNode
            s.stopListening();let sequence=s.point?.event_sequence
            XCTAssertNil(s.player.currentItem);XCTAssertEqual(s.player.rate,0);XCTAssertFalse(s.cursor.wanted);XCTAssertFalse(s.engaged)
            XCTAssertNil(MPNowPlayingInfoCenter.default().nowPlayingInfo);XCTAssertFalse(MPRemoteCommandCenter.shared().playCommand.isEnabled)
            XCTAssertNil(s.store.journal.activeBook);XCTAssertEqual(s.point?.checkpoint_intent,"stop")
            XCTAssertEqual(s.point?.section_id,fixture.sections[4]);XCTAssertEqual(s.point?.seconds ?? -1,0.6,accuracy:0.2)
            s.stopListening();s.foreground();await s.restore();s.passiveSelection(fixture.book_id,section:fixture.sections[0])
            if let old,let oldNode {s.nodes[ObjectIdentifier(old)]=oldNode;s.ended(old)}
            try await Task.sleep(for:.milliseconds(200))
            XCTAssertNil(s.player.currentItem);XCTAssertFalse(s.engaged);XCTAssertEqual(s.point?.event_sequence,sequence)
            await s.sync.task?.value
            let remote=try await backend.progress(fixture.book_id);XCTAssertEqual(remote.seconds,s.point?.seconds)
            let restored=try service(store:CheckpointStore(url:s.store.url));await restored.restore()
            XCTAssertNil(restored.player.currentItem);restored.passiveSelection(fixture.book_id,section:fixture.sections[0])
            XCTAssertNil(restored.commandTask)
            s.play();try await wait({s.isPlaying});s.stopListening()
        }
    }
    func testStopCancelsOpeningEvenWhenCommandTaskHasNotRunYet() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[3],autoplay:false)
        s.launchOpen(fixture.book_id,section:fixture.sections[0],autoplay:true)
        s.stopListening();try await Task.sleep(for:.seconds(1))
        XCTAssertNil(s.player.currentItem);XCTAssertFalse(s.engaged);XCTAssertFalse(s.cursor.wanted)
        XCTAssertEqual(s.point?.section_id,fixture.sections[3]);XCTAssertEqual(s.point?.checkpoint_intent,"stop")
    }
    func testExplicitNewBookListenIsNotBlockedByItsEarlierStoppedWriter() async throws {
        let s=try service(),a=fixture.books[0],b=fixture.books[1],c=fixture.books[2]
        await s.open(b.book_id,section:b.sections[2],autoplay:false);s.stopListening();await s.sync.task?.value
        let oldSession=s.point?.session_id
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:try JSONSerialization.data(withJSONObject:["takeover_book":b.book_id]))
        await s.open(a.book_id,section:a.sections[0],autoplay:true);s.stopListening()
        await s.open(b.book_id,section:b.sections[2],autoplay:true,textOffset:0)
        XCTAssertEqual(s.point?.book_id,b.book_id,s.status);XCTAssertTrue(s.cursor.wanted,s.status);XCTAssertNotEqual(s.point?.session_id,oldSession)
        s.stopListening();await s.open(c.book_id,section:c.sections[30],autoplay:true)
        XCTAssertEqual(s.point?.book_id,c.book_id,s.status);XCTAssertTrue(s.cursor.wanted,s.status)
    }
    func testStopThenSameBookCreatesFreshWriterAndRetainsBookCheckpoint() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[3],autoplay:false);await s.seek(to:0.6)
        s.stopListening();await s.sync.task?.value;let prior=s.point
        s.play();try await wait({s.isPlaying});XCTAssertNotEqual(s.point?.session_id,prior?.session_id)
        XCTAssertEqual(s.point?.section_id,prior?.section_id)
    }
    func testTransientNextChapterFailureRecoversAnExhaustedQueueWithoutReplay() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[0],autoplay:false);await s.seek(to:2.38)
        let p=try XCTUnwrap(s.point),m=try await backend.manifest(p,section:fixture.sections[1]);let path=try XCTUnwrap(m.segments?.first?.url)
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:try JSONSerialization.data(withJSONObject:["rules":[["path":path,"remaining":3,"status":503]]]))
        s.play();try await wait({s.currentNode?.chapter.id==self.fixture.sections[1] && s.isPlaying},timeout:15)
        XCTAssertEqual(s.store.journal.checkpoints[fixture.book_id]?.section_id,fixture.sections[1])
    }
    func testFailedNativeItemRecoversWithoutDependingOnItsPlaybackClock() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[2],autoplay:false)
        let n=try XCTUnwrap(s.currentNode);s.player.removeAllItems();s.nodes.removeAll()
        let badNode=PlaybackService.Node(chapter:n.chapter,manifest:n.manifest,index:n.index,start:n.start,duration:n.duration,file:folder.appendingPathComponent("missing-file.wav"),narration:n.narration)
        let bad=s.item(badNode);s.activeItem=bad;s.activeNode=badNode
        s.player.insert(bad,after:nil);s.cursor.wanted=true;s.player.play()
        try await wait({s.player.currentItem !== bad && s.isPlaying},timeout:10)
        XCTAssertEqual(s.point?.section_id,fixture.sections[2]);XCTAssertEqual(s.point?.segment_index,0)
    }
    func testCoalescedAudioDownloadSurvivesOneCancelledWaiter() async throws {
        let s=try service();let manifest=try await backend.manifest(Checkpoint(book:fixture.book_id,section:fixture.sections[0],asset:"",remote:ServerProgress()),section:fixture.sections[0])
        let path=try XCTUnwrap(manifest.segments?.first?.url)
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:try JSONSerialization.data(withJSONObject:["rules":[["path":path,"remaining":1,"delay":1]]]))
        let first=Task {try await s.files.file(path,protecting:[])};try await Task.sleep(for:.milliseconds(100))
        let second=Task {try await s.files.file(path,protecting:[])};try await Task.sleep(for:.milliseconds(100));first.cancel()
        let local=try await second.value;XCTAssertTrue(FileManager.default.fileExists(atPath:local.path))
        do {_ = try await first.value;XCTFail("Cancelled waiter unexpectedly succeeded")}catch {}
    }
    func testSlowHighlightMetadataCannotBlockReadyAudioAtTheNextChunk() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[0],autoplay:false)
        let p=try XCTUnwrap(s.point),m=try await backend.manifest(p,section:fixture.sections[0]),asset=try XCTUnwrap(m.asset_id)
        let path="/api/audio/\(asset)/location/1"
        let hash=SHA256.hash(data:Data(path.utf8)).map {String(format:"%02x",$0)}.joined()
        try? FileManager.default.removeItem(at:backend.metadataRoot.appendingPathComponent(hash))
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:try JSONSerialization.data(withJSONObject:["rules":[["path":path,"remaining":1,"delay":8]]]))
        s.play();try await wait({s.currentNode?.index==1 && s.isPlaying},timeout:3)
        XCTAssertEqual(s.point?.section_id,fixture.sections[0])
    }
    func testRealSheetDismissalAndIndependentReadingNeverCommandPlayback() async throws {
        let s=try service(),c=ReaderController(service:s),book=fixture.books[2]
        let scene=try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene),window=UIWindow(windowScene:scene),host=UIViewController()
        window.rootViewController=host;window.makeKeyAndVisible();c.webView.frame=host.view.bounds;host.view.addSubview(c.webView);c.load();defer{window.isHidden=true}
        func js(_ value:String) async throws -> Any {try await c.webView.evaluateJavaScript(value)}
        func ready(_ value:String) async throws {let until=Date().addingTimeInterval(25);while Date()<until {if (try? await js(value)) as? Bool==true {return};try await Task.sleep(for:.milliseconds(100))};XCTFail(value);throw PlaybackError.unavailable}
        try await ready("!!window.Player && !!window.PlayerSheet")
        await s.open(book.book_id,section:book.sections[30],autoplay:false)
        _ = try await js("window.Player.navigate('/reader/\(book.book_id)?section=\(book.sections[40])')")
        try await ready("document.getElementById('workspace').contentWindow.ReaderNarration?.page()?.section_id===\(book.sections[40]) && document.getElementById('workspace').contentWindow.ReaderNarration.page().ready")
        XCTAssertEqual(s.point?.section_id,book.sections[30])
        await s.seek(to:2.2);s.play();try await wait({s.currentNode?.chapter.id==book.sections[31]});s.pause();await s.sync.task?.value
        let manual=try await js("document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id") as? Int
        XCTAssertEqual(manual,book.sections[40],"Natural narration stole the manual reading chapter")
        _ = try await js("window.Player.navigate('/reader/\(book.book_id)?section=\(book.sections[0])')")
        try await ready("document.getElementById('workspace').contentWindow.ReaderNarration?.page()?.section_id===\(book.sections[0]) && document.getElementById('workspace').contentWindow.ReaderNarration.page().ready")
        await s.sync.task?.value;let item=s.player.currentItem,point=s.point,generation=s.cursor.generation
        for drag in [false,true] {
            _ = try await js("document.getElementById('miniOpen').click()")
            try await ready("!document.getElementById('playerBackdrop').hidden && document.getElementById('closePlayer').hidden && document.getElementById('playerPanel').getAttribute('role')==='dialog'")
            if drag {
                _ = try await js("(() => {let h=document.getElementById('playerHandle'),p=document.getElementById('playerPanel');h.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:9,isPrimary:true,button:0,clientX:100,clientY:0}));p.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:9,isPrimary:true,clientX:100,clientY:150}));p.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:9,isPrimary:true,clientX:100,clientY:150}));return true})()")
            }else{_ = try await js("document.getElementById('playerBackdrop').click()")}
            try await ready("document.getElementById('playerBackdrop').hidden && !document.getElementById('workspace').inert")
            XCTAssertTrue(s.player.currentItem === item);XCTAssertEqual(s.point,point);XCTAssertEqual(s.cursor.generation,generation)
            let section=try await js("document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id") as? Int
            XCTAssertEqual(section,book.sections[0])
        }
        _ = try await js("document.getElementById('readPlaying').click()")
        try await ready("document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id===\(book.sections[31])")
        XCTAssertTrue(s.player.currentItem === item)
        s.stopListening()
        _ = try await js("window.Player.navigate('/reader/\(fixture.books[1].book_id)?section=\(fixture.books[1].sections[2])')")
        try await ready("document.getElementById('workspace').contentWindow.ReaderNarration?.page()?.section_id===\(fixture.books[1].sections[2]) && document.getElementById('workspace').contentWindow.ReaderNarration.page().ready")
        _ = try await js("document.getElementById('workspace').contentDocument.getElementById('dockListen').click()")
        try await wait({s.point?.book_id==self.fixture.books[1].book_id && s.isPlaying})
        XCTAssertEqual(s.point?.section_id,fixture.books[1].sections[2],s.status)
    }
    func testStopDuringDelayedConflictResolutionCannotReactivateTheSession() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[0],autoplay:false);await s.sync.task?.value
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:Data("{\"delay_next_ack\":3}".utf8))
        s.foreground();s.launchOpen(fixture.book_id,section:nil,autoplay:true,resolution:"local")
        try await wait({s.pendingPlayback != nil});s.stopListening()
        try await Task.sleep(for:.seconds(3.5))
        XCTAssertNil(s.player.currentItem);XCTAssertFalse(s.cursor.wanted);XCTAssertFalse(s.engaged);XCTAssertEqual(s.point?.checkpoint_intent,"stop")
    }
    func testPrefetchRetryBudgetCannotPauseAvailableQueuedAudio() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[0],autoplay:true)
        try await wait({s.player.items().count>=3 && s.isPlaying});let item=s.player.currentItem,point=s.point
        s.gapAttempts=3;s.scheduleContinuation(PlaybackError.unavailable,generation:s.cursor.generation)
        XCTAssertTrue(s.cursor.wanted);XCTAssertTrue(s.isPlaying);XCTAssertTrue(s.player.currentItem === item);XCTAssertEqual(s.point,point)
    }
    func testMissingChunkStopsAtTheLastAudibleCursorAfterBoundedRetriesAndAllowsNewBook() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[0],autoplay:false)
        let p=try XCTUnwrap(s.point),m=try await backend.manifest(p,section:p.section_id),path=try XCTUnwrap(m.segments?[1].url)
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:try JSONSerialization.data(withJSONObject:["rules":[["path":path,"remaining":50,"status":404]]]))
        s.play();try await wait({!s.cursor.wanted&&s.cursor.state=="error"},timeout:12)
        XCTAssertEqual(s.point?.section_id,fixture.sections[0]);XCTAssertEqual(s.point?.segment_index,0);XCTAssertTrue(s.status.contains("404"),s.status)
        let (data,_)=try await backend.session.data(from:backend.origin.resolve("/fixture/status"))
        let faults=(try JSONSerialization.jsonObject(with:data) as? [String:Any])?["faults"] as? [String:Any]
        let hits=faults?["rule_hits"] as? [[String:Any]]
        XCTAssertLessThanOrEqual(hits?.filter {($0["path"] as? String)==path}.count ?? 100,4)
        s.stopListening();await s.open(fixture.books[1].book_id,section:fixture.books[1].sections[0],autoplay:true)
        try await wait({s.isPlaying});XCTAssertEqual(s.point?.book_id,fixture.books[1].book_id)
    }
    func testAudioSessionUsesLegalPlaybackOptionsOnHardware() async throws {
        let s = try service(); try s.activate()
        let audio = AVAudioSession.sharedInstance()
        XCTAssertEqual(audio.category, .playback); XCTAssertEqual(audio.mode, .spokenAudio)
        XCTAssertFalse(audio.categoryOptions.contains(.allowAirPlay))
        s.pause()
    }
    func testOldListenURLOnRealWebViewRemountCannotReplaceNativeCheckpoint() async throws {
        let s=try service(), c=ReaderController(service:s)
        await s.open(fixture.book_id,section:fixture.sections[5],autoplay:false)
        await s.seek(to:1.6); await s.sync.task?.value
        let item=s.player.currentItem, session=s.point?.session_id, generation=s.cursor.generation
        let scene=try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
        let window=UIWindow(windowScene:scene), host=UIViewController()
        window.rootViewController=host;window.makeKeyAndVisible();c.webView.frame=host.view.bounds;host.view.addSubview(c.webView);c.load()
        defer {window.isHidden=true}
        func ready(_ script:String) async throws {
            let until=Date().addingTimeInterval(25)
            while Date()<until {if (try? await c.webView.evaluateJavaScript(script)) as? Bool==true {return};try await Task.sleep(for:.milliseconds(100))}
            XCTFail("Remount bridge not ready");throw PlaybackError.unavailable
        }
        try await ready("!!window.Player && document.readyState==='complete'")
        c.webView.load(URLRequest(url:try backend.origin.resolve("/?case=checkpoint-remount&listen=\(fixture.book_id)&section=\(fixture.sections[0])")))
        try await ready("location.search.includes('case=checkpoint-remount') && !!window.Player && document.readyState==='complete'")
        try await Task.sleep(for:.seconds(1))
        XCTAssertEqual(s.point?.section_id,fixture.sections[5],s.status)
        XCTAssertEqual(s.point?.segment_index,1)
        XCTAssertEqual(s.point?.session_id,session);XCTAssertEqual(s.cursor.generation,generation)
        XCTAssertTrue(s.player.currentItem === item,"Passive remount replaced the native audio item")
        let metrics=try await c.webView.evaluateJavaScript("window.Player.metrics().section_id") as? Int
        XCTAssertEqual(metrics,fixture.sections[5])
    }
    func testCoalescedBackgroundItemChangesAreReconciledBeforeForegroundSave() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[0],autoplay:true)
        try await wait({s.player.items().count>=3})
        // Fault injection: audio/queueing continue, item/progress notifications
        // are delayed, as they can be around suspension or a busy main actor.
        s.observation?.invalidate();s.healthTask?.cancel();s.healthTask=nil
        if let observer=s.timeObservation {s.player.removeTimeObserver(observer);s.timeObservation=nil}
        for token in s.tokens {NotificationCenter.default.removeObserver(token)};s.tokens=[]
        let until=Date().addingTimeInterval(25)
        while Date()<until && (s.currentNode?.chapter.id ?? 0)<fixture.sections[5] {
            s.fill();try await Task.sleep(for:.milliseconds(50))
        }
        let actual=try XCTUnwrap(s.currentNode)
        XCTAssertGreaterThanOrEqual(actual.chapter.id,fixture.sections[5])
        XCTAssertEqual(s.point?.section_id,fixture.sections[0],"Fault did not hold the old metadata")
        let generation=s.cursor.generation,item=s.player.currentItem
        s.foreground()
        XCTAssertEqual(s.point?.section_id,actual.chapter.id)
        XCTAssertEqual(s.store.journal.checkpoints[fixture.book_id]?.section_id,actual.chapter.id)
        XCTAssertTrue(s.player.currentItem === item);XCTAssertEqual(s.cursor.generation,generation)
        XCTAssertTrue(s.cursor.wanted)
        s.pause();await s.sync.task?.value
        let remote=try await backend.progress(fixture.book_id)
        XCTAssertEqual(remote.section_id,actual.chapter.id)
    }
    func testLateEarlierChunkCompletionInQueueGapCannotRollbackDurablePoint() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[0],autoplay:true)
        let earlier=try XCTUnwrap(s.player.currentItem),node=try XCTUnwrap(s.currentNode)
        try await wait({s.point?.segment_index==1})
        s.pause();let expected=try XCTUnwrap(s.point)
        s.player.removeAllItems();s.nodes[ObjectIdentifier(earlier)]=node
        s.ended(earlier)
        XCTAssertEqual(s.point?.segment_index,expected.segment_index)
        XCTAssertEqual(s.point?.seconds,expected.seconds)
        XCTAssertEqual(s.store.journal.checkpoints[fixture.book_id]?.segment_index,expected.segment_index)
    }
    func testDelayedAcknowledgementDoesNotReplaceNewerSeekOrSequence() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[0],autoplay:false)
        await s.sync.task?.value
        let _:IgnoredResponse=try await backend.request("/fixture/control",method:"POST",body:Data("{\"delay_next_ack\":2}".utf8))
        await s.seek(to:0.2);try await Task.sleep(for:.milliseconds(100))
        await s.seek(to:1.6);let sequence=try XCTUnwrap(s.point?.event_sequence)
        await s.sync.task?.value
        XCTAssertGreaterThanOrEqual(s.store.journal.checkpoints[fixture.book_id]?.event_sequence ?? 0,sequence)
        XCTAssertEqual(s.store.journal.checkpoints[fixture.book_id]?.segment_index,1)
        let remote=try await backend.progress(fixture.book_id)
        XCTAssertEqual(remote.segment_index,1);XCTAssertEqual(remote.segment_seconds ?? -1,0.4,accuracy:0.1)
    }
    func testPassiveRestoreAndSelectionNeverReplaceAnExistingNativeItem() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[5],autoplay:false)
        let item=s.player.currentItem,generation=s.cursor.generation
        await s.restore();s.passiveSelection(fixture.book_id,section:fixture.sections[0])
        XCTAssertEqual(s.point?.section_id,fixture.sections[5]);XCTAssertEqual(s.cursor.generation,generation)
        XCTAssertTrue(s.player.currentItem === item)
    }
    func testListenFromVisibleReaderOverridesOldAudioAndOrdinaryPlayResumesIt() async throws {
        let s = try service(), c = ReaderController(service: s)
        await s.open(fixture.book_id, section: fixture.sections[0], autoplay: false)
        await s.sync.task?.value
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
        let window = UIWindow(windowScene: scene), host = UIViewController()
        window.rootViewController = host; window.makeKeyAndVisible()
        c.webView.frame = host.view.bounds; host.view.addSubview(c.webView); c.load()
        defer { window.isHidden = true }
        func ready(_ script: String) async throws {
            for _ in 0..<150 {
                if (try? await c.webView.evaluateJavaScript(script)) as? Bool == true { return }
                try await Task.sleep(for: .milliseconds(100))
            }
            XCTFail("Reader condition timed out: \(script)"); throw PlaybackError.unavailable
        }
        try await ready("!!window.Player")
        _ = try await c.webView.evaluateJavaScript("window.Player.navigate('/reader/\(fixture.book_id)?section=\(fixture.sections[6])')")
        try await ready("document.getElementById('workspace').contentWindow.ReaderNarration?.page()?.ready === true")
        // Make this an intentional manual navigation even while the older audio
        // cursor remains selected. Only Listen may seek to this reader location.
        _ = try await c.webView.evaluateJavaScript("document.getElementById('workspace').contentWindow.document.getElementById('previousSection').click()")
        try await ready("document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id === \(fixture.sections[5]) && document.getElementById('workspace').contentWindow.ReaderNarration.page().ready")
        XCTAssertEqual(s.point?.section_id,fixture.sections[0])
        s.play(); try await wait({s.isPlaying},timeout:3)
        _ = try await c.webView.evaluateJavaScript("document.getElementById('workspace').contentWindow.document.getElementById('dockListen').click()")
        try await wait({s.point?.section_id == self.fixture.sections[5] && !s.activating})
        s.pause(); await s.sync.task?.value
        let saved=try XCTUnwrap(s.point)
        XCTAssertEqual(saved.section_id,fixture.sections[5])
        let remote=try await backend.progress(fixture.book_id)
        XCTAssertEqual(remote.section_id,saved.section_id)
        s.play(); try await wait({s.isPlaying},timeout:3); s.pause()
        XCTAssertEqual(s.point?.section_id,saved.section_id)
        XCTAssertEqual(s.point?.session_id,saved.session_id)
    }
    func testFailedListenPreparationPreservesLastDurableListeningPoint() async throws {
        let s=try service()
        await s.open(fixture.book_id,section:fixture.sections[0],autoplay:false)
        await s.seek(to:1.6); await s.sync.task?.value
        let previous=try XCTUnwrap(s.store.journal.checkpoints[fixture.book_id])
        await s.open(fixture.book_id,section:Int.max,autoplay:true,textOffset:120)
        XCTAssertEqual(s.cursor.state,"error")
        XCTAssertEqual(s.point?.section_id,previous.section_id)
        XCTAssertEqual(s.store.journal.checkpoints[fixture.book_id]?.section_id,previous.section_id)
        XCTAssertEqual(s.store.journal.checkpoints[fixture.book_id]?.seconds,previous.seconds)
        XCTAssertNil(s.pendingPlayback)
        let remote=try await backend.progress(fixture.book_id)
        XCTAssertEqual(remote.section_id,previous.section_id)
    }
    func testNativeQueueAdvances150Through156AndRestoresExactCursorWithoutWebView() async throws {
        let manualData = try JSONSerialization.data(withJSONObject: ["section_id":fixture.sections[0], "version":0, "fraction":0.4, "anchor":"text:2", "preferences":[:]] as [String:Any])
        let _: IgnoredResponse = try await backend.request("/api/books/\(fixture.book_id)/progress", method:"PUT", body:manualData)
        let manualBefore: Reading = try await backend.request("/api/books/\(fixture.book_id)/progress")
        let s = try service()
        await s.open(fixture.book_id,section:fixture.sections[0],autoplay:true)
        XCTAssertNotEqual(s.cursor.state,"error",s.status)
        try s.rate(2.5)
        try await wait({s.point?.section_id == self.fixture.sections[6]},timeout:20)
        s.pause();let saved = try XCTUnwrap(s.point)
        XCTAssertEqual(saved.section_id,fixture.sections[6]);XCTAssertGreaterThanOrEqual(saved.segment_seconds ?? -1,0)
        await s.sync.task?.value
        let remote = try await backend.progress(fixture.book_id)
        XCTAssertEqual(remote.section_id,saved.section_id)
        let freshStore = try CheckpointStore(url:s.store.url)
        XCTAssertEqual(freshStore.journal.checkpoints[fixture.book_id]?.section_id,saved.section_id)
        let resumed = try service(store:freshStore)
        await resumed.restore()
        XCTAssertEqual(resumed.point?.section_id,saved.section_id,resumed.status)
        XCTAssertEqual(resumed.point?.segment_index,saved.segment_index)
        XCTAssertEqual(resumed.point?.segment_seconds ?? -1,saved.segment_seconds ?? -2,accuracy:0.1)
        XCTAssertFalse(resumed.cursor.wanted)
        XCTAssertGreaterThan(s.fixtureSnapshots.count,0)
        let manualAfter: Reading = try await backend.request("/api/books/\(fixture.book_id)/progress")
        XCTAssertEqual(manualBefore,manualAfter)
    }
    func testIntentionalSeekStaysPausedAndSurvivesForegroundReconciliation() async throws {
        let s = try service();await s.open(fixture.book_id,section:fixture.sections[0],autoplay:true)
        s.pause();await s.seek(to:1.6)
        XCTAssertEqual(s.point?.segment_index,1,s.status)
        XCTAssertEqual(s.point?.segment_seconds ?? -1,0.4,accuracy:0.1)
        s.foreground();try await Task.sleep(for:.seconds(1))
        XCTAssertFalse(s.cursor.wanted);XCTAssertEqual(s.player.rate,0)
        XCTAssertEqual(s.point?.segment_index,1)
        XCTAssertNotNil(s.currentNode, "Paused item disappeared: \(s.status)")
        await s.seek(to:0.3)
        XCTAssertEqual(s.point?.segment_index,0);XCTAssertEqual(s.point?.seconds ?? -1,0.3,accuracy:0.1)
        XCTAssertEqual(s.point?.checkpoint_intent,"seek")
    }
    func testOfflineRestoreUsesDurableNativeCursorAndCachedAudio() async throws {
        let s = try service(); await s.open(fixture.book_id, section: fixture.sections[0], autoplay: true)
        s.pause(); await s.seek(to: 1.6); await s.sync.task?.value
        let expected = try XCTUnwrap(s.point)
        let offlineConfig = URLSessionConfiguration.ephemeral; offlineConfig.protocolClasses = [OfflineProtocol.self]
        backend = Backend(origin: backend.origin, configuration: offlineConfig)
        let offline = try service(store: CheckpointStore(url: s.store.url))
        await offline.restore()
        XCTAssertNotNil(offline.currentNode, offline.status)
        XCTAssertEqual(offline.point?.section_id, expected.section_id)
        XCTAssertEqual(offline.point?.segment_index, 1)
        XCTAssertEqual(offline.point?.segment_seconds ?? -1, 0.4, accuracy: 0.1)
        XCTAssertFalse(offline.cursor.wanted)
    }
    func testSystemInterruptionAndHeadphoneRemovalRequireExplicitResume() async throws {
        let s = try service(); await s.open(fixture.book_id, section: fixture.sections[0], autoplay: true)
        let original = s.point?.section_id
        NotificationCenter.default.post(name: AVAudioSession.interruptionNotification, object: AVAudioSession.sharedInstance(), userInfo: [AVAudioSessionInterruptionTypeKey: AVAudioSession.InterruptionType.began.rawValue])
        try await wait({ !s.cursor.wanted }, timeout: 2)
        XCTAssertEqual(s.cursor.state, "interrupted"); XCTAssertEqual(s.player.rate, 0)
        NotificationCenter.default.post(name: AVAudioSession.interruptionNotification, object: AVAudioSession.sharedInstance(), userInfo: [AVAudioSessionInterruptionTypeKey: AVAudioSession.InterruptionType.ended.rawValue, AVAudioSessionInterruptionOptionKey: AVAudioSession.InterruptionOptions.shouldResume.rawValue])
        try await Task.sleep(for: .milliseconds(300)); XCTAssertFalse(s.cursor.wanted)
        s.play(); XCTAssertTrue(s.cursor.wanted)
        NotificationCenter.default.post(name: AVAudioSession.routeChangeNotification, object: AVAudioSession.sharedInstance(), userInfo: [AVAudioSessionRouteChangeReasonKey: AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue])
        try await wait({ !s.cursor.wanted }, timeout: 2)
        XCTAssertEqual(s.point?.section_id, original)
        XCTAssertTrue(MPRemoteCommandCenter.shared().playCommand.isEnabled)
        XCTAssertTrue(MPRemoteCommandCenter.shared().changePlaybackPositionCommand.isEnabled)
    }
    func testPrematureQueueAdvanceDoesNotCommitAnotherChunkOrChapter() async throws {
        let s = try service(); await s.open(fixture.book_id, section: fixture.sections[0], autoplay: true)
        try await wait({ s.player.items().count >= 2 }, timeout: 5)
        s.player.pause() // Inject a queue discontinuity before the first recording ends.
        s.player.advanceToNextItem()
        try await wait({ s.cursor.retries > 0 || s.cursor.state == "error" }, timeout: 2)
        s.pause()
        XCTAssertEqual(s.point?.section_id, fixture.sections[0])
        XCTAssertEqual(s.point?.segment_index, 0)
    }
    func testPauseWhileOpeningRejectsLateCallbacks() async throws {
        let s = try service()
        s.launchOpen(fixture.book_id,section:fixture.sections[0],autoplay:true)
        try await Task.sleep(for:.milliseconds(10));s.pause()
        try await Task.sleep(for:.seconds(1))
        XCTAssertFalse(s.cursor.wanted);XCTAssertEqual(s.player.rate,0)
        XCTAssertFalse(s.activating)
    }
    func testConflictFencesNativeWriterAndPreservesLocalCursor() async throws {
        let s=try service();await s.open(fixture.book_id,section:fixture.sections[0],autoplay:true)
        s.pause();await s.sync.task?.value
        let remote = try await backend.progress(fixture.book_id)
        var replacement=Checkpoint(book:fixture.book_id,section:fixture.sections[2],asset:"",remote:remote,session:"external-player-session")
        let m=try await backend.manifest(replacement,section:fixture.sections[2]);replacement.asset_id=try XCTUnwrap(m.asset_id)
        replacement.segment_index=0;replacement.segment_seconds=0
        let _: Acknowledgement=try await backend.request("/api/books/\(fixture.book_id)/listening",method:"PUT",body:JSONEncoder().encode(replacement))
        let localSection=s.point?.section_id
        s.persist();s.sync.schedule();await s.sync.task?.value
        XCTAssertTrue(s.sync.fencedBooks.contains(fixture.book_id));XCTAssertFalse(s.cursor.wanted)
        XCTAssertEqual(s.point?.section_id,localSection)
        let finalRemote = try await backend.progress(fixture.book_id)
        XCTAssertEqual(finalRemote.section_id,fixture.sections[2])
    }
    func testZPhysicalBackgroundPlaybackAndDurableCheckpoint() async throws {
        #if targetEnvironment(simulator)
        throw XCTSkip("This acceptance check is for a physical iPhone.")
        #else
        let s = try service(); await s.open(fixture.book_id, section: fixture.sections[0], autoplay: true)
        try await wait({ s.isPlaying && s.player.items().count >= 3 }, timeout: 10)
        let opened = await UIApplication.shared.open(URL(string: "https://example.com")!)
        XCTAssertTrue(opened)
        try await wait({ UIApplication.shared.applicationState == .background }, timeout: 5)
        try await wait({ s.point?.section_id == self.fixture.sections[6] }, timeout: Double(ProcessInfo.processInfo.environment["WEBNOVEL_BACKGROUND_TIMEOUT"] ?? "25") ?? 25)
        XCTAssertEqual(UIApplication.shared.applicationState, .background)
        XCTAssertEqual(s.point?.section_id, fixture.sections[6])
        XCTAssertTrue(s.isPlaying)
        let cleanup = UIApplication.shared.beginBackgroundTask(withName: "Save isolated verification results")
        defer { if cleanup != .invalid { UIApplication.shared.endBackgroundTask(cleanup) } }
        s.pause(); await s.sync.task?.value
        let durable = try CheckpointStore(url: s.store.url)
        XCTAssertEqual(durable.journal.checkpoints[fixture.book_id]?.section_id, fixture.sections[6])
        let remote = try await backend.progress(fixture.book_id)
        XCTAssertEqual(remote.section_id, fixture.sections[6])
        let evidence: [String:Any] = ["physicalDevice":true,"applicationState":"background","section_id":fixture.sections[6],"chapter_label":156,"segment_index":s.point?.segment_index ?? -1,"segment_seconds":s.point?.segment_seconds ?? -1,"savedLocally":true,"savedToIsolatedFixture":true,"audioRoute":AVAudioSession.sharedInstance().currentRoute.outputs.map { $0.portType.rawValue }]
        let attachment = XCTAttachment(data: try JSONSerialization.data(withJSONObject: evidence, options:.prettyPrinted), uniformTypeIdentifier:"public.json")
        attachment.name = "Physical background audio checkpoint"; attachment.lifetime = .keepAlways; add(attachment)
        #endif
    }
    func testVoiceChangeKeepsTextPositionAndNativeRateAndSleepControls() async throws {
        let s = try service()
        await s.open(fixture.book_id, section: fixture.sections[0], autoplay: false)
        await s.seek(to: 1.6)
        let offset = s.location?.normalized_offset
        XCTAssertNotNil(offset)
        try await s.changeVoice("en-US-AndrewNeural", synthesis: 0)
        XCTAssertEqual(s.location?.normalized_offset, offset)
        XCTAssertEqual(s.point?.section_id, fixture.sections[0]); XCTAssertFalse(s.cursor.wanted)
        for speed in [0.75,1.0,1.25,1.5,1.75,2.0,2.5,3.0] {
            try s.rate(speed); XCTAssertEqual(s.point?.playback_rate, speed); XCTAssertFalse(s.cursor.wanted)
        }
        try s.rate(1)
        s.play(); try await wait({s.isPlaying})
        s.sleepDeadline = Date().addingTimeInterval(0.3)
        try await wait({!s.cursor.wanted})
        XCTAssertNil(s.sleepDeadline); XCTAssertEqual(s.cursor.state, "paused")
        let durable = try CheckpointStore(url: s.store.url)
        XCTAssertEqual(durable.journal.checkpoints[fixture.book_id]?.section_id, s.point?.section_id)
    }
    func testLibraryBarNavigationAndExpandedPassagePreserveNativePlaybackAndBookPage() async throws {
        let s = try service(), c = ReaderController(service: s)
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
        let window = UIWindow(windowScene: scene), host = UIViewController()
        window.rootViewController = host; window.makeKeyAndVisible()
        c.webView.frame = host.view.bounds; host.view.addSubview(c.webView); c.load()
        defer { window.isHidden = true }
        func js(_ script: String) async throws -> Any { try await c.webView.evaluateJavaScript(script) }
        func ready(_ script: String) async throws {
            for _ in 0..<150 {
                if (try? await js(script)) as? Bool == true { return }
                try await Task.sleep(for: .milliseconds(100))
            }
            XCTFail("Web page condition timed out: \(script)"); throw PlaybackError.unavailable
        }
        try await ready("!!window.Player && !!document.getElementById('workspace')")
        await s.open(fixture.book_id, section: fixture.sections[6], autoplay: false)
        await s.seek(to: 0.6); c.publish()
        let session = s.point?.session_id, generation = s.cursor.generation
        let item = s.player.currentItem, seconds = s.point?.seconds
        // Expanding in the library shows contextual passages, without navigating.
        _ = try await js("document.getElementById('expandPlayer').click()")
        try await ready("!!document.querySelector('#spokenPassages .current-passage mark') && document.getElementById('closePlayer').hidden && document.getElementById('expandPlayer').hidden")
        let context = try await js("(() => {let p=document.querySelector('#spokenPassages .current-passage');return {text:p.textContent,word:p.querySelector('mark').textContent,nearby:document.querySelectorAll('#spokenPassages .nearby-passage').length,route:document.getElementById('workspace').src};})()") as? [String:Any]
        XCTAssertGreaterThan((context?["text"] as? String ?? "").count, (context?["word"] as? String ?? "").count + 5)
        XCTAssertGreaterThan(context?["nearby"] as? Int ?? 0, 0)
        XCTAssertTrue((context?["route"] as? String ?? "").contains("/library"))
        _ = try await js("document.getElementById('playerBackdrop').click();document.getElementById('miniOpen').click()")
        try await ready("!!document.getElementById('workspace').contentWindow.ReaderNarration?.page()?.ready && document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id === \(fixture.sections[6])")
        try await ready("document.getElementById('playerPanel').dataset.mini === 'true' && document.getElementById('workspace').contentWindow.document.getElementById('readingFrame').contentDocument.querySelectorAll('[data-narration-mark]').length > 0")
        XCTAssertEqual(s.point?.session_id, session); XCTAssertEqual(s.cursor.generation, generation)
        XCTAssertTrue(s.player.currentItem === item); XCTAssertEqual(s.point?.seconds, seconds)
        XCTAssertFalse(s.cursor.wanted); XCTAssertTrue(s.following); XCTAssertFalse(s.followSuspended)
        let textScript = "document.getElementById('workspace').contentWindow.document.getElementById('readingFrame').contentDocument.body.textContent"
        let fullText = try await js(textScript) as? String
        XCTAssertGreaterThan(fullText?.count ?? 0, 180)
        await s.seek(to: 1.6); c.publish(); try await Task.sleep(for: .milliseconds(300))
        let updatedText = try await js(textScript) as? String
        XCTAssertEqual(updatedText, fullText)
        // An in-book title tap retains the existing expand/follow behavior, with no iframe reload.
        _ = try await js("window.__bookDocument=document.getElementById('workspace').contentDocument;document.getElementById('readPlaying').click()")
        try await Task.sleep(for: .milliseconds(300))
        let sameDocument = try await js("window.__bookDocument===document.getElementById('workspace').contentDocument") as? Bool
        XCTAssertEqual(sameDocument, true)
        // Return to the library and exercise each independent transport/navigation target.
        _ = try await js("window.Player.navigate('/library')")
        try await ready("document.getElementById('workspace').contentDocument?.querySelector('main') !== null && !document.getElementById('workspace').contentWindow.ReaderNarration")
        _ = try await js("document.getElementById('miniPlay').click()")
        try await wait({s.cursor.wanted})
        let playingRoute = try await js("document.getElementById('workspace').src") as? String ?? ""
        XCTAssertTrue(playingRoute.contains("/library"))
        _ = try await js("document.getElementById('miniPlay').click()")
        try await wait({!s.cursor.wanted})
        for id in ["miniPrevious", "miniNext"] {
            _ = try await js("document.getElementById('\(id)').click()")
            try await Task.sleep(for: .milliseconds(200))
            let route = try await js("document.getElementById('workspace').src") as? String ?? ""
            XCTAssertTrue(route.contains("/library"))
            XCTAssertFalse(s.cursor.wanted)
        }
        for target in ["readPlaying"] {
            await s.open(fixture.book_id, section: fixture.sections[6], autoplay: false)
            await s.seek(to: 0.6); c.publish()
            let savedSession = s.point?.session_id, savedGeneration = s.cursor.generation
            _ = try await js("document.getElementById('\(target)').click()")
            try await ready("!!document.getElementById('workspace').contentWindow.ReaderNarration?.page()?.ready && document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id === \(fixture.sections[6])")
            XCTAssertEqual(s.point?.session_id, savedSession); XCTAssertEqual(s.cursor.generation, savedGeneration)
            XCTAssertFalse(s.cursor.wanted)
            _ = try await js("window.Player.navigate('/library')")
            try await ready("!document.getElementById('workspace').contentWindow.ReaderNarration && document.getElementById('workspace').contentDocument?.readyState==='complete'")
        }
        // Native playback continues while the bar opens the current live chapter.
        await s.open(fixture.book_id, section: fixture.sections[0], autoplay: true)
        try await wait({s.isPlaying}); c.publish()
        let playingSession = s.point?.session_id, playingGeneration = s.cursor.generation
        _ = try await js("document.getElementById('readPlaying').click()")
        try await ready("!!document.getElementById('workspace').contentWindow.ReaderNarration")
        XCTAssertEqual(s.point?.session_id, playingSession); XCTAssertEqual(s.cursor.generation, playingGeneration)
        XCTAssertTrue(s.cursor.wanted)
        s.pause()
        let image = try await c.webView.takeSnapshot(configuration: nil)
        let attachment = XCTAttachment(image: image); attachment.name = "Existing full-page reader opened from native library bar"; attachment.lifetime = .keepAlways; add(attachment)
    }
    func testRealWebViewBridgeDisablesHTMLAudioAndFollowsNativePositionAfterRemount() async throws {
        let s=try service();let c=ReaderController(service:s)
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
        let window = UIWindow(windowScene: scene); let host = UIViewController()
        window.rootViewController = host; window.makeKeyAndVisible()
        c.webView.frame = host.view.bounds; c.webView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        host.view.addSubview(c.webView); c.load()
        defer { window.isHidden = true }
        let end=Date().addingTimeInterval(15)
        var ready=false
        while Date()<end {
            ready=(try? await c.webView.evaluateJavaScript("!!window.Player && !!document.getElementById('workspace')")) as? Bool ?? false
            if ready { break };try await Task.sleep(for:.milliseconds(100))
        }
        XCTAssertTrue(ready,c.error ?? "Bridge not ready")
        let blocked=try await c.webView.callAsyncJavaScript("try { await document.getElementById('bookAudio').play(); return false; } catch(e) { return e.name === 'NotAllowedError'; }",arguments:[:],in:nil,contentWorld:.page)
        XCTAssertEqual(blocked as? Bool,true)
        // Original browser closure exposes no native flag; the injected adapter does.
        let native=try await c.webView.evaluateJavaScript("window.Player.metrics().native")
        XCTAssertEqual(native as? Bool,true)
        let _ = try await c.webView.callAsyncJavaScript("await window.Player.listen(book, section, true);return true;",arguments:["book":fixture.book_id,"section":fixture.sections[0]],in:nil,contentWorld:.page)
        try await wait({s.currentNode != nil && !s.activating})
        s.pause();await s.seek(to:1.6);s.publish()
        let state=try await c.webView.callAsyncJavaScript("const r=await window.Player.resolveNarration(book); return {section:r?.section_id,index:r?.segment_index,offset:r?.normalized_offset};",arguments:["book":fixture.book_id],in:nil,contentWorld:.page) as? [String:Any]
        XCTAssertEqual(state?["section"] as? Int,fixture.sections[0]);XCTAssertEqual(state?["index"] as? Int,1)
        XCTAssertNotNil(state?["offset"])
        c.webView.reload();try await Task.sleep(for:.seconds(2));c.publish()
        let after=try await c.webView.callAsyncJavaScript("return await window.Player.readerResume(book);",arguments:["book":fixture.book_id],in:nil,contentWorld:.page) as? [String:Any]
        XCTAssertNotNil(after?["location"])
        XCTAssertEqual(s.point?.segment_index,1)
        await s.open(fixture.book_id, section: fixture.sections[6], autoplay: false)
        await s.seek(to: 1.6); c.publish()
        let _ = try await c.webView.callAsyncJavaScript("window.Player.navigate('/reader/'+book+'?section='+section);return true;", arguments:["book":fixture.book_id, "section":fixture.sections[0]], in:nil, contentWorld:.page)
        var pageReady = false
        for _ in 0..<100 {
            pageReady = (try? await c.webView.evaluateJavaScript("!!document.getElementById('workspace').contentWindow.ReaderNarration?.page()?.ready")) as? Bool ?? false
            if pageReady { break }; try await Task.sleep(for:.milliseconds(100))
        }
        XCTAssertTrue(pageReady)
        let manualSection = try await c.webView.evaluateJavaScript("document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id")
        XCTAssertEqual(manualSection as? Int, fixture.sections[0])
        let _ = try await c.webView.callAsyncJavaScript("await document.getElementById('workspace').contentWindow.ReaderNarration.follow();return true;", arguments:[:], in:nil, contentWorld:.page)
        var followedSection = 0, markCount = 0
        for _ in 0..<100 {
            let state = try await c.webView.evaluateJavaScript("(() => { const r=document.getElementById('workspace').contentWindow; return {section:r.ReaderNarration.page().section_id, marks:r.document.getElementById('readingFrame').contentDocument?.querySelectorAll('[data-narration-mark]').length || 0}; })()") as? [String:Any]
            followedSection = state?["section"] as? Int ?? 0; markCount = state?["marks"] as? Int ?? 0
            if followedSection == fixture.sections[6] && markCount > 0 { break }
            try await Task.sleep(for:.milliseconds(100))
        }
        XCTAssertEqual(followedSection, fixture.sections[6])
        XCTAssertGreaterThan(markCount, 0)
        XCTAssertEqual(s.point?.section_id, fixture.sections[6])
        let image = try await c.webView.takeSnapshot(configuration: nil)
        let attachment = XCTAttachment(image: image); attachment.name = "Back to Read Aloud at native Chapter 156"; attachment.lifetime = .keepAlways; add(attachment)
        XCTAssertEqual(MPNowPlayingInfoCenter.default().nowPlayingInfo?[MPMediaItemPropertyTitle] as? String,s.chapter?.title)
    }
}

final class OfflineProtocol: URLProtocol, @unchecked Sendable {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() { client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet)) }
    override func stopLoading() {}
}

// Test-only loopback relay. The release app never contains this code or the
// fixture host. All playback and UI requests remain on localhost in tests;
// only synthetic fixtures travel over the private LAN to the disposable Flask.
final class PhoneFixtureRelay: @unchecked Sendable {
    static let shared = PhoneFixtureRelay()
    private let queue = DispatchQueue(label: "Webnovel.IsolatedFixtureRelay")
    private var listener: NWListener?
    private var connections: [UUID: FixtureConnection] = [:]
    func start(host: String) async throws {
        if listener != nil { return }
        guard host.hasPrefix("192.168."), IPv4Address(host) != nil else { throw PlaybackError.untrusted }
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: 18765)
        let server = try NWListener(using: parameters)
        server.newConnectionHandler = { [weak self] local in
            guard let self, self.connections.count < 32 else { local.cancel(); return }
            let id = UUID()
            let remote = NWConnection(host: NWEndpoint.Host(host), port: 18766, using: .tcp)
            let pair = FixtureConnection(local: local, remote: remote, queue: self.queue) { [weak self] in self?.connections.removeValue(forKey: id) }
            self.connections[id] = pair; pair.start()
        }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            var resolved = false
            server.stateUpdateHandler = { state in
                guard !resolved else { return }
                switch state {
                case .ready: resolved = true; continuation.resume()
                case .failed(let error): resolved = true; continuation.resume(throwing: error)
                default: break
                }
            }
            server.start(queue: queue)
        }
        listener = server
    }
}
private final class FixtureConnection: @unchecked Sendable {
    let local: NWConnection; let remote: NWConnection; let queue: DispatchQueue
    let completion: () -> Void; var closed = false
    init(local: NWConnection, remote: NWConnection, queue: DispatchQueue, completion: @escaping () -> Void) { self.local = local; self.remote = remote; self.queue = queue; self.completion = completion }
    func start() {
        local.stateUpdateHandler = { [weak self] state in if case .failed = state { self?.close() } }
        remote.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            if case .ready = state { self.pump(self.local, to: self.remote); self.pump(self.remote, to: self.local) }
            if case .failed = state { self.close() }
        }
        local.start(queue: queue); remote.start(queue: queue)
        queue.asyncAfter(deadline: .now() + 60) { [weak self] in self?.close() }
    }
    func pump(_ source: NWConnection, to target: NWConnection) {
        source.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, complete, error in
            guard let self, !self.closed else { return }
            if let data, !data.isEmpty {
                target.send(content: data, completion: .contentProcessed { [weak self] sendError in
                    guard let self else { return }
                    if sendError != nil || error != nil || complete { self.close() }
                    else { self.pump(source, to: target) }
                })
            } else if complete || error != nil { self.close() }
            else { self.pump(source, to: target) }
        }
    }
    func close() { guard !closed else { return }; closed = true; local.cancel(); remote.cancel(); completion() }
}
