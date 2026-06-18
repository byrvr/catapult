import SwiftUI
import AppKit

@main
struct CatapultNativeApp: App {
    @StateObject private var state = AppState()

    var body: some Scene {
        WindowGroup("Catapult", id: "main") {
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
                .task {
                    await state.start()
                }
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
