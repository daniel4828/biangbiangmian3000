# Biang – iOS-App

Native Hülle (SwiftUI + WKWebView) um die Web-App `https://powerdaniel3000.duckdns.org`.
Keine Abhängigkeiten. Änderungen an der Web-App sind sofort in der App – nur Änderungen
an dieser Hülle brauchen eine neue Installation.

Was die App mehr kann als Safari:
- Login bleibt (Cookie in eigenem, dauerhaftem Speicher)
- Audio startet ohne extra Tippen; läuft bei gesperrtem Bildschirm und Stummschalter weiter
- Nach unten ziehen = neu laden, von links wischen = zurück
- Links auf andere Seiten öffnen in Safari

## Auf das iPhone installieren (einmalig, ca. 5 Minuten)

1. iPhone per Kabel an den Mac, entsperren, „Diesem Computer vertrauen“.
2. `ios/Biang.xcodeproj` in Xcode öffnen (Doppelklick).
3. Links das Projekt „Biang“ → Target „Biang“ → Tab **Signing & Capabilities**
   → bei **Team** deine Apple-ID wählen (ggf. „Add an Account…“).
   Falls der Bundle Identifier schon vergeben ist: `eu.danielschreiber.biang` leicht ändern.
4. Oben in der Leiste dein iPhone als Ziel wählen → ▶ (Cmd+R).
5. Beim ersten Mal auf dem iPhone: Einstellungen → Allgemein → VPN & Geräteverwaltung
   → Entwickler-App vertrauen. Außerdem muss der **Entwicklermodus** an sein
   (Einstellungen → Datenschutz & Sicherheit → Entwicklermodus).

Mit kostenloser Apple-ID läuft die Signatur nach **7 Tagen** ab → dann einfach in Xcode
nochmal ▶ drücken. Mit bezahltem Developer-Account (99 €/Jahr) hält sie ein Jahr, und
TestFlight wäre möglich.

## Bauen ohne Xcode-Oberfläche

```bash
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project ios/Biang.xcodeproj -scheme Biang -sdk iphonesimulator \
  CODE_SIGNING_ALLOWED=NO build
```
