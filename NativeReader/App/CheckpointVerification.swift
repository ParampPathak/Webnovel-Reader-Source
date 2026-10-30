import Foundation
import AVFoundation
import UIKit
import WebKit
import Network
import MediaPlayer

// Explicit Mac-launched synthetic verification only. The normal launch path,
// production origin, real journal and real audio cache are never used here.
@MainActor
final class CheckpointVerification {
    struct FixtureBook:Decodable {let book_id:String;let sections:[Int]}
    struct Info:Decodable {let book_id:String;let sections:[Int];let books:[FixtureBook]?}
    let reliability=ProcessInfo.processInfo.arguments.contains("--reliability-verification")
    let interactive=ProcessInfo.processInfo.arguments.contains("--interactive-fixture")
    let service:PlaybackService;let controller:ReaderController;let reportURL:URL
    var info:Info?;var samples:[[String:Any]]=[];var phase="starting"
    var wasBackground=false;var backgroundTargetReached=false;var returning=false;var uiSection:Int?;var failure:String?
    var task:Task<Void,Never>?
    init(service:PlaybackService,controller:ReaderController,support:URL,resume:Bool) {
        self.service=service;self.controller=controller
        reportURL=support.appendingPathComponent("CheckpointVerification/"+(resume ? "resume-report.json":"run-report.json"))
        task=Task {await self.run(resume:resume)}
    }
    func sample() {
        let p=service.point,j=service.store.journal.checkpoints[p?.book_id ?? info?.book_id ?? ""],n=service.currentNode
        let state=UIApplication.shared.applicationState
        if state != .active {wasBackground=true}
        samples.append(["at":Date().timeIntervalSince1970,"application_state":state == .active ? "active":state == .background ? "background":"inactive",
                        "book_id":p?.book_id ?? "","live_section":n?.chapter.id ?? -1,"live_segment":n?.index ?? -1,"live_seconds":service.observedSeconds,
                        "point_section":p?.section_id ?? -1,"point_segment":p?.segment_index ?? -1,"sequence":p?.event_sequence ?? 0,
                        "durable_section":j?.section_id ?? -1,"durable_segment":j?.segment_index ?? -1,"durable_seconds":j?.seconds ?? 0,
                        "durable_sequence":j?.event_sequence ?? 0,"generation":service.cursor.generation,"playing":service.isPlaying,
                        "session":p?.session_id ?? "","engaged":service.engaged,"queue_count":service.player.items().count,"native_rate":service.player.rate,"now_playing_cleared":MPNowPlayingInfoCenter.default().nowPlayingInfo == nil,"remote_play_enabled":MPRemoteCommandCenter.shared().playCommand.isEnabled,"status":service.status])
        if samples.count>400 {samples.removeFirst()}
        write()
    }
    func write() {
        var report:[String:Any]=["phase":phase,"samples":samples,"transitions":service.fixtureSnapshots,
                               "was_background":wasBackground,"synthetic_only":true,"diagnostics":service.events]
        report["ui_section"]=uiSection;report["failure"]=failure
        if let data=try? JSONSerialization.data(withJSONObject:report,options:[.sortedKeys]) {try? CheckpointStore.write(data,to:reportURL)}
    }
    func run(resume:Bool) async {
        do {
            info=try await service.backend.request("/fixture/info")
            if reliability,let books=info?.books,books.count>=3 {
                info=Info(book_id:books[2].book_id,sections:Array(books[2].sections[30..<38]),books:books)
            }
            guard let info else {return}
            if interactive {
                await service.restore();phase="interactive-synthetic-only";sample()
                for _ in 0..<600 {try await Task.sleep(for:.seconds(1));sample()};return
            }
            if resume {
                let saved=service.store.journal.checkpoints[info.book_id]
                await service.restore();phase="restored-stopped";sample()
                guard service.player.currentItem == nil,!service.engaged,saved?.checkpoint_intent == "stop" else {throw PlaybackError.invalidSequence}
                await service.open(info.book_id,section:nil,autoplay:false)
                guard service.point?.section_id == saved?.section_id,abs((service.point?.seconds ?? -1)-(saved?.seconds ?? 0))<0.25 else {throw PlaybackError.invalidSequence}
                phase="explicit-resume-restored-paused";sample()
                service.play();try await Task.sleep(for:.seconds(3));service.stopListening();await service.sync.task?.value
                phase="resume-complete";sample();return
            }
            if reliability {try await crossBookPreflight(info)}
            await service.open(info.book_id,section:info.sections[0],autoplay:true)
            phase="playing";sample()
            for _ in 0..<360 {
                try await Task.sleep(for:.seconds(1));sample()
                if let n=service.currentNode,n.chapter.id==info.sections[6] || n.chapter.id==info.sections[7] {
                    backgroundTargetReached=true
                }
                if backgroundTargetReached {
                    phase="ready-for-foreground";write()
                    if wasBackground && UIApplication.shared.applicationState == .active {await verifyForeground();return}
                }
                if service.cursor.state=="error" {throw PlaybackError.unavailable}
            }
            throw PlaybackError.unavailable
        } catch {failure=error.localizedDescription;phase="failed";service.pause();sample()}
    }
    func verifyForeground() async {
        guard !returning,let info else {return};returning=true
        service.foreground();sample()
        let generation=service.cursor.generation,session=service.point?.session_id
        do {
            controller.webView.load(URLRequest(url:try service.backend.origin.resolve("/?case=checkpoint-remount&listen=\(info.book_id)&section=\(info.sections[0])")))
            for _ in 0..<100 {
                if (try? await controller.webView.evaluateJavaScript("location.search.includes('case=checkpoint-remount') && !!window.Player && document.readyState==='complete'")) as? Bool==true {break}
                try await Task.sleep(for:.milliseconds(100))
            }
            try await Task.sleep(for:.seconds(1))
            uiSection=(try? await controller.webView.evaluateJavaScript("window.Player.metrics().section_id")) as? Int
            guard service.cursor.generation==generation,service.point?.session_id==session,service.cursor.wanted,
                  uiSection==service.currentNode?.chapter.id,service.point?.section_id==service.currentNode?.chapter.id,
                  service.point?.section_id != info.sections[0] else {throw PlaybackError.invalidSequence}
            phase="passed-live-remount";sample()
            if reliability {try await verifyIndependentReading(info)}
            try await verifyStopControls(info)
            phase="passed-live-remount-and-stops";sample()
        } catch {failure=error.localizedDescription;phase="failed";service.pause();sample()}
    }
    func playing() async throws {
        for _ in 0..<100 {if service.isPlaying {return};try await Task.sleep(for:.milliseconds(100))}
        throw PlaybackError.unavailable
    }
    func crossBookPreflight(_ info:Info) async throws {
        guard let books=info.books,books.count>=3 else {throw PlaybackError.unavailable}
        phase="cross-book-preflight";sample()
        await service.open(books[1].book_id,section:books[1].sections[2],autoplay:false)
        await service.seek(to:0.4);service.stopListening();await service.sync.task?.value
        let prior=service.point
        await service.open(books[0].book_id,section:books[0].sections[0],autoplay:true);try await playing();service.stopListening()
        await service.open(books[1].book_id,section:nil,autoplay:true);try await playing()
        guard service.point?.book_id==books[1].book_id,service.point?.section_id==prior?.section_id,service.point?.session_id != prior?.session_id else {throw PlaybackError.invalidSequence}
        phase="cross-book-resume-passed";sample();service.stopListening();await service.sync.task?.value
        let stoppedWriter=service.point?.session_id
        service.play();try await playing()
        guard service.point?.session_id != stoppedWriter else {throw PlaybackError.invalidSequence}
        phase="same-book-restart-passed";sample();service.stopListening();await service.sync.task?.value
        await service.open(books[2].book_id,section:books[2].sections[1],autoplay:true);try await playing()
        phase="three-book-restart-passed";sample();service.stopListening();await service.sync.task?.value
        // Cancel a real slow preparation and verify that its late completion
        // cannot affect a different book. Only the disposable fixture is used.
        let candidate=Checkpoint(book:books[2].book_id,section:books[2].sections[0],asset:"",remote:ServerProgress())
        let manifest=try await service.backend.manifest(candidate,section:candidate.section_id)
        if let path=manifest.segments?.first?.url {
            let body=try JSONSerialization.data(withJSONObject:["rules":[["path":path,"remaining":1,"delay":4]]])
            let _:IgnoredResponse=try await service.backend.request("/fixture/control",method:"POST",body:body)
            service.launchOpen(candidate.book_id,section:candidate.section_id,autoplay:true)
            try await Task.sleep(for:.milliseconds(200));service.stopListening()
            await service.open(books[0].book_id,section:books[0].sections[1],autoplay:true);try await playing()
            try await Task.sleep(for:.seconds(4.2))
            guard service.point?.book_id==books[0].book_id,service.cursor.wanted else {throw PlaybackError.invalidSequence}
            service.stopListening();await service.sync.task?.value
        }
        phase="cancelled-buffer-switch-passed";sample()
    }
    func verifyIndependentReading(_ info:Info) async throws {
        guard let book=info.books?[2] else {throw PlaybackError.unavailable}
        func openReading(_ index:Int) async throws {
            _ = try await controller.webView.evaluateJavaScript("window.Player.navigate('/reader/\(book.book_id)?section=\(book.sections[index])')")
            try await ready("document.getElementById('workspace').contentWindow.ReaderNarration?.page()?.section_id===\(book.sections[index]) && document.getElementById('workspace').contentWindow.ReaderNarration.page().ready")
        }
        let generation=service.cursor.generation,session=service.point?.session_id
        try await openReading(40)
        guard service.cursor.generation==generation,service.point?.session_id==session,service.cursor.wanted else {throw PlaybackError.invalidSequence}
        // Accelerate only the synthetic chapter end so natural next-chapter
        // advancement can be observed while a different reading page is open.
        let old=service.point?.section_id
        await service.seek(to:max(0,service.total-0.2))
        for _ in 0..<100 {if service.point?.section_id != old {break};try await Task.sleep(for:.milliseconds(100))}
        let reading=try await controller.webView.evaluateJavaScript("document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id") as? Int
        guard service.point?.section_id != old,reading==book.sections[40] else {throw PlaybackError.invalidSequence}
        try await openReading(0)
        for drag in [false,true] {
            let epoch=service.cursor.generation,owner=service.point?.session_id,chapter=service.point?.section_id
            _ = try await controller.webView.evaluateJavaScript("document.getElementById('expandPlayer').click()")
            try await ready("!document.getElementById('playerBackdrop').hidden && document.getElementById('closePlayer').hidden")
            if drag {
                _ = try await controller.webView.evaluateJavaScript("(() => {let h=document.getElementById('playerHandle'),p=document.getElementById('playerPanel');h.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,pointerId:9,isPrimary:true,button:0,clientX:100,clientY:0}));p.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,pointerId:9,isPrimary:true,clientX:100,clientY:150}));p.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,pointerId:9,isPrimary:true,clientX:100,clientY:150}));return true})()")
            }else{_ = try await controller.webView.evaluateJavaScript("document.getElementById('playerBackdrop').click()")}
            try await ready("document.getElementById('playerBackdrop').hidden && !document.getElementById('workspace').inert")
            let manual=try await controller.webView.evaluateJavaScript("document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id") as? Int
            guard service.cursor.wanted,service.cursor.generation==epoch,service.point?.session_id==owner,service.point?.section_id==chapter,manual==book.sections[0] else {throw PlaybackError.invalidSequence}
        }
        _ = try await controller.webView.evaluateJavaScript("document.getElementById('readPlaying').click()")
        try await ready("document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id===\(service.point?.section_id ?? -1)")
        try await openReading(0)
        await service.open(book.book_id,section:book.sections[40],autoplay:true);try await playing()
        let reverse=try await controller.webView.evaluateJavaScript("document.getElementById('workspace').contentWindow.ReaderNarration.page().section_id") as? Int
        guard reverse==book.sections[0],service.point?.section_id==book.sections[40] else {throw PlaybackError.invalidSequence}
        phase="listen160-read120-passed";sample()
        _ = try await controller.webView.evaluateJavaScript("document.getElementById('workspace').contentDocument.getElementById('dockListen').click()")
        for _ in 0..<100 {if service.point?.section_id==book.sections[0]&&service.isPlaying {break};try await Task.sleep(for:.milliseconds(100))}
        guard service.point?.section_id==book.sections[0],service.isPlaying else {throw PlaybackError.invalidSequence}
        phase="independent-reading-and-sheet-passed";sample()
    }
    func ready(_ script:String) async throws {
        for _ in 0..<150 {
            if (try? await controller.webView.evaluateJavaScript(script)) as? Bool==true {return}
            try await Task.sleep(for:.milliseconds(100))
        }
        throw PlaybackError.unavailable
    }
    func verifyStopControls(_ info:Info) async throws {
        // Real DOM controls on the iPhone, real native queue, synthetic state only.
        _ = try await controller.webView.evaluateJavaScript("window.Player.navigate('/library')")
        try await Task.sleep(for:.milliseconds(400))
        let _:IgnoredResponse = try await service.backend.request("/fixture/control",method:"POST",body:Data("{\"outage_seconds\":5}".utf8))
        _ = try await controller.webView.evaluateJavaScript("document.getElementById('closePlayer').click()")
        try await Task.sleep(for:.milliseconds(200))
        try assertStopped(info);phase="library-X-stopped-offline";sample()
        let stoppedSequence=service.point?.event_sequence
        service.foreground();service.passiveSelection(info.book_id,section:info.sections[0])
        try await Task.sleep(for:.seconds(1));try assertStopped(info)
        guard service.point?.event_sequence==stoppedSequence else {throw PlaybackError.invalidSequence}
        let _:IgnoredResponse = try await service.backend.request("/fixture/control",method:"POST",body:Data("{\"outage_seconds\":0}".utf8))
        await service.sync.task?.value;service.sync.schedule();await service.sync.task?.value
        _ = try await controller.webView.evaluateJavaScript("window.Player.listen('\(info.book_id)');true")
        for _ in 0..<100 {if service.isPlaying {break};try await Task.sleep(for:.milliseconds(100))}
        guard service.isPlaying else {throw PlaybackError.unavailable}
        _ = try await controller.webView.evaluateJavaScript("window.Player.navigate('/reader/\(info.book_id)')")
        try await ready("!!document.getElementById('workspace').contentWindow.ReaderNarration && !!document.getElementById('workspace').contentDocument.getElementById('readerMiniClose')")
        _ = try await controller.webView.evaluateJavaScript("document.getElementById('workspace').contentDocument.getElementById('readerMiniClose').click()")
        try await Task.sleep(for:.milliseconds(200));try assertStopped(info)
        phase="reader-X-stopped";sample();await service.sync.task?.value
        let remote=try await service.backend.progress(info.book_id)
        guard remote.section_id==service.point?.section_id,abs(remote.seconds-(service.point?.seconds ?? -1))<0.25 else {throw PlaybackError.invalidSequence}
    }
    func assertStopped(_ info:Info) throws {
        guard !service.engaged,!service.cursor.wanted,service.player.currentItem==nil,service.player.rate==0,
              MPNowPlayingInfoCenter.default().nowPlayingInfo==nil,!MPRemoteCommandCenter.shared().playCommand.isEnabled,
              service.store.journal.activeBook==nil,service.store.journal.checkpoints[info.book_id]?.checkpoint_intent=="stop" else {throw PlaybackError.invalidSequence}
    }

}

final class ProbeFixtureRelay: @unchecked Sendable {
    static let shared = ProbeFixtureRelay()
    private let queue = DispatchQueue(label: "Webnovel.IsolatedFixtureRelay")
    private var listener: NWListener?
    private var connections: [UUID: ProbeFixtureConnection] = [:]
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
            let pair = ProbeFixtureConnection(local: local, remote: remote, queue: self.queue) { [weak self] in self?.connections.removeValue(forKey: id) }
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
private final class ProbeFixtureConnection: @unchecked Sendable {
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
