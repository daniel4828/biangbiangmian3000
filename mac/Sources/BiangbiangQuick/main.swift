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
