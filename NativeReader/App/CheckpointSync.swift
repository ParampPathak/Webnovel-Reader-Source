import Foundation

@MainActor
final class CheckpointSync {
    let backend: Backend; let store: CheckpointStore
    var task: Task<Void, Never>?
    var onConflict: ((Checkpoint) -> Void)?
    var onNotice: ((String) -> Void)?
    var fencedBooks = Set<String>()
    init(backend: Backend, store: CheckpointStore) { self.backend = backend; self.store = store }
    func schedule() {
        guard task == nil else { return }
        task = Task { [weak self] in
            guard let self else { return }
            defer { self.task = nil }
            do {
                while !Task.isCancelled {
                    let journal = store.journal
                    let next = journal.inFlight ?? journal.checkpoints.values.filter { !fencedBooks.contains($0.book_id) && $0.event_sequence > (journal.acknowledged[$0.book_id] ?? 0) }.sorted { $0.book_id < $1.book_id }.first
                    guard var event = next else { return }
                    if let latest=journal.checkpoints[event.book_id],
                       latest.session_id != event.session_id || latest.session_generation != event.session_generation {
                        try store.update {$0.inFlight=nil};continue
                    }
                    if fencedBooks.contains(event.book_id) { return }
                    try store.update { $0.inFlight = event }
                    var ack: Acknowledgement?
                    for attempt in 0..<2 {
                        do { ack = try await backend.request("/api/books/\(event.book_id)/listening", method: "PUT", body: JSONEncoder().encode(event)); break }
                        catch let error as HTTPFailure where error.status == 409 {
                            let remote = try await backend.progress(event.book_id)
                            if let latest=store.journal.checkpoints[event.book_id],latest.session_id != event.session_id || latest.session_generation != event.session_generation {
                                try store.update {$0.inFlight=nil};break
                            }
                            // Exact lost-ack retries are acknowledged by Flask's immutable-event
                            // idempotency branch. A 409 with a reused sequence is never an ack.
                            guard attempt == 0, error.code == "version_conflict", CheckpointPolicy.canRebase(event, on: remote) else {
                                fencedBooks.insert(event.book_id)
                                try store.update { $0.inFlight = nil;$0.conflicts[event.book_id]=remote }
                                onConflict?(event); return
                            }
                            event.version = remote.version
                            try store.update { $0.inFlight = event }
                        }
                    }
                    guard let ack, ack.session_id == event.session_id, ack.session_generation == event.session_generation, ack.event_sequence == event.event_sequence else { throw PlaybackError.conflict }
                    try store.update { journal in
                        journal.inFlight = nil
                        if var latest = journal.checkpoints[event.book_id], latest.session_id == event.session_id, latest.session_generation==event.session_generation {
                            latest.version = max(latest.version,ack.version); latest.claim_session = false
                            journal.checkpoints[event.book_id] = latest
                            journal.acknowledged[event.book_id] = max(journal.acknowledged[event.book_id] ?? 0,event.event_sequence)
                        }
                    }
                }
            } catch { onNotice?("Saved on iPhone; server sync pending: \(error.localizedDescription)") }
        }
    }
}
