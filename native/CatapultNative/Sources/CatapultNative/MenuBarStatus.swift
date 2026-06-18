import SwiftUI
import AppKit

struct MenuBarStatusLabel: View {
    @ObservedObject var state: AppState
    @Environment(\.openWindow) private var openWindow
    @State private var didOpenMainWindow = false

    var body: some View {
        Label {
            Text(state.menuBarTitle)
        } icon: {
            CatapultMenuBarIcon()
        }
        .task {
            guard !didOpenMainWindow else {
                return
                }
                didOpenMainWindow = true
                openWindow(id: "main")
                NSApp.activate(ignoringOtherApps: true)
            }
    }
}

struct MenuBarStatusMenu: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Text("Catapult")
            .font(.headline)

        Text(state.menuBarStatusDetail)
            .foregroundStyle(.secondary)

        if let appleID = state.signedInAppleID {
            Text(appleID)
                .foregroundStyle(.secondary)
        }

        Divider()

        Text(state.menuBarAutoRefreshDetail)

        if let info = state.accountInfo {
            Text("\(info.apps.filter { !$0.extensionSlot }.count) apps · \(info.appCount)/\(info.appLimit) App IDs")
                .foregroundStyle(.secondary)
        }

        Divider()

        Button {
            openWindow(id: "main")
            NSApp.activate(ignoringOtherApps: true)
        } label: {
            Label("Open Catapult", systemImage: CatapultIcon.appLogo)
        }

        Button {
            Task { await state.refreshDevices() }
        } label: {
            Label("Refresh Devices", systemImage: CatapultIcon.refresh)
        }
        .disabled(state.backend.status != .ready || state.isRefreshingDevices)

        Button {
            Task { await state.reloadAccountInfo() }
        } label: {
            Label("Reload Account", systemImage: CatapultIcon.account)
        }
        .disabled(!state.isAuthenticated || state.isLoadingAccountInfo)

        Button {
            Task { await state.copyDiagnosticsToClipboard() }
        } label: {
            Label("Copy Diagnostics", systemImage: CatapultIcon.copy)
        }
        .disabled(state.backend.status != .ready)

        Divider()

        Button {
            NSApp.terminate(nil)
        } label: {
            Label("Quit Catapult", systemImage: CatapultIcon.signOut)
        }
        .keyboardShortcut("q")
    }
}

extension AppState {
    var menuBarTitle: String {
        switch backend.status {
        case .ready:
            let due = eligibleAutoRefreshCount
            if due > 0 {
                return due == 1 ? "Catapult due" : "Catapult \(due) due"
            }
            if let next = nextAutoRefreshDate {
                return "Catapult \(Self.compactRelativeTime(until: next))"
            }
            return "Catapult"
        case .starting:
            return "Catapult ..."
        case .failed:
            return "Catapult !"
        case .stopped:
            return "Catapult off"
        }
    }

    var menuBarStatusDetail: String {
        switch backend.status {
        case .ready:
            return "Engine ready"
        case .starting:
            return "Starting engine"
        case .failed(let message):
            return "Engine failed: \(message)"
        case .stopped:
            return "Engine stopped"
        }
    }

    var menuBarAutoRefreshDetail: String {
        guard backend.status == .ready else {
            return "Auto-refresh waits for the local engine."
        }
        guard isAuthenticated else {
            return "Auto-refresh waits for Apple ID sign-in."
        }
        guard accountInfo != nil else {
            return "Auto-refresh status loading..."
        }
        let due = eligibleAutoRefreshCount
        if due > 0 {
            return due == 1 ? "1 app is eligible for auto-refresh now." : "\(due) apps are eligible for auto-refresh now."
        }
        if let next = nextAutoRefreshDate {
            return "Next auto-refresh window starts \(Self.fullDateFormatter.string(from: next))."
        }
        return "No saved app is scheduled for auto-refresh."
    }

    private var eligibleAutoRefreshCount: Int {
        accountInfo?.apps.filter {
            !$0.extensionSlot && $0.autoRefreshEligible == true
        }.count ?? 0
    }

    private var nextAutoRefreshDate: Date? {
        accountInfo?.apps.compactMap { app in
            guard !app.extensionSlot,
                  app.autoRefreshEligible != true,
                  let raw = app.autoRefreshAfter else {
                return nil
            }
            return Self.parseISODate(raw)
        }.min()
    }

    private static func parseISODate(_ value: String) -> Date? {
        if let date = isoDateFormatter.date(from: value) {
            return date
        }
        return fractionalISODateFormatter.date(from: value)
    }

    private static func compactRelativeTime(until date: Date) -> String {
        let seconds = max(0, Int(date.timeIntervalSinceNow.rounded(.down)))
        if seconds == 0 {
            return "now"
        }
        let days = seconds / 86_400
        let hours = (seconds % 86_400) / 3_600
        let minutes = (seconds % 3_600) / 60
        if days > 0 {
            return "\(days)d\(hours)h"
        }
        if hours > 0 {
            return "\(hours)h\(minutes)m"
        }
        return "\(max(1, minutes))m"
    }

    private static let isoDateFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    private static let fractionalISODateFormatter: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let fullDateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "MMM d, yyyy h:mm a z"
        formatter.locale = .current
        formatter.timeZone = .current
        return formatter
    }()
}
