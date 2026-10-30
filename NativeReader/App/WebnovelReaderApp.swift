import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    @Published var controller: ReaderController?
    @Published var failure: String?
    var verification:CheckpointVerification?
    @Published var shareURL: URL?
    init() {
        if ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil { return }
        Task {do {try await configure()}catch {failure="Could not open the listening journal. Existing data was preserved. \(error.localizedDescription)"}}
    }
    func configure() async throws {
            let support = try FileManager.default.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            let arguments=ProcessInfo.processInfo.arguments
            let probeIndex=arguments.firstIndex(of:"--checkpoint-verification")
            let probe=probeIndex != nil
            if let index=probeIndex {
                guard arguments.indices.contains(index+1) else {throw PlaybackError.untrusted}
                try await ProbeFixtureRelay.shared.start(host:arguments[index+1])
            }
            let fixture: Bool
            #if DEBUG
            fixture = probe || ProcessInfo.processInfo.arguments.contains("--fixture")
            #else
            fixture = probe
            #endif
            let origin = try TrustedOrigin(URL(string: fixture ? "http://localhost:18765" : (Bundle.main.object(forInfoDictionaryKey: "NativeBackendURL") as? String ?? "https://reader.example.invalid"))!, allowLoopback: fixture)
            let store = try CheckpointStore(url: support.appendingPathComponent(probe ? "checkpoint-verification-listening.json" : fixture ? "fixture-listening.json" : "listening.json"))
            let backend = Backend(origin: origin)
            let service = try PlaybackService(backend: backend, store: store, cache: support.appendingPathComponent(fixture ? "CheckpointVerificationAudio":"NativeAudio"))
            let c = ReaderController(service: service)
            c.onDownload = { [weak self] in self?.shareURL = $0 }
            controller = c
            if probe {verification=CheckpointVerification(service:service,controller:c,support:support,resume:arguments.contains("--checkpoint-resume"))}
            else {Task {await service.restore();service.sync.schedule()}}
    }
}
@main
struct WebnovelReaderApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var phase
    var body: some Scene {
        WindowGroup {
            Group {
                if let controller = model.controller { ReaderScreen(controller: controller) }
                else { ContentUnavailableView("Listening storage unavailable", systemImage: "externaldrive.badge.exclamationmark", description: Text(model.failure ?? "Unable to open the reader.")) }
            }
            .preferredColorScheme(.dark)
            .sheet(isPresented: Binding(get: { model.shareURL != nil }, set: { if !$0 { model.shareURL = nil } })) { if let url = model.shareURL { ShareSheet(url: url) } }
            .onChange(of: phase) { _, next in
                guard let c=model.controller else {return}
                let s=c.service;c.sceneActive=next == .active
                if next == .active { s.foreground() }
                else { s.persist(); s.sync.schedule() }
            }
        }
    }
}
struct ReaderScreen: View {
    @ObservedObject var controller: ReaderController
    var body: some View {
        ZStack {
            Color(red: 0.09, green: 0.1, blue: 0.12).ignoresSafeArea()
            ReaderWebView(controller: controller)
            if let error = controller.error {
                VStack(spacing: 16) {
                    Image(systemName: "network").font(.largeTitle)
                    Text("Private server unavailable").font(.headline)
                    Text(error).font(.callout).multilineTextAlignment(.center)
                    Button("Retry connection") { controller.error = nil; controller.load() }.buttonStyle(.borderedProminent)
                    Button("Pause native playback") { controller.service.pause() }
                }.padding(28).background(.ultraThickMaterial, in: RoundedRectangle(cornerRadius: 20)).padding()
            }
        }
    }
}
struct ShareSheet: UIViewControllerRepresentable {
    let url: URL
    func makeUIViewController(context: Context) -> UIActivityViewController { UIActivityViewController(activityItems: [url], applicationActivities: nil) }
    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}
