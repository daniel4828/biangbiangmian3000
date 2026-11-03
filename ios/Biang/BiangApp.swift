import SwiftUI
import AVFoundation

@main
struct BiangApp: App {
    init() {
        // Lets story/TTS audio keep playing with the screen locked or the
        // silent switch on (needs UIBackgroundModes = audio in Info.plist).
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
        try? AVAudioSession.sharedInstance().setActive(true)
    }

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}
