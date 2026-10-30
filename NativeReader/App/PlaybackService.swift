import AVFoundation
import MediaPlayer
import UIKit

@MainActor
final class PlaybackService {
    struct Node {
        let chapter: Chapter; let manifest: Manifest; let index: Int?; let start: Double
        let duration: Double; let file: URL; var narration: Narration?
        var asset: String { manifest.asset_id! }
    }
    let backend: Backend; let store: CheckpointStore; let sync: CheckpointSync; let files: AudioFiles
    let player = AVQueuePlayer()
    var cursor = QueueCursor()
    var book: Book?; var chapter: Chapter?; var point: Checkpoint?
    var nodes: [ObjectIdentifier: Node] = [:]
    var lastNode: Node?
    var activeItem: AVPlayerItem?
    var activeNode: Node?
    var location: Narration?
    var status = "Choose a book to listen."
    var engaged = false; var following = true; var followSuspended = false
    var volume: Float = 1 { didSet { player.volume = volume; UserDefaults.standard.set(volume, forKey: "nativeVolume") } }
    var sleepDeadline: Date?
    var onChange: (() -> Void)?
    var fillTask: Task<Void, Never>?; var commandTask: Task<Void, Never>?
    private let failedItems=FailedItems()
    var itemStatuses:[ObjectIdentifier:NSKeyValueObservation]=[:]
    var observation: NSKeyValueObservation?; var timeObservation: Any?
    var tokens: [NSObjectProtocol] = []; var remoteTokens: [(MPRemoteCommand, Any)] = []
    var recoveryTask:Task<Void,Never>?
    var healthTask:Task<Void,Never>?
    var lastMovement=Date()
    var mediaCommandFloor=ProcessInfo.processInfo.systemUptime
    var gapAttempts=0
    var boundaryRefresh:String?
    var metadataOffline=false
    var events:[[String:Any]]=[]
    var narrationMemo:[String:Narration]=[:]
    var narrationWaiting:[Node]=[]
    var narrationTasks:[String:(id:UUID,task:Task<Void,Never>)]=[:]
    var activating = false; var seeking = false; var restoreOffset: Double = 0
    var lastSync = Date.distantPast; var lastPersist = Date.distantPast
    var completed = false; var endPending = false; var stalledTicks = 0; var lastClock: Double = -1
    var fixtureSnapshots: [[String: Any]] = []
    // The durable listening point remains authoritative until preparation and
    // the requested native seek have both succeeded.
    var pendingPlayback: (point: Checkpoint?, book: Book?, chapter: Chapter?)?

    init(backend: Backend, store: CheckpointStore, cache: URL) throws {
        self.backend = backend; self.store = store
        sync = CheckpointSync(backend: backend, store: store)
        files = try AudioFiles(backend: backend, root: cache)
        if UserDefaults.standard.object(forKey: "nativeVolume") != nil { volume = UserDefaults.standard.float(forKey: "nativeVolume"); player.volume = volume }
        player.automaticallyWaitsToMinimizeStalling = true
        player.actionAtItemEnd = .advance
        observation = player.observe(\.currentItem, options: [.new]) { [weak self] _, _ in
            Task { @MainActor in self?.currentChanged() }
        }
        timeObservation = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.2, preferredTimescale: 600), queue: .main) { [weak self] _ in
            Task { @MainActor in self?.tick() }
        }
        let nc = NotificationCenter.default
        tokens.append(nc.addObserver(forName: .AVPlayerItemDidPlayToEndTime, object: nil, queue: .main) { [weak self] note in
            let item = note.object as? AVPlayerItem
            Task { @MainActor in guard let self, let item, self.nodes[ObjectIdentifier(item)] != nil else { return }; self.ended(item) }
        })
        tokens.append(nc.addObserver(forName: .AVPlayerItemFailedToPlayToEndTime, object: nil, queue: .main) { [weak self] note in
            let item = note.object as? AVPlayerItem
            if let item {self?.failedItems.insert(ObjectIdentifier(item))}
            Task { @MainActor in guard let self,let item else {return};self.itemFailed(item) }
        })
        tokens.append(nc.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] note in
            let type = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            Task { @MainActor in if type == AVAudioSession.InterruptionType.began.rawValue { self?.pause(interrupted: true) } }
        })
        tokens.append(nc.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] note in
            let reason = note.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
            Task { @MainActor in if reason == AVAudioSession.RouteChangeReason.oldDeviceUnavailable.rawValue { self?.pause(interrupted: true) } }
        })
        tokens.append(nc.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.pause(interrupted: true); self?.status = "Audio services restarted. Tap Play to restore the saved position."; self?.publish() }
        })
        sync.onConflict = { [weak self] event in
            guard let self,point?.book_id==event.book_id,point?.session_id==event.session_id,
                  point?.session_generation==event.session_generation else {return}
            pause(interrupted:true);status=PlaybackError.conflict.localizedDescription;publish()
        }
        sync.onNotice = { [weak self] text in self?.status = text; self?.publish() }
        backend.onDiagnostic={ [weak self] fields in
            if (fields["http_status"] as? Int)==200 {self?.metadataOffline=false}
            self?.trace("transport",fields:fields)
        }
        installRemoteCommands()
        // A failed/exhausted item may stop AVPlayer's periodic clock entirely.
        // Health/retry scheduling therefore cannot depend on that same clock.
        healthTask=Task { [weak self] in
            while !Task.isCancelled {
                do {try await Task.sleep(for:.seconds(1))}catch{return}
                if self?.cursor.wanted==true {self?.tick()}
            }
        }
    }
    deinit {healthTask?.cancel()}
    func trace(_ event:String,_ detail:String="",fields:[String:Any]=[:]) {
        var entry:[String:Any]=["at":Date().timeIntervalSince1970,"event":event,"detail":detail,"generation":cursor.generation,"book_id":point?.book_id ?? "","section_id":currentNode?.chapter.id ?? point?.section_id ?? -1,"segment_index":currentNode?.index ?? -1,"queue_count":player.items().count,"intent":cursor.wanted,"state":cursor.state,"rate":player.rate,"time_control":player.timeControlStatus.rawValue,"waiting_reason":player.reasonForWaitingToPlay?.rawValue ?? "","item_status":player.currentItem?.status.rawValue ?? -1]
        entry.merge(fields){_,new in new};events.append(entry)
        if events.count>200 {events.removeFirst()}
    }
    var currentNode: Node? { player.currentItem.flatMap { nodes[ObjectIdentifier($0)] } }
    var activePoint: Checkpoint? { point }
    var isPlaying: Bool { cursor.wanted && player.timeControlStatus == .playing }
    var total: Double {
        guard let n = currentNode ?? lastNode else { return 0 }
        if n.index == nil { return player.currentItem?.duration.seconds.isFinite == true ? player.currentItem!.duration.seconds : n.duration }
        return (n.manifest.segments ?? []).reduce(0) { $0 + $1.duration }
    }
    func persist(intent: String = "playback", observe: Bool = true) {
        // Lifecycle/remote controls can arrive before queued item KVO callbacks.
        // Reconcile the actual item first; never publish stale metadata as a
        // newer ordered event merely because the app became active.
        if point?.checkpoint_intent == "stop" && player.currentItem == nil && intent != "stop" { return }
        if observe && !activating && !seeking && pendingPlayback == nil { currentChangedIfNeeded() }
        guard pendingPlayback == nil, var p = point, !p.asset_id.isEmpty else { return }
        if observe && !activating && !seeking, let n = currentNode, n.chapter.id == p.section_id, n.asset == p.asset_id, n.index == p.segment_index, activeItem === player.currentItem {
            let value = player.currentTime().seconds
            if value.isFinite && value >= 0 {
                p.observe(seconds: n.start + value, segment: n.index, offset: n.index == nil ? nil : value, state: cursor.state, intent: intent)
            }
        } else { p.event_sequence += 1; p.checkpoint_intent = intent; p.playback_state = cursor.state }
        p.text_anchor = location?.anchor ?? ""; p.normalized_offset = location?.normalized_offset
        // Sync acknowledgements may have advanced the version while playback continued.
        if let durable = store.journal.checkpoints[p.book_id], durable.session_id == p.session_id {
            p.version = durable.version; p.claim_session = durable.claim_session
        }
        do {
            try store.update { journal in
                journal.activeBook = intent == "stop" ? nil : p.book_id
                if journal.checkpoints[p.book_id]?.session_id != p.session_id { journal.acknowledged[p.book_id] = 0 }
                journal.checkpoints[p.book_id] = p
            }
            point = p; lastPersist = Date()
            if let data=try? JSONSerialization.data(withJSONObject:events) {try? CheckpointStore.write(data,to:store.url.deletingPathExtension().appendingPathExtension("diagnostics.json"))}
        } catch {
            player.pause(); cursor.pause(); status = PlaybackError.storage.localizedDescription
        }
    }
    func stopListening() {
        // Silence first. Neither persistence nor a network request delays stop.
        player.pause()
        cancelNarration();trace("stop");mediaCommandFloor=ProcessInfo.processInfo.systemUptime
        recoveryTask?.cancel();recoveryTask=nil;gapAttempts=0
        try? store.update {$0.pendingListen=nil}
        guard engaged || pendingPlayback != nil || player.currentItem != nil || commandTask != nil else { return }
        cursor.replace(); cursor.pause()
        commandTask?.cancel(); commandTask = nil
        fillTask?.cancel(); fillTask = nil
        rollbackOpening(); activating = false; seeking = false
        persist(intent: "stop")
        engaged = false; following = false; followSuspended = true; sleepDeadline = nil
        player.removeAllItems(); nodes.removeAll();itemStatuses.removeAll();failedItems.retain([]); activeItem = nil; activeNode = nil; lastNode = nil
        location = nil; endPending = false; completed = false
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        status = "Listening stopped"; publish(); sync.schedule()
    }
    func pause(interrupted: Bool = false, stop: Bool = false) {
        if stop { stopListening(); return }
        recoveryTask?.cancel();recoveryTask=nil;gapAttempts=0
        guard engaged || pendingPlayback != nil else { return }
        cursor.replace(); cursor.pause(interrupted: interrupted); player.pause()
        rollbackOpening()
        activating = false; seeking = false
        fillTask?.cancel(); fillTask = nil
        if stop { engaged = false; following = false; followSuspended = true }
        persist(intent: stop ? "stop" : "pause"); sync.schedule()
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        status = interrupted ? "Playback interrupted. Tap Play to resume." : "Paused"
        publish()
    }
    func restore() async {
        guard point == nil, player.currentItem == nil, pendingPlayback == nil, !activating else { foreground(); return }
        guard let id = store.journal.activeBook, let saved = store.journal.checkpoints[id] else { return }
        await open(id, section: nil, autoplay: false, localRestore: saved)
    }
    func passiveSelection(_ id: String, section: Int?) {
        guard point == nil, player.currentItem == nil, pendingPlayback == nil, !activating else { foreground(); return }
        if store.journal.checkpoints[id]?.checkpoint_intent == "stop" {return}
        if store.journal.activeBook != nil {commandTask=Task {await self.restore()}}
        else {launchOpen(id,section:section,autoplay:false)}
    }
    func open(_ id: String, section requested: Int?, autoplay: Bool, textOffset: Int? = nil, localRestore: Checkpoint? = nil, resolution: String? = nil, selectedVoice: String? = nil, synthesis: Int? = nil) async {
        guard !Task.isCancelled else { return }
        cancelNarration();trace("open",id)
        recoveryTask?.cancel();recoveryTask=nil;gapAttempts=0
        rollbackOpening()
        persist(); player.pause(); fillTask?.cancel(); fillTask = nil
        pendingPlayback = (point, book, chapter)
        if resolution != nil {
            await sync.task?.value
            guard !Task.isCancelled else {return}
            try? store.update { if $0.inFlight?.book_id == id { $0.inFlight = nil } }
        }
        mediaCommandFloor=ProcessInfo.processInfo.systemUptime
        let generation = cursor.replace(); activating = true; cursor.wanted = autoplay; cursor.state = "buffering"
        player.removeAllItems(); nodes.removeAll();itemStatuses.removeAll();failedItems.retain([]); lastNode = nil; activeItem = nil; activeNode = nil; completed = false; endPending = false; location = nil
        engaged = true; status = "Opening native audiobook…"; publish()
        let target=ListenTarget(book:id,section:requested,offset:textOffset,voice:selectedVoice,synthesis:synthesis)
        do {
            if localRestore==nil {try store.update {$0.pendingListen=target}}
            let local = localRestore ?? store.journal.checkpoints[id]
            // Restoring cached native audio is allowed offline. No new claim is invented offline.
            var remote: ServerProgress
            var online=true
            do { remote = try await backend.progress(id) }
            catch { online=false;guard let local else { throw error }; remote = ServerProgress(); remote.version = local.version; remote.session_id = local.session_id; remote.session_generation = local.session_generation; remote.event_sequence = local.event_sequence; remote.checkpoint_revision = local.version }
            guard cursor.accepts(generation) else { return }
            metadataOffline = !online
            // An explicit book/position command starts a new writer after Stop.
            // Offline restores retain the last known causal owner until it can sync.
            let newWriter=online && localRestore==nil && (requested != nil || textOffset != nil || point?.book_id != id || local?.checkpoint_intent=="stop")
            var useLocal = local != nil
            if let local {
                let policy = CheckpointPolicy.reconcile(local, remote)
                if policy == .conflict && resolution == nil && !newWriter {
                    rollbackOpening()
                    point = local; cursor.pause(interrupted: true); activating = false
                    sync.fencedBooks.insert(id); status = PlaybackError.conflict.localizedDescription; publish(); return
                }
                if policy == .server || policy == .conflict && newWriter || resolution == "server" { useLocal = false }
            }
            if Task.isCancelled { throw CancellationError() }
            status="Loading the selected book…";publish()
            let loadedBook: Book = try await backend.request("/api/books/\(id)",cacheOnly:metadataOffline)
            guard cursor.accepts(generation) else { return }
            book = loadedBook
            let section = requested ?? (useLocal ? local?.section_id : remote.section_id) ?? loadedBook.first_section_id
            guard let section else { throw PlaybackError.unavailable }
            var p: Checkpoint
            if useLocal, let saved = local, resolution != "local" { p = saved }
            else {
                p = Checkpoint(book: id, section: section, asset: "", remote: remote)
                if resolution == "local", let saved = local {
                    p.section_id = saved.section_id; p.asset_id = saved.asset_id; p.seconds = saved.seconds
                    p.segment_index = saved.segment_index; p.segment_seconds = saved.segment_seconds
                    p.voice = saved.voice; p.synthesis_rate = saved.synthesis_rate; p.playback_rate = saved.playback_rate
                } else if requested == nil && remote.section_id == section {
                    p.asset_id = remote.asset_id ?? ""; p.seconds = remote.seconds; p.segment_index = remote.segment_index; p.segment_seconds = remote.segment_seconds
                }
            }
            if newWriter {
                p.session_id=UUID().uuidString;p.session_generation=(remote.session_generation ?? 0)+1;p.event_sequence=1
                p.expected_session_id=remote.session_id ?? "";p.claim_session=true;p.version=remote.version
            }
            if let requested, requested != p.section_id || textOffset != nil {
                p.section_id = requested; p.asset_id = ""; p.seconds = 0; p.segment_index = nil; p.segment_seconds = nil
            }
            if local == nil, remote.section_id == nil, selectedVoice == nil,
               let defaults: PlaybackPreferences = try? await backend.request("/api/edge/preferences") {
                guard cursor.accepts(generation) else { return }
                if let voice = defaults.voice { p.voice = voice }
                p.synthesis_rate = defaults.rate ?? 0
                if let rate = defaults.playback_rate, (0.75...3).contains(rate) { p.playback_rate = rate }
            }
            guard cursor.accepts(generation) else { return }
            if let selectedVoice { p.voice = selectedVoice; p.synthesis_rate = synthesis ?? 0; p.asset_id = ""; p.seconds = 0; p.segment_index = nil; p.segment_seconds = nil }
            status="Loading the selected chapter…";publish()
            let ch = try await backend.chapter(id, p.section_id,cacheOnly:metadataOffline)
            guard cursor.accepts(generation) else { return }
            chapter = ch
            status="Checking available chapter audio…";publish()
            var manifest = try await availableManifest(p, chapter: ch, generation: generation, autoplay: autoplay)
            guard cursor.accepts(generation) else { return }
            if let textOffset, let asset = manifest.asset_id, !asset.hasPrefix("legacy-") {
                let mapped = try await availableStart(asset:asset,offset:textOffset,generation:generation)
                p.seconds = mapped.seconds; p.segment_index = mapped.segment_index; p.segment_seconds = mapped.segment_seconds
                if manifest.usesSegments,mapped.segment_index >= (manifest.segments ?? []).count {
                    p.asset_id=asset
                    manifest=try await availableManifest(p,chapter:ch,generation:generation,autoplay:autoplay,priorManifest:manifest)
                }
            }
            guard cursor.accepts(generation) else { return }
            if !p.asset_id.isEmpty && p.asset_id != manifest.asset_id { throw PlaybackError.invalidSequence }
            if manifest.usesSegments && p.segment_index == nil {
                var value = p.seconds; var index = 0
                let list = manifest.segments ?? []
                while index < list.count - 1 && value >= list[index].duration { value -= list[index].duration; index += 1 }
                p.segment_index = index; p.segment_seconds = value
            }
            if !manifest.usesSegments { p.segment_index = nil; p.segment_seconds = nil }
            status="Preparing native audio…";publish()
            let n = try await prepare(ch, manifest: manifest, index: p.segment_index, point: p)
            guard cursor.accepts(generation) else { return }
            p.asset_id = n.asset; restoreOffset = n.index == nil ? p.seconds : (p.segment_seconds ?? 0)
            let item = item(n); player.insert(item, after: nil)
            seeking = true
            let ok = await player.seek(to: CMTime(seconds: restoreOffset, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
            guard cursor.accepts(generation) else { return }
            guard ok else { throw PlaybackError.unavailable }
            let shouldPlay = cursor.wanted
            if shouldPlay { try activate() }
            try store.update {if $0.pendingListen==target {$0.pendingListen=nil}}
            point = p; pendingPlayback = nil; seeking = false; activating = false
            if resolution != nil || newWriter {sync.fencedBooks.remove(id);try store.update {$0.conflicts.removeValue(forKey:id)}}
            activeItem = item; activeNode = n
            location = n.narration?.location(at: restoreOffset)
            cursor.state = shouldPlay ? "recovering" : "paused"; currentChanged(); persist(intent: textOffset == nil ? "restore" : "reader-start")
            if shouldPlay { player.playImmediately(atRate: Float(p.playback_rate)); status = "Playing with native audio" }
            else { status = "Listening position restored. Tap Play to resume." }
            trace("open-ready");sync.schedule(); publish(); if shouldPlay { fill() }
        } catch {
            guard cursor.accepts(generation) else { return }
            trace("open-failed",error.localizedDescription)
            rollbackOpening()
            activating = false; seeking = false; cursor.pause(); cursor.state = "error"; status = error.localizedDescription
            publish()
        }
    }
    func rollbackOpening() {
        guard let prior = pendingPlayback else { return }
        player.pause(); player.removeAllItems(); nodes.removeAll()
        activeItem = nil; activeNode = nil; lastNode = nil; location = nil
        point = prior.point; book = prior.book; chapter = prior.chapter
        pendingPlayback = nil
    }
    func availableManifest(_ p: Checkpoint, chapter: Chapter, generation: Int, autoplay: Bool, prefetch: Bool = false, priorManifest:Manifest? = nil) async throws -> Manifest {
        var prior: Manifest?=priorManifest
        for attempt in 0..<7 {
            try Task.checkCancellation(); guard cursor.accepts(generation) else { throw CancellationError() }
            let m = try await backend.manifest(p, section: chapter.id,cacheOnly:metadataOffline).validated(after: prior)
            prior = m
            if m.usesSegments, (p.section_id==chapter.id ? (p.segment_index ?? 0):0) < (m.segments ?? []).count { return m }
            if !m.usesSegments,m.status == "ready",m.url != nil {return m}
            if activating {status=m.status=="missing" ? "Preparing missing chapter audio…" : "Waiting for the requested audio chunk to finish generating…";trace("waiting-generation",m.generation_status ?? m.status);publish()}
            if !autoplay || metadataOffline { throw PlaybackError.unavailable }
            if ["failed", "cancelled", "paused"].contains(m.generation_status ?? "") {
                throw HTTPFailure(status:0,code:"generation_"+(m.generation_status ?? "unknown"),message:"Audio generation is \(m.generation_status ?? "unavailable"). Resume or retry it in Jobs, then tap Play.")
            }
            if m.status == "missing" && attempt == 0 { try await backend.generate(p, section: chapter.id, prefetch: prefetch) }
            try await Task.sleep(for: .seconds(min(8, 1 << min(attempt, 3))))
        }
        throw PlaybackError.unavailable
    }
    func availableStart(asset:String,offset:Int,generation:Int) async throws -> StartPosition {
        for attempt in 0..<7 {
            try Task.checkCancellation();guard cursor.accepts(generation) else {throw CancellationError()}
            let mapped:StartPosition=try await backend.request("/api/audio/\(asset)/start?offset=\(offset)",cacheOnly:metadataOffline)
            if mapped.available {return mapped}
            if metadataOffline {throw PlaybackError.unavailable}
            status="Generating audio at the selected reading position…";trace("waiting-reader-offset");publish()
            try await Task.sleep(for:.seconds(min(4,1 << min(attempt,2))))
        }
        throw HTTPFailure(status:0,code:"reader_audio_pending",message:"Audio at the selected reading position is still being prepared. Tap Play to retry this location.")
    }
    func prepare(_ ch: Chapter, manifest m: Manifest, index: Int?, point: Checkpoint) async throws -> Node {
        guard let asset = m.asset_id, !asset.isEmpty else { throw PlaybackError.unavailable }
        var duration = 0.0, start = 0.0, path: String
        if m.usesSegments {
            guard let index, let s = m.segments?[safe: index] else { throw PlaybackError.unavailable }
            duration = s.duration; start = (m.segments ?? []).prefix(index).reduce(0) { $0 + $1.duration }; path = s.url
        } else { guard let url = m.url else { throw PlaybackError.unavailable }; path = url }
        let protected = Set(nodes.values.map(\.file))
        let local = try await files.file(path, protecting: protected)
        let key=asset+":"+String(index ?? -1)
        let narration=index.flatMap {narrationMemo[key] ?? backend.cachedNarration("/api/audio/\(asset)/location/\($0)")}
        let node=Node(chapter:ch,manifest:m,index:m.usesSegments ? index:nil,start:start,duration:duration,file:local,narration:narration)
        if narration==nil,index != nil {queueNarration(node)}
        return node
    }
    func item(_ n: Node) -> AVPlayerItem {
        let item=AVPlayerItem(url:n.file);item.audioTimePitchAlgorithm = .timeDomain
        var value=n;value.narration=value.narration ?? narrationMemo[n.asset+":"+String(n.index ?? -1)]
        let identity=ObjectIdentifier(item);failedItems.remove(identity);nodes[identity]=value
        let failures=failedItems
        itemStatuses[identity]=item.observe(\.status,options:[.initial,.new]) { [weak self,weak item] _,_ in
            guard let item,item.status == .failed else {return}
            failures.insert(ObjectIdentifier(item))
            Task { @MainActor in self?.itemFailed(item) }
        }
        return item
    }
    func itemFailed(_ item:AVPlayerItem) {
        guard engaged,cursor.wanted,!activating,!seeking,let n=nodes[ObjectIdentifier(item)] else {return}
        files.reject(n.file);trace("item-failed",item.error?.localizedDescription ?? "Native item could not decode")
        if player.currentItem===item || activeItem===item {recover(observe:false);return}
        let queued=player.items()
        if let index=queued.firstIndex(where:{$0===item}) {
            // Drop only this failed future item and its successors. Retain the
            // audible item; retry the same missing chunk instead of skipping it.
            for future in queued.dropFirst(index) {player.remove(future)}
            scheduleContinuation(PlaybackError.unavailable,generation:cursor.generation)
        }
    }
    func cancelNarration() {
        narrationWaiting=[]
        for entry in narrationTasks.values {entry.task.cancel()}
        narrationTasks=[:]
    }
    func queueNarration(_ n:Node) {
        let key=n.asset+":"+String(n.index ?? -1)
        guard narrationTasks[key]==nil,!narrationWaiting.contains(where:{$0.asset==n.asset&&$0.index==n.index}) else {return}
        narrationWaiting.append(n);drainNarration()
    }
    func drainNarration() {
        while narrationTasks.count<2,!narrationWaiting.isEmpty {
            let n=narrationWaiting.removeFirst(),id=UUID(),key=n.asset+":"+String(n.index ?? -1)
            guard let index=n.index else {continue}
            let task=Task { [weak self] in
                guard let self else {return}
                defer {if narrationTasks[key]?.id==id {narrationTasks[key]=nil;drainNarration()}}
                do {
                    let value:Narration=try await backend.request("/api/audio/\(n.asset)/location/\(index)")
                    guard !Task.isCancelled else {return}
                    narrationMemo[key]=value
                    if narrationMemo.count>64,let oldest=narrationMemo.keys.first {narrationMemo[oldest]=nil}
                    for (item,var node) in nodes where node.asset==n.asset && node.index==n.index {node.narration=value;nodes[item]=node}
                    if activeNode?.asset==n.asset,activeNode?.index==n.index {activeNode?.narration=value}
                    if lastNode?.asset==n.asset,lastNode?.index==n.index {lastNode?.narration=value}
                    if currentNode?.asset==n.asset,currentNode?.index==n.index {location=value.location(at:player.currentTime().seconds);publish()}
                    trace("timing-ready",key)
                }catch {if !Task.isCancelled {trace("timing-unavailable",error.localizedDescription)}}
            }
            narrationTasks[key]=(id,task)
        }
    }
    func fill() {
        guard fillTask == nil, recoveryTask == nil, cursor.wanted, !activating, player.items().count < 4, let p = point else { return }
        let generation = cursor.generation
        trace("prefetch-start")
        fillTask = Task { [weak self] in
            guard let self else { return }
            defer { if cursor.accepts(generation) { fillTask = nil } }
            do {
                var attempts = 0
                while cursor.accepts(generation), cursor.wanted, !Task.isCancelled, player.items().count < 4 {
                    guard let tail = player.items().last.flatMap({ nodes[ObjectIdentifier($0)] }) ?? lastNode else { return }
                    var ch = tail.chapter, m = tail.manifest, index = tail.index.map { $0 + 1 }
                    if let wantedIndex = index, wantedIndex >= (m.segments ?? []).count && !m.isComplete {
                        m = try await backend.manifest(p, section: ch.id,cacheOnly:metadataOffline).validated(after: tail.manifest)
                        guard cursor.accepts(generation), cursor.wanted else { return }
                        if m.status == "ready" && m.timeline_complete == false { throw PlaybackError.invalidSequence }
                        if wantedIndex >= (m.segments ?? []).count && !m.isComplete {
                            guard attempts < 6 else { throw PlaybackError.unavailable }; attempts += 1
                            try await Task.sleep(for: .seconds(min(8, 1 << min(attempts, 3)))); continue
                        }
                    }
                    let chapterEnd = index == nil || index! >= (m.segments ?? []).count
                    if chapterEnd {
                        guard m.isComplete else { throw PlaybackError.unavailable }
                        // The server may have appended a chapter since this item
                        // was prepared. Check once at the real EOF, without replacing
                        // the audible checkpoint or polling while queued audio plays.
                        let boundaryKey="\(generation):\(ch.id)"
                        if ch.next_id==nil,player.items().isEmpty,!metadataOffline,boundaryRefresh != boundaryKey {
                            ch=try await backend.chapter(p.book_id,ch.id,refresh:true)
                            guard cursor.accepts(generation),cursor.wanted,!Task.isCancelled else {return}
                            boundaryRefresh=boundaryKey;trace("chapter-boundary-refresh")
                        }
                        guard let next = ch.next_id else {
                            if player.items().isEmpty { cursor.wanted = false; cursor.state = "completed"; completed = true; status = "Book completed"; persist(observe: false); publish() }
                            return
                        }
                        ch = try await backend.chapter(p.book_id, next,cacheOnly:metadataOffline)
                        m = try await availableManifest(p, chapter: ch, generation: generation, autoplay: true, prefetch: true)
                        index = m.usesSegments ? 0 : nil
                    }
                    let n = try await prepare(ch, manifest: m, index: index, point: p)
                    guard cursor.accepts(generation), cursor.wanted, !Task.isCancelled else { return }
                    let wasEmpty = player.items().isEmpty
                    let newItem = item(n); player.insert(newItem, after: player.items().last)
                    if wasEmpty {
                        // This item is our validated continuation of a fully consumed
                        // queue. Its predecessor may no longer expose a usable clock.
                        activeItem=nil;activeNode=nil;currentChanged();try activate()
                        player.playImmediately(atRate:Float(point?.playback_rate ?? 1));status="Playing with native audio";trace("queue-resumed")
                    }
                    gapAttempts=0
                    attempts = 0
                    // Release metadata for consumed items; the audio cache has a separate size limit.
                    var live=Set(player.items().map(ObjectIdentifier.init));if let activeItem {live.insert(ObjectIdentifier(activeItem))}
                    nodes=nodes.filter {live.contains($0.key)};itemStatuses=itemStatuses.filter {live.contains($0.key)};failedItems.retain(live)
                }
            } catch is CancellationError {} catch {
                guard cursor.accepts(generation) else { return }
                trace("prefetch-failed",error.localizedDescription)
                scheduleContinuation(error,generation:generation)
                publish()
            }
        }
    }
    func scheduleContinuation(_ error:Error,generation:Int) {
        guard cursor.accepts(generation),cursor.wanted,recoveryTask==nil else {return}
        guard gapAttempts<3 else {
            if !player.items().isEmpty {
                status="Next audio is temporarily unavailable. Cached playback continues; retry is available if it runs out."
                trace("prefetch-budget-reached");return
            }
            cursor.pause();cursor.state="error";status="\(error.localizedDescription) Your place is saved; tap Play to retry or choose another book."
            persist(observe:false);trace("continuation-exhausted");return
        }
        gapAttempts+=1;let attempt=gapAttempts
        status=player.items().isEmpty ? "Loading the next audio segment… (retry \(attempt)/3)" : "Next audio is temporarily unavailable; queued playback continues."
        if player.items().isEmpty {cursor.state="buffering";persist(observe:false)}
        recoveryTask=Task { [weak self] in
            do {try await Task.sleep(for:.seconds(1 << (attempt-1)))}catch{return}
            guard let self,cursor.accepts(generation),cursor.wanted else {return}
            recoveryTask=nil;trace("continuation-retry",String(attempt));fill()
        }
    }
    func activate() throws {
        try AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio, options: [])
        try AVAudioSession.sharedInstance().setActive(true)
    }
    func currentChanged() {
        guard !activating, !seeking, let n = currentNode, var p = point else { return }
        if let previous = activeItem, previous !== player.currentItem, let prior = activeNode {
            let actual = previous.currentTime().seconds
            let expected = prior.duration > 0 ? prior.duration : previous.duration.seconds
            if failedItems.contains(ObjectIdentifier(previous)) || previous.error != nil || previous.status == .failed || !FileManager.default.fileExists(atPath:prior.file.path) {
                // A failed predecessor is not a coalesced successful completion.
                // AVQueuePlayer may already have advanced; do not skip its unheard audio.
                player.pause();files.reject(prior.file);trace("failed-predecessor",previous.error?.localizedDescription ?? "Failed item")
                if cursor.wanted {recover(observe:false)}else{cursor.state="error";publish()}
                return
            }
            if !expected.isFinite || expected <= 0 || !actual.isFinite || actual < expected - 0.35 {
                // AVQueuePlayer can consume several items before an observer is
                // delivered. Removed-item time is not proof that the CURRENT
                // audible item is invalid. Never seek back over live playback.
                let liveTime=player.currentTime().seconds
                if !(player.timeControlStatus == .playing || (liveTime.isFinite && liveTime > 0.05)) {
                    player.pause();cursor.pause();cursor.state="error"
                    status="Queue changed before observed playback. The last audible position is preserved; tap Play to retry."
                    publish();return
                }
            }
        }
        activeItem = player.currentItem; activeNode = n
        let changed = p.section_id != n.chapter.id || p.segment_index != n.index || p.asset_id != n.asset
        chapter = n.chapter; lastNode = n
        if changed {trace("item-transition");lastClock = -1;lastMovement=Date()}
        if changed {
            p.section_id = n.chapter.id; p.asset_id = n.asset; p.segment_index = n.index
            let value=player.currentTime().seconds
            let observed=value.isFinite && value >= 0 ? value : 0
            p.segment_seconds = n.index == nil ? nil : observed; p.seconds = n.start+observed; p.text_anchor = ""; p.normalized_offset = nil
            p.event_sequence += 1; point = p; location = nil
            persist(intent: "chapter-transition", observe: false); sync.schedule()
            fixtureSnapshots.append(["section_id": p.section_id, "segment_index": n.index as Any, "seconds": p.seconds]); if fixtureSnapshots.count > 200 { fixtureSnapshots.removeFirst() }
        }
        cursor.state = cursor.wanted ? "playing" : "paused"
        if cursor.wanted {status="Playing with native audio"}
        endPending = false; publish(); fill()
    }
    func ended(_ item: AVPlayerItem) {
        guard !activating, !seeking, let n = nodes[ObjectIdentifier(item)] else { return }
        // The next item's KVO may arrive before or after this notification.
        if player.currentItem != nil && player.currentItem !== item { currentChanged(); return }
        // Delayed completion of an earlier chunk cannot become a newer event
        // after the current chunk completed or the queue was replaced.
        guard activeItem === item, let p=point, p.section_id==n.chapter.id,
              p.asset_id==n.asset, p.segment_index==n.index else {return}
        let actual = item.currentTime().seconds, expected = n.duration > 0 ? n.duration : item.duration.seconds
        guard expected.isFinite, actual >= expected - 0.35 else { recover(); return }
        lastNode = n; endPending = true
        if var p = point, p.section_id == n.chapter.id {
            p.observe(seconds: n.start + actual, segment: n.index, offset: n.index == nil ? nil : actual, state: "buffering", intent: "playback")
            point = p; persist(observe: false); sync.schedule()
        }
        if cursor.wanted { cursor.state = "buffering"; status = "Waiting for the next published audio chunk…"; fill() }
        publish()
    }
    func tick() {
        guard !activating, !seeking else { return }
        if let deadline = sleepDeadline, Date() >= deadline { sleepDeadline = nil; pause(); return }
        if let item = player.currentItem, item.status == .failed {if let n=currentNode {files.reject(n.file)};trace("item-failed",item.error?.localizedDescription ?? "Unknown decoder error");recover(); return }
        currentChangedIfNeeded()
        guard !activating,!seeking else {return}
        if let n = currentNode {
            let value = player.currentTime().seconds
            if value.isFinite {
                location = n.narration?.location(at: value)
                if cursor.wanted {
                    let priorState=cursor.state
                    cursor.state = isPlaying ? "playing" : "buffering"
                    if cursor.state=="playing",priorState != "playing" {status="Playing with native audio"}
                    if abs(value-lastClock) > 0.01 {lastMovement=Date();lastClock=value}
                    if Date().timeIntervalSince(lastMovement)>12 {trace("stalled-local-item");lastMovement=Date();recover();return}
                }
            }
        }
        if cursor.wanted && player.currentItem==nil {fill()}
        if cursor.wanted && Date().timeIntervalSince(lastPersist) >= 5 { persist() }
        if Date().timeIntervalSince(lastSync) >= 5 { lastSync = Date(); sync.schedule() }
        publish()
    }
    func currentChangedIfNeeded() {
        if let n=currentNode, point?.section_id != n.chapter.id || point?.segment_index != n.index || point?.asset_id != n.asset || activeItem !== player.currentItem {currentChanged()}
    }
    var observedSeconds: Double {
        guard !activating,!seeking,let n=currentNode,let p=point,
              p.section_id==n.chapter.id,p.asset_id==n.asset,p.segment_index==n.index else {return point?.seconds ?? 0}
        let value=player.currentTime().seconds
        return value.isFinite && value>=0 ? n.start+value : p.seconds
    }
    func play() {
        if pendingPlayback != nil { cursor.wanted = true; publish(); return }
        if let target=store.journal.pendingListen {launchOpen(target.book_id,section:target.section_id,autoplay:true,offset:target.normalized_offset,selectedVoice:target.voice,synthesis:target.synthesis_rate);return}
        guard let p = point else { return }
        if sync.fencedBooks.contains(p.book_id) { status = PlaybackError.conflict.localizedDescription; publish(); return }
        recoveryTask?.cancel();recoveryTask=nil;gapAttempts=0
        engaged = true; cursor.wanted = true; cursor.retries = 0;lastMovement=Date();lastClock = -1; completed = false
        if player.currentItem == nil || player.currentItem?.status == .failed || cursor.state == "error" {
            launchOpen(p.book_id, section: nil, autoplay: true); return
        }
        do { try activate(); cursor.state = "recovering"; player.playImmediately(atRate: Float(p.playback_rate)); persist(); sync.schedule(); fill(); publish() }
        catch { status = error.localizedDescription; cursor.pause(); publish() }
    }
    func recover(observe:Bool=true) {
        guard !activating,!seeking else {return}
        guard cursor.retry(), let p = point else {
            player.pause();cursor.pause(interrupted:true);status="Audio recovery stopped. Your place is saved; tap Play to retry."
            persist(observe:false);publish();return
        }
        let retryCount=cursor.retries
        persist(observe:observe);fillTask?.cancel();fillTask=nil;recoveryTask?.cancel();recoveryTask=nil
        cursor.replace();cursor.retries=retryCount
        player.pause();activating=true;status = "Recovering the saved audio position…";trace("item-recovery");publish()
        let generation = cursor.generation, attempts = cursor.retries
        commandTask?.cancel(); commandTask = Task { [weak self] in
            do { try await Task.sleep(for: .seconds(attempts)) } catch { return }
            guard let self, cursor.accepts(generation), cursor.wanted else { return }
            activating=false
            await open(p.book_id, section: nil, autoplay: true)
            cursor.retries = attempts
        }
    }
    func launchOpen(_ id: String, section: Int?, autoplay: Bool, offset: Int? = nil, resolution: String? = nil, selectedVoice:String? = nil, synthesis:Int? = nil) {
        commandTask?.cancel()
        commandTask = Task { [weak self] in await self?.open(id, section: section, autoplay: autoplay, textOffset: offset, resolution: resolution,selectedVoice:selectedVoice,synthesis:synthesis) }
    }
    func seek(to seconds: Double) async {
        guard !Task.isCancelled, engaged, seconds.isFinite, seconds >= 0, let p = point, let current = currentNode, total > 0 else { return }
        let target = min(seconds, max(0, total - 0.02)), wanted = cursor.wanted
        persist(); let generation = cursor.replace(); fillTask?.cancel(); fillTask = nil
        player.pause(); activating = true; seeking = true
        do {
            let list = current.manifest.segments ?? []
            var index: Int? = current.index == nil ? nil : 0, value = target
            if index != nil { while index! < list.count - 1 && value >= list[index!].duration { value -= list[index!].duration; index! += 1 } }
            let n = try await prepare(current.chapter, manifest: current.manifest, index: index, point: p)
            guard cursor.accepts(generation) else { return }
            player.removeAllItems(); nodes.removeAll();itemStatuses.removeAll();failedItems.retain([]); player.insert(item(n), after: nil)
            let ok = await player.seek(to: CMTime(seconds: value, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
            guard cursor.accepts(generation) else { return }
            guard ok else { throw PlaybackError.unavailable }
            var next = p; next.observe(seconds: target, segment: index, offset: index == nil ? nil : value, state: wanted ? "playing" : "paused", intent: "seek")
            point = next; lastNode = n; activeItem = player.currentItem; activeNode = n; location = n.narration?.location(at: value)
            activating = false; seeking = false; endPending = false; cursor.wanted = wanted; cursor.state = wanted ? "playing" : "paused"
            persist(intent: "seek"); sync.schedule()
            if wanted { try activate(); player.playImmediately(atRate: Float(p.playback_rate)); fill() }; publish()
        } catch {
            guard cursor.accepts(generation) else { return }
            activating = false; seeking = false; cursor.pause(); status = error.localizedDescription; publish()
        }
    }
    func skip(_ delta: Double) { let goal = max(0, observedSeconds + delta); commandTask?.cancel(); commandTask = Task { [weak self] in await self?.seek(to: goal) } }
    func rate(_ value: Double) throws {
        guard value.isFinite, (0.75...3).contains(value), point != nil else { throw PlaybackError.invalidCommand }
        point?.playback_rate = value; if cursor.wanted { player.rate = Float(value) }; persist(); sync.schedule(); publish()
    }
    func changeVoice(_ voice: String, synthesis: Int) async throws {
        guard let p = point, voice.count < 100, voice == "legacy" || voice.range(of: "^[A-Za-z0-9-]+$", options: .regularExpression) != nil, (-50...100).contains(synthesis) else { throw PlaybackError.invalidCommand }
        let generation=cursor.generation
        var offset=voice=="legacy" ? nil:location?.normalized_offset
        if voice != "legacy",offset==nil,let n=currentNode,let index=n.index {
            let key=n.asset+":"+String(index)
            if let task=narrationTasks[key]?.task {await task.value}
            guard cursor.accepts(generation),point?.book_id==p.book_id,!Task.isCancelled else {throw CancellationError()}
            offset=location?.normalized_offset
            guard offset != nil else {throw HTTPFailure(status:0,code:"timing_pending",message:"Narration timing is still loading. Please retry the voice change shortly.")}
        }
        let wanted=cursor.wanted;pause()
        await open(p.book_id, section: nil, autoplay: wanted, textOffset: offset, selectedVoice: voice, synthesis: voice == "legacy" ? 0 : synthesis)
    }
    func foreground() { persist(intent: "foreground"); sync.schedule(); publish() }
    func publish() { updateNowPlaying(); onChange?() }
    func updateNowPlaying() {
        for (command, _) in remoteTokens {command.isEnabled = engaged}
        guard engaged, let p = point else { MPNowPlayingInfoCenter.default().nowPlayingInfo = nil; return }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = [MPMediaItemPropertyTitle: chapter?.title ?? "Audiobook", MPMediaItemPropertyAlbumTitle: book?.title ?? "Webnovel", MPMediaItemPropertyPlaybackDuration: total, MPNowPlayingInfoPropertyElapsedPlaybackTime: observedSeconds, MPNowPlayingInfoPropertyPlaybackRate: isPlaying ? p.playback_rate : 0, MPNowPlayingInfoPropertyDefaultPlaybackRate: p.playback_rate, MPNowPlayingInfoPropertyMediaType: MPNowPlayingInfoMediaType.audio.rawValue]
    }
    func installRemoteCommands() {
        let c = MPRemoteCommandCenter.shared()
        func add(_ command: MPRemoteCommand, action: @escaping @MainActor (MPRemoteCommandEvent) -> Void) {
            command.isEnabled = engaged
            let token = command.addTarget { [weak self] event in Task { @MainActor in guard let self,self.engaged,event.timestamp>=self.mediaCommandFloor else {return};action(event) }; return .success }
            remoteTokens.append((command, token))
        }
        add(c.playCommand) { [weak self] _ in self?.play() }
        add(c.pauseCommand) { [weak self] _ in self?.pause() }
        add(c.togglePlayPauseCommand) { [weak self] _ in guard let self else { return }; cursor.wanted ? pause() : play() }
        c.skipBackwardCommand.preferredIntervals = [15]; c.skipForwardCommand.preferredIntervals = [15]
        add(c.skipBackwardCommand) { [weak self] event in self?.skip(-((event as? MPSkipIntervalCommandEvent)?.interval ?? 15)) }
        add(c.skipForwardCommand) { [weak self] event in self?.skip((event as? MPSkipIntervalCommandEvent)?.interval ?? 15) }
        add(c.changePlaybackPositionCommand) { [weak self] event in guard let seconds = (event as? MPChangePlaybackPositionCommandEvent)?.positionTime else { return }; self?.commandTask?.cancel(); self?.commandTask = Task { await self?.seek(to: seconds) } }
        c.changePlaybackRateCommand.supportedPlaybackRates = [1, 1.25, 1.5, 1.75, 2, 2.5]
        add(c.changePlaybackRateCommand) { [weak self] event in guard let rate = (event as? MPChangePlaybackRateCommandEvent)?.playbackRate else { return }; try? self?.rate(Double(rate)) }
        add(c.nextTrackCommand) { [weak self] _ in guard let self, let p = point, let next = chapter?.next_id else { return }; launchOpen(p.book_id, section: next, autoplay: true) }
        add(c.previousTrackCommand) { [weak self] _ in guard let self, let p = point, let previous = chapter?.previous_id else { return }; launchOpen(p.book_id, section: previous, autoplay: true) }
    }
}
extension Array { subscript(safe index: Int) -> Element? { indices.contains(index) ? self[index] : nil } }

// KVO can signal failure before its main-actor callback runs. Remember that fact
// synchronously so a following currentItem callback cannot classify it as EOF.
private final class FailedItems: @unchecked Sendable {
    private let lock=NSLock();private var ids=Set<ObjectIdentifier>()
    func insert(_ id:ObjectIdentifier){lock.lock();ids.insert(id);lock.unlock()}
    func remove(_ id:ObjectIdentifier){lock.lock();ids.remove(id);lock.unlock()}
    func retain(_ live:Set<ObjectIdentifier>){lock.lock();ids.formIntersection(live);lock.unlock()}
    func contains(_ id:ObjectIdentifier)->Bool {lock.lock();defer{lock.unlock()};return ids.contains(id)}
}
