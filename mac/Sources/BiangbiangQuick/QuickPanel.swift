// Schwebendes Fenster mit WKWebView, das <Server>/quick lädt. Die Seite meldet ihre Höhe über den
// Message-Handler "quick"; die Unterkante des Fensters bleibt fest (ein Drittel über dem unteren
// Bildschirmrand), es wächst nach oben – die Eingabezeile sitzt unten (#1520).
import AppKit
import WebKit

/// Randloses Fenster, das Tastatureingaben annimmt, ohne die vordere App zu wechseln.
final class KeyPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override func cancelOperation(_ sender: Any?) { (delegate as? QuickPanelController)?.hide() }

    /// Menüleisten-App ohne Bearbeiten-Menü, und das Fenster aktiviert die App nicht: ⌘V & Co.
    /// erreichen das WKWebView sonst nie. Daher die Kurzbefehle hier selbst in die Responder-Kette schicken.
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        guard event.type == .keyDown, flags.contains(.command), !flags.contains(.option), !flags.contains(.control),
              let key = event.charactersIgnoringModifiers?.lowercased() else {
            return super.performKeyEquivalent(with: event)
        }
        let shift = flags.contains(.shift)
        let action: String?
        switch key {
        case "x" where !shift: action = "cut:"
        case "c" where !shift: action = "copy:"
        case "v" where !shift: action = "paste:"
        case "v": action = "pasteAsPlainText:"
        case "a" where !shift: action = "selectAll:"
        case "z": action = shift ? "redo:" : "undo:"
        default: action = nil
        }
        // Nicht NSApp.sendAction(to: nil): Das sucht über NSApp.keyWindow, und das ist bei einem
        // nichtaktivierenden Panel oft nil, weil die App selbst nie aktiv wird. Stattdessen direkt
        // beim Ersthelfer dieses Fensters anfangen und die Responder-Kette hochlaufen.
        if let action {
            let selector = NSSelectorFromString(action)
            var responder: NSResponder? = firstResponder
            while let current = responder {
                if current.responds(to: selector) {
                    current.perform(selector, with: self)
                    return true
                }
                responder = current.nextResponder
            }
        }
        return super.performKeyEquivalent(with: event)
    }
}

/// Position und Größe des Fensters; im Menü „Fenster…“ änderbar.
enum PanelSettings {
    static let bottomKey = "panelBottomPercent"
    static let topKey = "panelTopPercent"
    static let heightKey = "panelMaxHeight"
    static let widthKey = "panelWidth"
    static let defaultPercent: Double = 12.5
    static let defaultWidth: Double = 580

    /// Abstand der Unterkante zum unteren Bildschirmrand, in % der Bildschirmhöhe.
    static var bottomPercent: Double { percent(bottomKey) }
    /// Abstand, den das Fenster beim Wachsen zum oberen Rand mindestens lässt, in %.
    static var topPercent: Double { percent(topKey) }

    private static func percent(_ key: String) -> Double {
        guard let value = UserDefaults.standard.object(forKey: key) as? NSNumber else { return defaultPercent }
        return min(max(value.doubleValue, 0), 45)
    }
}

enum ServerSettings {
    static let key = "serverURL"
    static let defaultURL = "https://biangbiangmian3000.danielschreiber.eu"

    static var base: String {
        let stored = UserDefaults.standard.string(forKey: key) ?? ""
        return stored.isEmpty ? defaultURL : stored
    }

    static var quickURL: URL? { URL(string: base + "/quick") }

    /// Zuletzt benutzte Sprache. Die Seite merkt sie sich auch in localStorage; hier zusätzlich,
    /// damit sie nicht verloren geht, wenn WebKit seinen Speicher leert.
    static let langKey = "quickLang"
    static var lastLang: String? {
        guard let value = UserDefaults.standard.string(forKey: langKey),
              !value.isEmpty, value.allSatisfy({ $0.isASCII && $0.isLetter }) else { return nil }
        return value
    }
}

@MainActor
final class QuickPanelController: NSObject, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate {
    private static let defaultWidth = CGFloat(PanelSettings.defaultWidth)
    private static let heightKey = PanelSettings.heightKey
    private static let widthKey = PanelSettings.widthKey
    private static let minWidth: CGFloat = 360
    private static let minHeight: CGFloat = 60
    /// Vom Nutzer gezogene Größe, gilt beim nächsten Öffnen als Höchstmaß.

    private let panel: KeyPanel
    private let webView: WKWebView
    /// Sichtbarer Bereich des aktuellen Bildschirms.
    private var area = NSRect(x: 0, y: 0, width: 1440, height: 900)
    /// Unterkante (Bildschirmkoordinaten), auf der das Fenster steht.
    private var bottom: CGFloat = 0
    private var width: CGFloat = defaultWidth
    private var maxHeight: CGFloat = 400
    /// Höhe, die die Seite zuletzt gemeldet hat.
    private var contentHeight: CGFloat = minHeight
    /// Direkt nach dem Ziehen behält das Fenster die gezogene Höhe, bis sich der Inhalt ändert
    /// (neue Nachricht, neuer Chat) – dann gilt wieder: Inhaltshöhe, höchstens maxHeight (#1524).
    private var pinned = false
    private var resizeMonitor: Any?

    var isVisible: Bool { panel.isVisible }

    override init() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = WKWebsiteDataStore.default()
        let controller = WKUserContentController()
        config.userContentController = controller
        webView = WKWebView(frame: .zero, configuration: config)
        webView.underPageBackgroundColor = .windowBackgroundColor
        webView.autoresizingMask = [.width, .height]

        panel = KeyPanel(contentRect: NSRect(x: 0, y: 0, width: Self.defaultWidth, height: Self.minHeight),
                         styleMask: [.nonactivatingPanel, .borderless, .fullSizeContentView],
                         backing: .buffered, defer: true)
        super.init()
        controller.add(self, name: "quick")
        webView.navigationDelegate = self
        webView.uiDelegate = self

        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.hidesOnDeactivate = false
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.delegate = self

        let container = NSView(frame: NSRect(x: 0, y: 0, width: Self.defaultWidth, height: Self.minHeight))
        container.wantsLayer = true
        container.layer?.cornerRadius = 14
        container.layer?.masksToBounds = true
        webView.frame = container.bounds
        container.addSubview(webView)
        panel.contentView = container

        reload()
    }

    // MARK: Laden

    func reload() {
        guard let url = ServerSettings.quickURL else {
            webView.loadHTMLString("<body style='font:14px -apple-system;padding:20px'>Ungültige Server-Adresse.</body>", baseURL: nil)
            return
        }
        webView.load(URLRequest(url: url))
    }

    // MARK: Anzeigen

    func toggle() {
        if panel.isVisible { hide() } else { show() }
    }

    func show() {
        let mouse = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
        guard let visible = screen?.visibleFrame else { return }
        area = visible
        // Unterkante ein Achtel über dem unteren Rand; wachsen bis ein Achtel unter dem oberen.
        bottom = area.minY + area.height * CGFloat(PanelSettings.bottomPercent / 100)
        let defaults = UserDefaults.standard
        let storedHeight = CGFloat(defaults.double(forKey: Self.heightKey))
        let storedWidth = CGFloat(defaults.double(forKey: Self.widthKey))
        maxHeight = clampHeight(storedHeight > 0 ? storedHeight : .greatestFiniteMagnitude)
        width = clampWidth(storedWidth > 0 ? storedWidth : Self.defaultWidth)
        pinned = false
        applyFrame()
        panel.makeKeyAndOrderFront(nil)
        panel.makeFirstResponder(webView)
        webView.evaluateJavaScript("window.quickFocus && window.quickFocus()", completionHandler: nil)
    }

    func hide() {
        endResize()
        panel.orderOut(nil)
    }

    func windowDidResignKey(_ notification: Notification) { hide() }

    private func clampHeight(_ value: CGFloat) -> CGFloat {
        min(max(value, Self.minHeight), max(Self.minHeight, area.maxY - area.height * CGFloat(PanelSettings.topPercent / 100) - bottom))
    }

    private func clampWidth(_ value: CGFloat) -> CGFloat {
        min(max(value, Self.minWidth), max(Self.minWidth, area.width))
    }

    /// Unterkante fest, horizontal zentriert; Höhe = Inhalt, höchstens maxHeight.
    private func applyFrame() {
        let height = pinned ? maxHeight : min(max(contentHeight, Self.minHeight), maxHeight)
        let frame = NSRect(x: area.midX - width / 2, y: bottom, width: width, height: height)
        panel.setFrame(frame, display: true, animate: false)
    }

    private func setContentHeight(_ value: CGFloat) {
        if abs(value - contentHeight) >= 1 { pinned = false }
        contentHeight = value
        if resizeMonitor == nil { applyFrame() }
    }

    // MARK: Größe ziehen

    /// Die Seite meldet mousedown auf einem Griff (obere Kante / obere Ecken); ab hier verfolgen wir die Maus.
    private func beginResize(edge: String) {
        guard resizeMonitor == nil, NSEvent.pressedMouseButtons & 1 != 0 else { return }
        let startMouse = NSEvent.mouseLocation
        let startHeight = panel.frame.height
        let startWidth = width
        let horizontal: CGFloat = edge == "topleft" ? -1 : (edge == "topright" ? 1 : 0)
        resizeMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDragged, .leftMouseUp]) { [weak self] event in
            guard let self else { return event }
            if event.type == .leftMouseUp {
                self.endResize(save: true)
                return event
            }
            let mouse = NSEvent.mouseLocation
            self.maxHeight = self.clampHeight(startHeight + (mouse.y - startMouse.y))
            // Zentriert: Ziehen an einer Ecke verbreitert auf beiden Seiten.
            self.width = self.clampWidth(startWidth + 2 * horizontal * (mouse.x - startMouse.x))
            self.pinned = true
            self.applyFrame()
            return nil
        }
    }

    private func endResize(save: Bool = false) {
        if let monitor = resizeMonitor {
            NSEvent.removeMonitor(monitor)
            resizeMonitor = nil
        }
        guard save else { return }
        UserDefaults.standard.set(Double(maxHeight), forKey: Self.heightKey)
        UserDefaults.standard.set(Double(width), forKey: Self.widthKey)
    }

    // MARK: Nachrichten der Seite

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        switch type {
        case "height":
            if let number = body["value"] as? NSNumber { setContentHeight(CGFloat(truncating: number)) }
        case "resizeStart":
            beginResize(edge: body["edge"] as? String ?? "top")
        case "close":
            hide()
        case "lang":
            if let value = body["value"] as? String, value.allSatisfy({ $0.isASCII && $0.isLetter }) {
                UserDefaults.standard.set(value, forKey: ServerSettings.langKey)
            }
        default:
            break
        }
    }

    // MARK: Navigation

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard let path = webView.url?.path else { return }
        if path == "/login" {
            // Das Anmeldeformular braucht Platz, die Seite meldet dort keine Höhe.
            setContentHeight(maxHeight)
        } else if path == "/quick", let lang = ServerSettings.lastLang {
            webView.evaluateJavaScript("window.quickSetLang && window.quickSetLang('\(lang)')", completionHandler: nil)
        } else if path == "/" || path.isEmpty {
            // Nach der Anmeldung leitet der Server auf die ganze App weiter: zurück zur Schnellseite.
            reload()
        }
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        showLoadError(error)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        showLoadError(error)
    }

    private func showLoadError(_ error: Error) {
        // Abgebrochene Navigation (z. B. reload() während einer Weiterleitung) ist kein Fehler.
        if (error as NSError).code == NSURLErrorCancelled { return }
        let message = error.localizedDescription
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
        webView.loadHTMLString("<body style='font:14px -apple-system;padding:20px;color:gray'>Laden fehlgeschlagen: \(message)<br><br>Menü → „Neu laden“ oder „Server-Adresse…“.</body>", baseURL: nil)
    }

    // Links mit target=_blank im Standardbrowser öffnen.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
        return nil
    }
}
