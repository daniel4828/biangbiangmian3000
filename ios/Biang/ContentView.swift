import SwiftUI

struct ContentView: View {
    @StateObject private var model = WebModel()

    var body: some View {
        ZStack {
            Color(uiColor: .systemBackground).ignoresSafeArea()
            WebView(model: model)
            if let error = model.loadError {
                VStack(spacing: 16) {
                    Text("Keine Verbindung").font(.headline)
                    Text(error).font(.footnote).foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                    Button("Erneut versuchen") { model.reload() }
                        .buttonStyle(.borderedProminent)
                }
                .padding(32)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
                .padding()
            }
        }
    }
}
