// Biangbiang Quick: Menüleisten-App. ⌥⇧K öffnet in jeder App das schwebende Wörterbuch-Fenster.
import AppKit
import Carbon.HIToolbox
import ServiceManagement

/// Der Carbon-Callback ist ein C-Funktionszeiger und kann nichts einfangen: Ziel liegt global.
nonisolated(unsafe) var hotKeyAction: (@MainActor () -> Void)?

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var statusItem: NSStatusItem?
    private var panel: QuickPanelController?
    private var hotKeyRef: EventHotKeyRef?
    private var hotKeyFailed = false
    private let loginItem = NSMenuItem(title: "Beim Login starten", action: #selector(toggleLogin), keyEquivalent: "")

    func applicationDidFinishLaunching(_ notification: Notification) {
        let panel = QuickPanelController()
        self.panel = panel
        hotKeyAction = { [weak panel] in panel?.toggle() }
        registerHotKey()
        setupStatusItem()
    }

    // MARK: Kurzbefehl

    private func registerHotKey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
            Task { @MainActor in hotKeyAction?() }
            return noErr
        }, 1, &spec, nil, nil)
        let id = EventHotKeyID(signature: OSType(0x42514B31), id: 1)
        let status = RegisterEventHotKey(UInt32(kVK_ANSI_K), UInt32(optionKey | shiftKey), id,
                                         GetApplicationEventTarget(), 0, &hotKeyRef)
        hotKeyFailed = status != noErr
    }

    // MARK: Menü

    private func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let image = NSImage(systemSymbolName: "character.book.closed", accessibilityDescription: "Biangbiang Quick") {
            item.button?.image = image
        } else {
            item.button?.title = "辞"
        }
        let menu = NSMenu()
        menu.delegate = self
        if hotKeyFailed {
            let warning = NSMenuItem(title: "⚠ ⌥⇧K belegt", action: nil, keyEquivalent: "")
            warning.isEnabled = false
            menu.addItem(warning)
        }
        menu.addItem(withTitle: "Öffnen (⌥⇧K)", action: #selector(openPanel), keyEquivalent: "").target = self
        loginItem.target = self
        menu.addItem(loginItem)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Fenster…", action: #selector(editPanel), keyEquivalent: ",").target = self
        menu.addItem(withTitle: "Server-Adresse…", action: #selector(editServer), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Neu laden", action: #selector(reloadPanel), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Beenden", action: #selector(quit), keyEquivalent: "q").target = self
        item.menu = menu
        statusItem = item
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        loginItem.state = SMAppService.mainApp.status == .enabled ? .on : .off
    }

    @objc private func openPanel() {
        // Das Menü schließt sich erst nach der Aktion; danach Fokus an das Fenster geben.
        DispatchQueue.main.async { [weak self] in
            guard let panel = self?.panel, !panel.isVisible else { return }
            panel.show()
        }
    }

    @objc private func reloadPanel() { panel?.reload() }

    @objc private func quit() { NSApp.terminate(nil) }

    @objc private func toggleLogin() {
        let service = SMAppService.mainApp
        do {
            if service.status == .enabled { try service.unregister() } else { try service.register() }
        } catch {
            NSApp.activate(ignoringOtherApps: true)
            let alert = NSAlert()
            alert.messageText = "Login-Start konnte nicht geändert werden"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }

    /// Position und Größe des Fensters. Leeres Feld = Standard; gilt beim nächsten Öffnen.
    @objc private func editPanel() {
        NSApp.activate(ignoringOtherApps: true)
        let defaults = UserDefaults.standard
        func number(_ key: String) -> String {
            guard let value = defaults.object(forKey: key) as? NSNumber, value.doubleValue > 0 else { return "" }
            return String(format: "%g", value.doubleValue.rounded())
        }
        func percent(_ key: String) -> String {
            guard let value = defaults.object(forKey: key) as? NSNumber else { return "" }
            return String(format: "%g", value.doubleValue)
        }
        let rows: [(String, String, String)] = [
            ("Abstand unten (% der Bildschirmhöhe)", percent(PanelSettings.bottomKey), String(format: "%g", PanelSettings.defaultPercent)),
            ("Abstand oben beim Wachsen (%)", percent(PanelSettings.topKey), String(format: "%g", PanelSettings.defaultPercent)),
            ("Breite (px)", number(PanelSettings.widthKey), String(format: "%g", PanelSettings.defaultWidth)),
            ("Höchste Höhe (px)", number(PanelSettings.heightKey), "bis zum oberen Abstand"),
        ]
        let stack = NSStackView()
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 6
        var fields: [NSTextField] = []
        for (label, value, placeholder) in rows {
            stack.addArrangedSubview(NSTextField(labelWithString: label))
            let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
            field.stringValue = value
            field.placeholderString = "Standard: \(placeholder)"
            field.widthAnchor.constraint(equalToConstant: 260).isActive = true
            stack.addArrangedSubview(field)
            fields.append(field)
        }
        stack.frame = NSRect(x: 0, y: 0, width: 260, height: 4 * 50)
        let alert = NSAlert()
        alert.messageText = "Fenster"
        alert.informativeText = "Leeres Feld = Standard. Gilt beim nächsten Öffnen (⌥⇧K). Ziehen am oberen Rand ändert Breite und Höhe ebenfalls."
        alert.accessoryView = stack
        alert.addButton(withTitle: "Speichern")
        alert.addButton(withTitle: "Abbrechen")
        alert.addButton(withTitle: "Alles zurücksetzen")
        alert.window.initialFirstResponder = fields[0]
        let keys = [PanelSettings.bottomKey, PanelSettings.topKey, PanelSettings.widthKey, PanelSettings.heightKey]
        switch alert.runModal() {
        case .alertThirdButtonReturn:
            keys.forEach { defaults.removeObject(forKey: $0) }
        case .alertFirstButtonReturn:
            var values: [Double?] = []
            for field in fields {
                let text = field.stringValue.trimmingCharacters(in: .whitespaces).replacingOccurrences(of: ",", with: ".")
                if text.isEmpty { values.append(nil); continue }
                guard let value = Double(text), value >= 0 else {
                    let error = NSAlert()
                    error.messageText = "Ungültige Zahl: \(field.stringValue)"
                    error.runModal()
                    return
                }
                values.append(value)
            }
            if let bottom = values[0], let top = values[1], bottom + top > 90 {
                let error = NSAlert()
                error.messageText = "Abstand unten + oben ist zu groß"
                error.informativeText = "Zusammen höchstens 90 %."
                error.runModal()
                return
            }
            for (key, value) in zip(keys, values) {
                if let value { defaults.set(value, forKey: key) } else { defaults.removeObject(forKey: key) }
            }
        default:
            return
        }
    }

    @objc private func editServer() {
        NSApp.activate(ignoringOtherApps: true)
        let alert = NSAlert()
        alert.messageText = "Server-Adresse"
        alert.informativeText = "Standard: \(ServerSettings.defaultURL)"
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 360, height: 24))
        field.stringValue = ServerSettings.base
        alert.accessoryView = field
        alert.addButton(withTitle: "Speichern")
        alert.addButton(withTitle: "Abbrechen")
        alert.window.initialFirstResponder = field
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        var text = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        while text.hasSuffix("/") { text.removeLast() }
        guard let url = URL(string: text), ["http", "https"].contains(url.scheme ?? ""), url.host != nil else {
            let error = NSAlert()
            error.messageText = "Ungültige Adresse"
            error.informativeText = "Beispiel: https://example.com"
            error.runModal()
            return
        }
        UserDefaults.standard.set(text, forKey: ServerSettings.key)
        panel?.reload()
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    withExtendedLifetime(delegate) { app.run() }
}
