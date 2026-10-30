import SwiftUI
import WebKit

@MainActor
final class ReaderController: NSObject, ObservableObject, WKNavigationDelegate, WKScriptMessageHandlerWithReply, WKUIDelegate, WKDownloadDelegate {
    let service: PlaybackService
    @Published var error: String?
    var webView: WKWebView!
    var revision = 0
    var sceneActive = true {
        didSet {if sceneActive && !oldValue {webView?.evaluateJavaScript("window.dispatchEvent(new Event('native-reader-foreground'))",completionHandler:nil)}}
    }
    var evaluating = false
    var publishPending = false
    var documentGeneration = 0
    var downloadURL: URL?
    var onDownload: ((URL) -> Void)?
    init(service: PlaybackService) {
        self.service = service; super.init()
        let content = WKUserContentController()
        content.addScriptMessageHandler(self, contentWorld: .page, name: "reader")
        let js = (try? String(contentsOf: Bundle.main.url(forResource: "NativeBridge", withExtension: "js")!, encoding: .utf8)) ?? ""
        content.addUserScript(WKUserScript(source: js, injectionTime: .atDocumentStart, forMainFrameOnly: false))
        let config = WKWebViewConfiguration(); config.userContentController = content
        config.mediaTypesRequiringUserActionForPlayback = .all; config.allowsInlineMediaPlayback = false
        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self; webView.uiDelegate = self
        webView.isInspectable = true; webView.scrollView.contentInsetAdjustmentBehavior = .never
        service.onChange = { [weak self] in self?.publish() }
    }
    func load() {
        let host = NSRegularExpression.escapedPattern(for: service.backend.origin.url.host ?? "")
        let rules = """
        [{"trigger":{"url-filter":"^https?://\(host.replacingOccurrences(of: "\\", with: "\\\\"))(:[0-9]+)?/static/player\\\\.js([?].*)?$"},"action":{"type":"block"}}]
        """
        WKContentRuleListStore.default().compileContentRuleList(forIdentifier: "NativeAudioOnly-v1", encodedContentRuleList: rules) { [weak self] list, error in
            Task { @MainActor in
                guard let self else { return }
                guard let list else { self.error = "Could not disable the browser audio engine: \(error?.localizedDescription ?? "unknown error")"; return }
                self.webView.configuration.userContentController.add(list)
                self.webView.load(URLRequest(url: self.service.backend.origin.url))
            }
        }
    }
    func snapshot() -> [String: Any] {
        revision += 1
        let s = service, p = s.point
        var result: [String: Any] = ["revision": revision, "native": true, "engaged": s.engaged, "playing": s.isPlaying, "intent": s.cursor.wanted, "state": s.cursor.state, "status": s.status, "following": s.following, "follow_suspended": s.followSuspended, "seconds": s.observedSeconds, "total": s.total, "title": s.book?.title ?? "", "chapter_title": s.chapter?.title ?? "", "playback_rate": p?.playback_rate ?? 1, "volume": s.volume, "sync_conflict": p.map { s.sync.fencedBooks.contains($0.book_id) } ?? false]
        result["retry_target_available"] = s.store.journal.pendingListen != nil
        result["asset_id"] = p?.asset_id
        result["book_id"] = p?.book_id; result["section_id"] = p?.section_id; result["segment_index"] = p?.segment_index
        result["voice"] = p?.voice; result["synthesis_rate"] = p?.synthesis_rate
        result["session_id"] = p?.session_id; result["event_sequence"] = p?.event_sequence
        result["next_id"] = s.chapter?.next_id; result["previous_id"] = s.chapter?.previous_id; result["cover_url"] = s.book?.cover_url
        result["sleep_deadline"] = s.sleepDeadline?.timeIntervalSince1970
        if let n = s.location, let data = try? JSONEncoder().encode(n) { result["narration"] = try? JSONSerialization.jsonObject(with: data) }
        return result
    }
    func publish() {
        guard sceneActive,!evaluating else {publishPending=true;return}
        guard let data = try? JSONSerialization.data(withJSONObject: snapshot()), let json = String(data: data, encoding: .utf8) else { return }
        evaluating=true;publishPending=false
        let generation=documentGeneration
        webView.evaluateJavaScript("window.NativeReader?.receive(\(json))") { [weak self] _,_ in
            guard let self,generation==self.documentGeneration else {return}
            self.evaluating=false
            if self.publishPending {self.publish()}
        }
    }
    func webView(_ webView:WKWebView,didStartProvisionalNavigation navigation:WKNavigation!) {
        documentGeneration+=1;evaluating=false;publishPending=true
    }
    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage, replyHandler: @escaping (Any?, String?) -> Void) {
        let origin = message.frameInfo.securityOrigin
        guard message.frameInfo.isMainFrame,
              origin.protocol == service.backend.origin.url.scheme,
              origin.host == service.backend.origin.url.host,
              (origin.port == 0 ? (origin.protocol == "https" ? 443 : 80) : origin.port) == (service.backend.origin.url.port ?? (origin.protocol == "https" ? 443 : 80)),
              let url = message.frameInfo.request.url, service.backend.origin.permits(url),
              let body = message.body as? [String: Any], let command = body["command"] as? String,
              (try? JSONSerialization.data(withJSONObject: body).count) ?? Int.max < 8192 else { replyHandler(nil, "Untrusted native bridge message."); return }
        Task { @MainActor in
            do {
                switch command {
                case "state": service.foreground()
                case "restore-selection":
                    guard let id=body["book_id"] as? String, validID(id) else {throw PlaybackError.invalidCommand}
                    let section=integer(body["section_id"])
                    if let section, section<1 {throw PlaybackError.invalidCommand}
                    service.passiveSelection(id,section:section)
                case "listen":
                    guard let id = body["book_id"] as? String, validID(id) else { throw PlaybackError.invalidCommand }
                    let section = integer(body["section_id"]), offset = integer(body["normalized_offset"])
                    if let section, section < 1 { throw PlaybackError.invalidCommand }
                    if let offset, offset < 0 { throw PlaybackError.invalidCommand }
                    let autoplay = body["autoplay"] as? Bool ?? true
                    if id == service.point?.book_id, section == nil, offset == nil, service.store.journal.pendingListen == nil, !service.sync.fencedBooks.contains(id) { if autoplay { service.play() } }
                    else {
                        service.launchOpen(id, section: section, autoplay: autoplay, offset: offset)
                        // Explicit reader starts keep their JS transition guard
                        // until native preparation/seek settles. Transport controls
                        // still return immediately and can cancel the operation.
                        if offset != nil {
                            await service.commandTask?.value
                            if service.point?.book_id != id || service.cursor.state=="error" {
                                throw HTTPFailure(status:0,code:"native_preparation",message:service.status)
                            }
                        }
                    }
                case "toggle": service.cursor.wanted ? service.pause() : service.play()
                case "pause": service.pause()
                case "stop": service.stopListening()
                case "skip": guard let value = number(body["seconds"]), abs(value) <= 3600 else { throw PlaybackError.invalidCommand }; service.skip(value)
                case "seek": guard let value = number(body["seconds"]), (0...86400).contains(value) else { throw PlaybackError.invalidCommand }; service.commandTask?.cancel(); service.commandTask = Task { await self.service.seek(to: value) }
                case "rate": guard let value = number(body["value"]) else { throw PlaybackError.invalidCommand }; try service.rate(value)
                case "volume": guard let value = number(body["value"]), (0...1).contains(value) else { throw PlaybackError.invalidCommand }; service.volume = Float(value)
                case "sleep": guard let minutes = integer(body["minutes"]), (0...120).contains(minutes) else { throw PlaybackError.invalidCommand }; service.sleepDeadline = minutes == 0 ? nil : Date().addingTimeInterval(Double(minutes * 60))
                case "follow": service.following = body["enabled"] as? Bool ?? true; service.followSuspended = body["suspended"] as? Bool ?? false
                case "chapter": guard let p = service.point, let direction = body["direction"] as? String, ["next", "previous"].contains(direction), let id = direction == "next" ? service.chapter?.next_id : service.chapter?.previous_id else { throw PlaybackError.invalidCommand }; service.launchOpen(p.book_id, section: id, autoplay: true)
                case "voice": guard let voice = body["voice"] as? String, let rate = integer(body["rate"]) else { throw PlaybackError.invalidCommand }; service.commandTask?.cancel(); service.commandTask = Task { do { try await self.service.changeVoice(voice, synthesis: rate) } catch { self.service.status = error.localizedDescription; self.service.publish() } }
                case "resolve": guard let id = service.point?.book_id, let resolution = body["resolution"] as? String, ["local", "server"].contains(resolution) else { throw PlaybackError.invalidCommand }; service.launchOpen(id, section: nil, autoplay: false, resolution: resolution)
                default: throw PlaybackError.invalidCommand
                }
                replyHandler(snapshot(), nil)
            } catch { replyHandler(["error": error.localizedDescription], nil) }
        }
    }
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String, initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        guard let url = frame.request.url, service.backend.origin.permits(url),
              var presenter = webView.window?.rootViewController else { completionHandler(false); return }
        while let presented = presenter.presentedViewController { presenter = presented }
        let alert = UIAlertController(title: "Webnovel", message: message, preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in completionHandler(false) })
        alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completionHandler(true) })
        presenter.present(alert, animated: true)
    }
    func validID(_ id: String) -> Bool { !id.isEmpty && id.count <= 100 && id.range(of: "^[A-Za-z0-9_-]+$", options: .regularExpression) != nil }
    func number(_ value: Any?) -> Double? { guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite else { return nil }; return n.doubleValue }
    func integer(_ value: Any?) -> Int? { guard let n = number(value), n.rounded() == n, abs(n) <= 1_000_000_000 else { return nil }; return Int(n) }
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if service.backend.origin.permits(url) { decisionHandler(navigationAction.shouldPerformDownload ? .download : .allow) }
        else if navigationAction.targetFrame?.isMainFrame == false && ["about", "blob"].contains(url.scheme ?? "") { decisionHandler(.allow) }
        else { decisionHandler(.cancel) }
    }
    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        error = nil
        webView.configuration.websiteDataStore.httpCookieStore.getAllCookies { cookies in for cookie in cookies { HTTPCookieStorage.shared.setCookie(cookie) } }
        publish()
    }
    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) { self.error = "Connect Tailscale to your private network, then retry. \(error.localizedDescription)" }
    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) { webView.reload() }
    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) { decisionHandler(navigationResponse.canShowMIMEType ? .allow : .download) }
    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appendingPathComponent(URL(fileURLWithPath: suggestedFilename).lastPathComponent)
        downloadURL = url; completionHandler(url)
    }
    func downloadDidFinish(_ download: WKDownload) { if let url = downloadURL { onDownload?(url) } }
}
struct ReaderWebView: UIViewRepresentable {
    let controller: ReaderController
    func makeUIView(context: Context) -> WKWebView { controller.load(); return controller.webView }
    func updateUIView(_ uiView: WKWebView, context: Context) {}
}
