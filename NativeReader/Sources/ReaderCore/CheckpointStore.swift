import Foundation
#if canImport(Darwin)
import Darwin
#else
import Glibc
#endif

public struct ListenTarget: Codable, Equatable, Sendable {
    public var book_id:String
    public var section_id:Int?
    public var normalized_offset:Int?
    public var voice:String?
    public var synthesis_rate:Int?
    public init(book:String,section:Int?,offset:Int?,voice:String?=nil,synthesis:Int?=nil) {
        book_id=book;section_id=section;normalized_offset=offset;self.voice=voice;synthesis_rate=synthesis
    }
}

public struct Journal: Codable, Sendable {
    public var activeBook: String?
    // Failed preparation is an intent, never an audible checkpoint.
    public var pendingListen:ListenTarget?
    public var checkpoints: [String: Checkpoint] = [:]
    // An immutable in-flight event survives a lost acknowledgement and process death.
    public var inFlight: Checkpoint?
    public var acknowledged: [String: Int] = [:]
    public var conflicts: [String: ServerProgress] = [:]
    public init() {}
    enum CodingKeys: String,CodingKey {case activeBook,pendingListen,checkpoints,inFlight,acknowledged,conflicts}
    public init(from decoder: Decoder) throws {
        let c=try decoder.container(keyedBy:CodingKeys.self)
        pendingListen=try c.decodeIfPresent(ListenTarget.self,forKey:.pendingListen)
        activeBook=try c.decodeIfPresent(String.self,forKey:.activeBook)
        checkpoints=try c.decodeIfPresent([String:Checkpoint].self,forKey:.checkpoints) ?? [:]
        inFlight=try c.decodeIfPresent(Checkpoint.self,forKey:.inFlight)
        acknowledged=try c.decodeIfPresent([String:Int].self,forKey:.acknowledged) ?? [:]
        conflicts=try c.decodeIfPresent([String:ServerProgress].self,forKey:.conflicts) ?? [:]
    }
}
public final class CheckpointStore {
    public let url: URL
    private var contents: Journal
    private let lock=NSRecursiveLock()
    public var journal: Journal {lock.lock();defer{lock.unlock()};return contents}
    public init(url: URL) throws {
        self.url = url
        if FileManager.default.fileExists(atPath: url.path) {
            contents = try JSONDecoder().decode(Journal.self, from: Data(contentsOf: url))
        } else { contents = Journal() }
    }
    public func update(_ change: (inout Journal) -> Void) throws {
        lock.lock();defer{lock.unlock()}
        var next = contents; change(&next)
        for (book,old) in contents.checkpoints {
            guard let candidate=next.checkpoints[book],candidate.session_id==old.session_id,
                  candidate.session_generation==old.session_generation else {continue}
            guard candidate.event_sequence>=old.event_sequence,candidate.version>=old.version else {throw PlaybackError.invalidSequence}
            if candidate.event_sequence==old.event_sequence {
                var acknowledged=old;acknowledged.version=candidate.version;acknowledged.claim_session=candidate.claim_session
                guard acknowledged==candidate else {throw PlaybackError.invalidSequence}
            }
        }
        try Self.write(try JSONEncoder().encode(next),to:url)
        contents = next
    }
    public static func write(_ data:Data,to url:URL) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let temporary=url.deletingLastPathComponent().appendingPathComponent(".checkpoint-"+UUID().uuidString)
        defer {try? FileManager.default.removeItem(at:temporary)}
        var attributes:[FileAttributeKey:Any]=[.posixPermissions:0o600]
        #if os(iOS)
        attributes[.protectionKey]=FileProtectionType.completeUntilFirstUserAuthentication
        try FileManager.default.setAttributes([.protectionKey:FileProtectionType.completeUntilFirstUserAuthentication],ofItemAtPath:url.deletingLastPathComponent().path)
        #endif
        guard FileManager.default.createFile(atPath:temporary.path,contents:nil,attributes:attributes) else {throw PlaybackError.storage}
        let file=try FileHandle(forWritingTo:temporary)
        do {try file.write(contentsOf:data);try file.synchronize();try file.close()}
        catch {try? file.close();throw error}
        guard rename(temporary.path,url.path)==0 else {throw PlaybackError.storage}
        let directory=open(url.deletingLastPathComponent().path,O_RDONLY)
        if directory>=0 {defer{close(directory)};_ = fsync(directory)}
    }
}

public struct TrustedOrigin: Sendable {
    public let url: URL
    public init(_ url: URL, allowLoopback: Bool = false) throws {
        guard url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              url.path.isEmpty || url.path == "/", url.scheme == "https" || (allowLoopback && url.scheme == "http" && ["localhost", "127.0.0.1"].contains(url.host ?? "")) else { throw PlaybackError.untrusted }
        self.url = url
    }
    public func permits(_ candidate: URL) -> Bool {
        candidate.scheme == url.scheme && candidate.host == url.host && (candidate.port ?? (candidate.scheme == "https" ? 443 : 80)) == (url.port ?? (url.scheme == "https" ? 443 : 80)) && candidate.user == nil && candidate.password == nil
    }
    public func resolve(_ path: String) throws -> URL {
        guard let result = URL(string: path, relativeTo: url)?.absoluteURL, permits(result) else { throw PlaybackError.untrusted }
        return result
    }
}
