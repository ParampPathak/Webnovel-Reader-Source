import Foundation
import CryptoKit

struct HTTPFailure: Error, LocalizedError {
    let status: Int; let code: String; let message: String
    var errorDescription: String? { message }
}
final class PrivateSessionDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    let origin: TrustedOrigin
    init(origin: TrustedOrigin) { self.origin = origin }
    func urlSession(_ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(request.url.map(origin.permits) == true ? request : nil)
    }
}
@MainActor
final class Backend {
    let origin: TrustedOrigin
    let session: URLSession
    var onDiagnostic:(([String:Any])->Void)?
    let metadataRoot: URL
    init(origin: TrustedOrigin, configuration: URLSessionConfiguration? = nil) {
        self.origin = origin
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let partition = SHA256.hash(data: Data(origin.url.absoluteString.utf8)).map { String(format: "%02x", $0) }.joined()
        metadataRoot = base.appendingPathComponent("NativeMetadata").appendingPathComponent(partition)
        try? FileManager.default.createDirectory(at: metadataRoot, withIntermediateDirectories: true)
        let config = configuration ?? URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 15; config.timeoutIntervalForResource = 60
        config.httpCookieStorage = .shared
        session = URLSession(configuration: config, delegate: PrivateSessionDelegate(origin: origin), delegateQueue: nil)
    }
    func request<T: Decodable>(_ path: String, method: String = "GET", body: Data? = nil, cacheOnly:Bool = false, allowCachedFallback:Bool = true) async throws -> T {
        let began=Date();var httpStatus=0,byteCount=0,cacheFallback=false,errorCode=0
        let phase=path.contains("/listening") ? (method=="GET" ? "checkpoint-read":"checkpoint-write") : path.contains("/start?") ? "reader-offset" : path.contains("/location/") ? "highlight-timing" : path.contains("/sections/") ? "chapter" : path.contains("/audio/jobs") ? "generation-request" : path.contains("/audio?") ? "audio-manifest" : "book-metadata"
        defer {onDiagnostic?(["phase":phase,"method":method,"http_status":httpStatus,"bytes":byteCount,"cache_fallback":cacheFallback,"cache_only":cacheOnly,"error_code":errorCode,"elapsed_ms":Date().timeIntervalSince(began)*1000])}
        var req = URLRequest(url: try origin.resolve(path)); req.httpMethod = method; req.httpBody = body; req.cachePolicy = .reloadIgnoringLocalCacheData
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if body != nil { req.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        if method != "GET" { req.setValue(origin.url.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")), forHTTPHeaderField: "Origin") }
        let cacheable = method == "GET" && ((path.hasPrefix("/api/books/") && !path.contains("/listening") && !path.contains("/progress")) || path.hasPrefix("/api/audio/"))
        let key = SHA256.hash(data: Data(path.utf8)).map { String(format: "%02x", $0) }.joined()
        let cached = metadataRoot.appendingPathComponent(key)
        if cacheOnly {
            guard cacheable,let prior=try? Data(contentsOf:cached) else {throw URLError(.notConnectedToInternet)}
            cacheFallback=true;byteCount=prior.count
            return try JSONDecoder().decode(T.self,from:prior)
        }
        let data: Data, response: URLResponse
        do { (data, response) = try await session.data(for: req) }
        catch let error as URLError {
            errorCode=error.errorCode
            if allowCachedFallback, cacheable, error.code != .cancelled, let data = try? Data(contentsOf: cached) { cacheFallback=true;return try JSONDecoder().decode(T.self, from: data) }
            throw error
        }
        httpStatus=(response as? HTTPURLResponse)?.statusCode ?? 0;byteCount=data.count
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            if allowCachedFallback,cacheable,let http=response as? HTTPURLResponse,[408,429,500,502,503,504].contains(http.statusCode),
               let prior=try? Data(contentsOf:cached) {cacheFallback=true;return try JSONDecoder().decode(T.self,from:prior)}
            let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            throw HTTPFailure(status: (response as? HTTPURLResponse)?.statusCode ?? 0, code: payload?["code"] as? String ?? "http", message: payload?["error"] as? String ?? "Private server request failed.")
        }
        let decoded = try JSONDecoder().decode(T.self, from: data)
        if cacheable { try? data.write(to: cached, options: .atomic); try? FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: cached.path) }
        return decoded
    }
    func cachedNarration(_ path:String) -> Narration? {
        let key=SHA256.hash(data:Data(path.utf8)).map {String(format:"%02x",$0)}.joined()
        guard let data=try? Data(contentsOf:metadataRoot.appendingPathComponent(key)) else {return nil}
        return try? JSONDecoder().decode(Narration.self,from:data)
    }
    func progress(_ book: String) async throws -> ServerProgress { try await request("/api/books/\(book)/listening") }
    func chapter(_ book: String, _ section: Int, cacheOnly:Bool = false, refresh:Bool = false) async throws -> Chapter { try await request("/api/books/\(book)/sections/\(section)",cacheOnly:cacheOnly,allowCachedFallback:!refresh) }
    func manifest(_ point: Checkpoint, section: Int, cacheOnly:Bool = false) async throws -> Manifest {
        var c = URLComponents(); c.queryItems = [URLQueryItem(name: "section_id", value: String(section)), URLQueryItem(name: "voice", value: point.voice), URLQueryItem(name: "rate", value: String(point.synthesis_rate)), URLQueryItem(name: "provider", value: point.voice == "legacy" ? "legacy" : "edge")]
        return try await request("/api/books/\(point.book_id)/audio?" + (c.percentEncodedQuery ?? ""),cacheOnly:cacheOnly)
    }
    func generate(_ point: Checkpoint, section: Int, prefetch: Bool) async throws {
        guard point.voice != "legacy" else { return }
        let data = try JSONSerialization.data(withJSONObject: ["section_id": section, "voice": point.voice, "rate": point.synthesis_rate, "prefetch": prefetch])
        let _: IgnoredResponse = try await request("/api/books/\(point.book_id)/audio/jobs", method: "POST", body: data)
    }
}
struct PlaybackPreferences: Codable { var voice: String?; var rate: Int?; var playback_rate: Double?; var volume: Double? }
struct IgnoredResponse: Decodable {}
struct Acknowledgement: Decodable {
    let version: Int; let session_id: String; let session_generation: Int; let event_sequence: Int
}

/// Only immutable audio from this origin is cached. At most one transfer is active.
@MainActor
final class AudioFiles {
    let backend: Backend; let root: URL
    struct Transfer {let id:UUID;let task:Task<URL,Error>;var waiters:Set<UUID>}
    var pending:[String:Transfer]=[:]
    var rejected=Set<URL>()
    func reject(_ file:URL) {rejected.insert(file)}
    init(backend: Backend, root: URL) throws {
        self.backend = backend; self.root = root
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    }
    func file(_ path: String, protecting: Set<URL>) async throws -> URL {
        _ = try backend.origin.resolve(path)
        let transfer:Transfer
        if let prior=pending[path],!prior.task.isCancelled {transfer=prior}
        else {transfer=Transfer(id:UUID(),task:Task {try await self.download(path,protecting:protecting)},waiters:[])}
        let ticket=UUID();pending[path]=transfer;pending[path]?.waiters.insert(ticket)
        defer {release(path,transfer:transfer.id,ticket:ticket,cancelled:Task.isCancelled)}
        return try await withTaskCancellationHandler(operation:{
            let result=try await transfer.task.value;try Task.checkCancellation();return result
        },onCancel:{Task { @MainActor in self.release(path,transfer:transfer.id,ticket:ticket,cancelled:true)}})
    }
    private func release(_ path:String,transfer:UUID,ticket:UUID,cancelled:Bool) {
        guard var current=pending[path],current.id==transfer else {return}
        current.waiters.remove(ticket)
        if current.waiters.isEmpty {if cancelled {current.task.cancel()};pending[path]=nil}
        else {pending[path]=current}
    }
    private func download(_ path:String,protecting:Set<URL>) async throws -> URL {
        try Task.checkCancellation()
        let url=try backend.origin.resolve(path)
        let key = SHA256.hash(data: Data(url.absoluteString.utf8)).map { String(format: "%02x", $0) }.joined()
        for ext in ["mp3", "wav", "m4a"] {
            let existing = root.appendingPathComponent(key).appendingPathExtension(ext)
            if FileManager.default.fileExists(atPath: existing.path),!rejected.contains(existing) {backend.onDiagnostic?(["phase":"audio-cache","cache_hit":true]); return existing }
        }
        let began=Date();var httpStatus=0,byteCount=0,ready=false
        defer {backend.onDiagnostic?(["phase":"audio-download","http_status":httpStatus,"bytes":byteCount,"ready":ready,"elapsed_ms":Date().timeIntervalSince(began)*1000])}
        let (temporary, response) = try await backend.session.download(from: url)
        httpStatus=(response as? HTTPURLResponse)?.statusCode ?? 0
        byteCount=(try? FileManager.default.attributesOfItem(atPath:temporary.path)[.size] as? NSNumber)?.intValue ?? 0
        defer { try? FileManager.default.removeItem(at: temporary) }
        guard let http=response as? HTTPURLResponse else {throw PlaybackError.unavailable}
        guard http.statusCode==200 else {throw HTTPFailure(status:http.statusCode,code:"audio_http_\(http.statusCode)",message:"Audio delivery failed (HTTP \(http.statusCode)). Your place is saved.")}
        guard (http.mimeType ?? "").hasPrefix("audio/"),
              let bytes=try FileManager.default.attributesOfItem(atPath:temporary.path)[.size] as? NSNumber,
              bytes.intValue>0,bytes.intValue<64*1024*1024 else {
            throw HTTPFailure(status:http.statusCode,code:"invalid_audio_payload",message:"The server did not return a valid audio file. Your place is saved.")
        }
        let handle = try FileHandle(forReadingFrom: temporary)
        let header = try handle.read(upToCount: 12) ?? Data(); try handle.close()
        let ext = header.starts(with: Data("RIFF".utf8)) ? "wav" : (header.dropFirst(4).starts(with: Data("ftyp".utf8)) ? "m4a" : "mp3")
        if ext=="wav",header.count>=8 {
            let declared=header[4..<8].enumerated().reduce(UInt64(0)) {$0 | UInt64($1.element) << ($1.offset*8)}
            guard declared+8<=bytes.uint64Value else {throw HTTPFailure(status:0,code:"incomplete_audio",message:"The audio response was incomplete; your place is preserved.")}
        }
        try Task.checkCancellation()
        let local = root.appendingPathComponent(key).appendingPathExtension(ext)
        if FileManager.default.fileExists(atPath:local.path) {
            let quarantine=root.appendingPathComponent("RejectedAudio",isDirectory:true)
            try FileManager.default.createDirectory(at:quarantine,withIntermediateDirectories:true)
            try FileManager.default.moveItem(at:local,to:quarantine.appendingPathComponent(UUID().uuidString+"-"+local.lastPathComponent))
        }
        try FileManager.default.copyItem(at: temporary, to: local)
        rejected.remove(local)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: local.path)
        let files = try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: [.fileSizeKey, .contentModificationDateKey,.isRegularFileKey]).filter { (try? $0.resourceValues(forKeys:[.isRegularFileKey]).isRegularFile)==true }.sorted { ((try? $0.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast) < ((try? $1.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast) }
        var total = files.reduce(0) { $0 + ((try? $1.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) }
        for candidate in files where total > 192 * 1024 * 1024 && candidate != local && !protecting.contains(candidate) {
            let size = (try? candidate.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
            try? FileManager.default.removeItem(at: candidate); total -= size
        }
        ready=true;return local
    }
}
