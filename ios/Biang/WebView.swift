import SwiftUI
import WebKit

/// The whole app is the web app; this is the native shell around it.
enum AppConfig {
    static let homeURL = URL(string: "https://powerdaniel3000.duckdns.org/")!
    static var appHost: String { homeURL.host ?? "" }
}

@MainActor
final class WebModel: ObservableObject {
    @Published var loadError: String?
    weak var webView: WKWebView?

    func reload() {
        loadError = nil
        if let webView, webView.url != nil {
            webView.reload()
        } else {
            webView?.load(URLRequest(url: AppConfig.homeURL))
        }
    }
}

struct WebView: UIViewRepresentable {
    @ObservedObject var model: WebModel

    func makeCoordinator() -> Coordinator { Coordinator(model: model) }

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        // Persistent store: the one-year login cookie survives app restarts.
        config.websiteDataStore = .default()
        config.allowsInlineMediaPlayback = true
        // TTS plays without an extra tap (Safari demands a gesture per element).
        config.mediaTypesRequiringUserActionForPlayback = []
        config.applicationNameForUserAgent = "BiangApp/1.0"

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        webView.allowsBackForwardNavigationGestures = true
        webView.isInspectable = true
        webView.scrollView.contentInsetAdjustmentBehavior = .automatic
        webView.isOpaque = false
        webView.backgroundColor = .systemBackground

        let refresh = UIRefreshControl()
        refresh.addTarget(context.coordinator, action: #selector(Coordinator.pullToRefresh(_:)),
                          for: .valueChanged)
        webView.scrollView.refreshControl = refresh

        model.webView = webView
        webView.load(URLRequest(url: AppConfig.homeURL))
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {}

    @MainActor
    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let model: WebModel
        init(model: WebModel) { self.model = model }

        @objc func pullToRefresh(_ sender: UIRefreshControl) {
            model.reload()
            sender.endRefreshing()
        }

        // Links to other sites open in Safari, the app stays on its own host.
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
            guard let url = action.request.url else { return decisionHandler(.allow) }
            let scheme = url.scheme?.lowercased() ?? ""
            if ["http", "https"].contains(scheme) {
                if url.host == AppConfig.appHost || action.targetFrame?.isMainFrame == false {
                    return decisionHandler(.allow)
                }
                if action.navigationType == .linkActivated || action.targetFrame == nil {
                    UIApplication.shared.open(url)
                    return decisionHandler(.cancel)
                }
                return decisionHandler(.allow)
            }
            if ["about", "blob", "data"].contains(scheme) { return decisionHandler(.allow) }
            UIApplication.shared.open(url)   // mailto:, tel:, spotify:, …
            decisionHandler(.cancel)
        }

        // target="_blank" / window.open
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if let url = action.request.url {
                if url.host == AppConfig.appHost { webView.load(action.request) }
                else { UIApplication.shared.open(url) }
            }
            return nil
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            model.loadError = nil
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: Error) {
            show(error)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            show(error)
        }

        private func show(_ error: Error) {
            let ns = error as NSError
            if ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled { return }
            model.loadError = error.localizedDescription
        }

        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            webView.reload()
        }

        // JS alert/confirm (the web app uses confirm() in a few places)
        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo,
                     completionHandler: @escaping @MainActor () -> Void) {
            present(message, cancel: false) { _ in completionHandler() }
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo,
                     completionHandler: @escaping @MainActor (Bool) -> Void) {
            present(message, cancel: true, completion: completionHandler)
        }

        // Microphone for the search mic button: grant inside the app's own host.
        func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                     initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                     decisionHandler: @escaping @MainActor (WKPermissionDecision) -> Void) {
            decisionHandler(origin.host == AppConfig.appHost ? .grant : .prompt)
        }

        private func present(_ message: String, cancel: Bool, completion: @escaping (Bool) -> Void) {
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            if cancel {
                alert.addAction(UIAlertAction(title: "Abbrechen", style: .cancel) { _ in completion(false) })
            }
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in completion(true) })
            guard let root = UIApplication.shared.connectedScenes
                .compactMap({ ($0 as? UIWindowScene)?.keyWindow?.rootViewController }).first
            else { return completion(false) }
            var top = root
            while let presented = top.presentedViewController { top = presented }
            top.present(alert, animated: true)
        }
    }
}
