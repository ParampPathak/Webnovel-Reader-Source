import Foundation

public struct ServerProgress: Codable, Sendable {
    public var version: Int = 0
    public var section_id: Int?
    public var asset_id: String?
    public var seconds: Double = 0
    public var segment_index: Int?
    public var segment_seconds: Double?
    public var playback_rate: Double = 1
    public var voice: String = "en-US-AndrewNeural"
    public var synthesis_rate: Int = 0
    public var session_id: String?
    public var session_generation: Int?
    public var event_sequence: Int?
    public var checkpoint_revision: Int?
    public init() {}
    // Missing fields are normal for a book with no listening history.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Keys.self)
        version = try c.decodeIfPresent(Int.self, forKey: .version) ?? 0
        section_id = try c.decodeIfPresent(Int.self, forKey: .section_id)
        asset_id = try c.decodeIfPresent(String.self, forKey: .asset_id)
        seconds = try c.decodeIfPresent(Double.self, forKey: .seconds) ?? 0
        segment_index = try c.decodeIfPresent(Int.self, forKey: .segment_index)
        segment_seconds = try c.decodeIfPresent(Double.self, forKey: .segment_seconds)
        playback_rate = try c.decodeIfPresent(Double.self, forKey: .playback_rate) ?? 1
        voice = try c.decodeIfPresent(String.self, forKey: .voice) ?? "en-US-AndrewNeural"
        synthesis_rate = try c.decodeIfPresent(Int.self, forKey: .synthesis_rate) ?? 0
        session_id = try c.decodeIfPresent(String.self, forKey: .session_id)
        session_generation = try c.decodeIfPresent(Int.self, forKey: .session_generation)
        event_sequence = try c.decodeIfPresent(Int.self, forKey: .event_sequence)
        checkpoint_revision = try c.decodeIfPresent(Int.self, forKey: .checkpoint_revision)
    }
    enum Keys: String, CodingKey { case version, section_id, asset_id, seconds, segment_index, segment_seconds, playback_rate, voice, synthesis_rate, session_id, session_generation, event_sequence, checkpoint_revision }
    public func encode(to encoder: Encoder) throws { var c = encoder.container(keyedBy: Keys.self); try c.encode(version, forKey: .version); try c.encodeIfPresent(section_id, forKey: .section_id); try c.encodeIfPresent(asset_id, forKey: .asset_id); try c.encode(seconds, forKey: .seconds); try c.encodeIfPresent(segment_index, forKey: .segment_index); try c.encodeIfPresent(segment_seconds, forKey: .segment_seconds); try c.encode(playback_rate, forKey: .playback_rate); try c.encode(voice, forKey: .voice); try c.encode(synthesis_rate, forKey: .synthesis_rate); try c.encodeIfPresent(session_id, forKey: .session_id); try c.encodeIfPresent(session_generation, forKey: .session_generation); try c.encodeIfPresent(event_sequence, forKey: .event_sequence); try c.encodeIfPresent(checkpoint_revision, forKey: .checkpoint_revision) }
}

public struct Checkpoint: Codable, Equatable, Sendable {
    public var book_id: String
    public var section_id: Int
    public var asset_id: String
    public var seconds: Double
    public var segment_index: Int?
    public var segment_seconds: Double?
    public var playback_rate: Double
    public var voice: String
    public var synthesis_rate: Int
    public var version: Int
    public var session_id: String
    public var session_generation: Int
    public var event_sequence: Int
    public var claim_session: Bool
    public var expected_session_id: String
    public var checkpoint_intent: String
    public var playback_state: String
    public var text_anchor: String = ""
    public var normalized_offset: Int?
    public init(book: String, section: Int, asset: String, remote: ServerProgress, session: String = UUID().uuidString) {
        book_id = book; section_id = section; asset_id = asset; seconds = 0
        playback_rate = remote.playback_rate; voice = remote.voice; synthesis_rate = remote.synthesis_rate
        version = remote.version; session_id = session; session_generation = (remote.session_generation ?? 0) + 1
        event_sequence = 1; claim_session = true; expected_session_id = remote.session_id ?? ""
        checkpoint_intent = "restore"; playback_state = "paused"
    }
    public mutating func observe(seconds: Double, segment: Int?, offset: Double?, state: String, intent: String) {
        guard seconds.isFinite, seconds >= 0, offset == nil || (offset!.isFinite && offset! >= 0) else { return }
        self.seconds = seconds; segment_index = segment; segment_seconds = offset
        playback_state = state; checkpoint_intent = intent; event_sequence += 1
    }
}

public enum Reconciliation: Equatable { case local, server, conflict }
public enum CheckpointPolicy {
    public static func reconcile(_ local: Checkpoint, _ remote: ServerProgress) -> Reconciliation {
        let owner = remote.session_id ?? "", generation = remote.session_generation ?? 0
        if owner == local.session_id && generation == local.session_generation {
            guard remote.checkpoint_revision == remote.version else { return .conflict }
            return local.event_sequence >= (remote.event_sequence ?? 0) ? .local : .server
        }
        if local.claim_session && owner == local.expected_session_id && generation + 1 == local.session_generation {
            // An unfenced older client may have written while this client was offline.
            if owner.isEmpty && remote.version != local.version { return .conflict }
            return .local
        }
        return .conflict
    }
    public static func canRebase(_ event: Checkpoint, on remote: ServerProgress) -> Bool {
        reconcile(event, remote) == .local
    }
}

public struct Chapter: Codable, Equatable, Sendable {
    public var id: Int; public var title: String; public var next_id: Int?; public var previous_id: Int?
    public init(id: Int, title: String, next: Int? = nil, previous: Int? = nil) { self.id = id; self.title = title; next_id = next; previous_id = previous }
}
public struct Book: Codable, Sendable { public var id: String; public var title: String; public var first_section_id: Int?; public var cover_url: String? }
public struct Segment: Codable, Equatable, Sendable {
    public var index: Int; public var duration: Double; public var url: String
    public init(index: Int, duration: Double, url: String) { self.index = index; self.duration = duration; self.url = url }
}
public struct Manifest: Codable, Sendable {
    public var status: String; public var asset_id: String?; public var url: String?
    public var segments: [Segment]?; public var complete: Bool?; public var timeline_complete: Bool?
    public var generation_status: String?; public var job_id: String?; public var error: String?
    public init(status: String, asset: String, segments: [Segment], complete: Bool) { self.status = status; asset_id = asset; self.segments = segments; self.complete = complete }
    public var usesSegments: Bool { !(segments ?? []).isEmpty && (status != "ready" || timeline_complete != false) }
    public var isComplete: Bool { complete == true || status == "ready" }
    public func validated(after prior: Manifest? = nil) throws -> Manifest {
        let list = segments ?? []
        guard list.enumerated().allSatisfy({ $0.offset == $0.element.index && $0.element.duration.isFinite && $0.element.duration > 0 && !$0.element.url.isEmpty }) else { throw PlaybackError.invalidSequence }
        if let prior {
            guard prior.asset_id == asset_id, list.count >= (prior.segments ?? []).count,
                  Array(list.prefix((prior.segments ?? []).count)) == (prior.segments ?? []) else { throw PlaybackError.invalidSequence }
        }
        return self
    }
}
public enum PlaybackError: Error, LocalizedError {
    case invalidSequence, unavailable, conflict, untrusted, storage, invalidCommand
    public var errorDescription: String? {
        switch self {
        case .invalidSequence: "Published audio sequence changed. Your position is preserved."
        case .unavailable: "Audio is unavailable. Tap Play to retry at the saved position."
        case .conflict: "Another player owns listening progress. Your local position is preserved. Use Resume server position to switch, or Keep local position to take ownership explicitly."
        case .untrusted: "Only the configured private server is trusted."
        case .storage: "Could not durably save listening progress. Playback paused."
        case .invalidCommand: "Invalid playback command."
        }
    }
}
public struct Cue: Codable, Sendable { public var at: Double; public var offset: Int; public var quote: String }
public struct Narration: Codable, Sendable {
    public var book_id: String; public var section_id: Int; public var segment_index: Int
    public var quote: String; public var anchor: String; public var fraction: Double; public var precision: String
    public var normalized_offset: Int?; public var cues: [Cue]?
    public var before: String?; public var after: String?
    public func location(at seconds: Double) -> Narration {
        guard let indexed = cues.map { Array($0.enumerated()) }?.last(where: { $0.element.at <= seconds }) else { var copy = self; copy.cues = nil; return copy }
        var copy = self; copy.quote = indexed.element.quote; copy.anchor += ":word:\(indexed.offset)"
        copy.normalized_offset = (normalized_offset ?? 0) + indexed.element.offset; copy.cues = nil; copy.precision = "word"
        // Match the web player's context windows using the metadata's UTF-16 offsets.
        // The in-book renderer still receives exactly the same word and anchor.
        let units = Array(quote.utf16), start = indexed.element.offset
        let end = start + indexed.element.quote.utf16.count
        if start >= 0, end <= units.count {
            copy.before = String(decoding: units[max(0, start - 140)..<start], as: UTF16.self)
            copy.after = String(decoding: units[end..<min(units.count, end + 160)], as: UTF16.self)
        }
        return copy
    }
}
public struct StartPosition: Codable, Sendable { public var segment_index: Int; public var segment_seconds: Double; public var seconds: Double; public var available: Bool }

public struct QueueCursor: Equatable, Sendable {
    public var generation: Int = 0
    public var wanted = false
    public var state = "paused"
    public var retries = 0
    public init() {}
    @discardableResult public mutating func replace() -> Int { generation += 1; retries = 0; return generation }
    public func accepts(_ token: Int) -> Bool { generation == token }
    public mutating func pause(interrupted: Bool = false) { wanted = false; state = interrupted ? "interrupted" : "paused" }
    public mutating func retry() -> Bool { guard wanted, retries < 3 else { return false }; retries += 1; state = "recovering"; return true }
}
