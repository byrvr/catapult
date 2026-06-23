import SwiftUI
import AppKit

@main
struct CatapultNativeApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        Window("Catapult", id: "main") {
            ContentView()
                .environmentObject(state)
                .frame(minWidth: 860, minHeight: 620)
                .task {
                    await state.start()
                }
        }
        MenuBarExtra {
            MenuBarStatusMenu()
                .environmentObject(state)
        } label: {
            MenuBarStatusLabel(state: state)
        }
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("Catapult") {
                Button("Refresh Devices") {
                    Task { await state.refreshDevices() }
                }
                .keyboardShortcut("r", modifiers: [.command])
            }
        }
    }
}
