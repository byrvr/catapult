import SwiftUI
import AppKit

/// Cross-device sync setup.
///
/// The whole point is that a second Mac needs exactly one thing: the recovery
/// key. Mac #1 copies it, Mac #2 pastes it — Universal Clipboard usually
/// carries it across, since both Macs are already on the same Apple Account.
struct SyncSettingsView: View {
    @EnvironmentObject private var state: AppState

    @State private var recoveryKeyToShow: String?
    @State private var savedAcknowledged = false
    @State private var enteredKey = ""
    @State private var busy = false
    @State private var message: String?
    @State private var isError = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                storageSection
                Divider()
                vaultSection
                Divider()
                wakeSection
                if let message {
                    Label(message, systemImage: isError ? CatapultIcon.warning : CatapultIcon.ready)
                        .font(.caption)
                        .foregroundStyle(isError ? Color.orange : Color.secondary)
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .task { await state.loadSyncStatus() }
        .sheet(item: Binding(
            get: { recoveryKeyToShow.map(IdentifiedKey.init) },
            set: { if $0 == nil { recoveryKeyToShow = nil } }
        )) { identified in
            RecoveryKeySheet(key: identified.value, acknowledged: $savedAcknowledged) {
                recoveryKeyToShow = nil
                savedAcknowledged = false
            }
        }
    }

    // MARK: - Where the vault lives

    private var storageSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Vault location")
                .font(.headline)
            Text("Catapult keeps an encrypted copy of your IPAs so another Mac can refresh them. It goes in storage you already own — Catapult never sees your files.")
                .font(.caption)
                .foregroundStyle(.secondary)

            if state.sync?.icloudAvailable == false {
                Label("iCloud Drive is turned off. Turn it on in System Settings, or choose another folder.",
                      systemImage: CatapultIcon.warning)
                    .font(.caption)
                    .foregroundStyle(.orange)
            }

            HStack(spacing: 10) {
                Button("Use iCloud Drive") {
                    Task { await configure(provider: "folder", folder: nil) }
                }
                .disabled(busy || state.sync?.icloudAvailable == false)

                Button("Choose a folder…") {
                    if let folder = pickFolder() {
                        Task { await configure(provider: "folder", folder: folder.path) }
                    }
                }
                .disabled(busy)

                if state.sync?.provider != "disabled" {
                    Button("Turn off sync") {
                        Task { await configure(provider: "disabled", folder: nil) }
                    }
                    .disabled(busy)
                }
            }

            if let folder = state.sync?.folder, !folder.isEmpty {
                Text(folder)
                    .font(.caption2.monospaced())
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            if let bytes = state.sync?.vaultBytes, bytes > 0 {
                Text("\(formatted(bytes)) stored. iCloud's free tier is 5 GB, shared with Photos, Mail, and device backups.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Recovery key

    @ViewBuilder
    private var vaultSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Recovery key")
                .font(.headline)

            switch state.sync?.resolvedState ?? "disabled" {
            case "disabled":
                Text("Choose where the vault should live first.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

            case "needs_setup":
                Text("Create a vault. Catapult will show you a recovery key once — you'll need it on your other Macs.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Create vault") { Task { await createVault() } }
                    .disabled(busy)

            case "locked", "wrong_key":
                Text("This vault is locked. Paste the recovery key from your other Mac.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                HStack {
                    TextField("CAT1-…", text: $enteredKey)
                        .textFieldStyle(.roundedBorder)
                        .font(.body.monospaced())
                        .onSubmit { Task { await unlock() } }
                    Button("Unlock") { Task { await unlock() } }
                        .disabled(busy || enteredKey.isEmpty)
                }
                Button("Start a new vault instead") { Task { await createVault() } }
                    .buttonStyle(.link)
                    .font(.caption)

            default:
                Label("Vault unlocked on this Mac.", systemImage: CatapultIcon.ready)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Button("Sync now") { Task { await runSync() } }
                    .disabled(busy)
            }
        }
    }

    // MARK: - Wake to refresh

    private var wakeSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Wake to refresh")
                .font(.headline)
            Text("Apps expire after 7 days. Catapult refreshes them while it is running, and holds off idle sleep while it works — but it cannot wake a Mac that is fully asleep on its own.")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text("Scheduling a wake needs administrator rights, so Catapult won't do it for you. Run this in Terminal to wake nightly at 03:00:")
                .font(.caption)
                .foregroundStyle(.secondary)
            HStack {
                Text("sudo pmset repeat wake MTWRFSU 03:00:00")
                    .font(.caption2.monospaced())
                    .textSelection(.enabled)
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString("sudo pmset repeat wake MTWRFSU 03:00:00", forType: .string)
                    show("Copied.", error: false)
                } label: {
                    Image(systemName: CatapultIcon.copy)
                }
                .buttonStyle(.borderless)
                .help("Copy")
            }
            Text("Works on a desktop Mac, or a laptop on power. A laptop on battery with the lid closed goes straight back to sleep — macOS does not allow that to be overridden.")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - Actions

    private func configure(provider: String, folder: String?) async {
        busy = true
        defer { busy = false }
        do {
            try await state.configureSync(provider: provider, folder: folder)
            show(provider == "disabled" ? "Sync turned off." : "Vault location saved.", error: false)
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    private func createVault() async {
        busy = true
        defer { busy = false }
        do {
            if let key = try await state.createVault() {
                recoveryKeyToShow = key
            }
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    private func unlock() async {
        busy = true
        defer { busy = false }
        do {
            try await state.unlockVault(recoveryKey: enteredKey)
            enteredKey = ""
            show("Vault unlocked.", error: false)
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    private func runSync() async {
        busy = true
        defer { busy = false }
        do {
            try await state.runSync()
            show("Sync complete.", error: false)
        } catch {
            show(error.localizedDescription, error: true)
        }
    }

    private func show(_ text: String, error: Bool) {
        message = text
        isError = error
    }

    private func pickFolder() -> URL? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Use This Folder"
        panel.message = "Pick a folder that your cloud storage already syncs."
        return panel.runModal() == .OK ? panel.url : nil
    }

    private func formatted(_ bytes: Int) -> String {
        ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
    }
}

private struct IdentifiedKey: Identifiable {
    let value: String
    var id: String { value }

    init(_ value: String) {
        self.value = value
    }
}

/// Shown exactly once, when a vault is created.
private struct RecoveryKeySheet: View {
    let key: String
    @Binding var acknowledged: Bool
    let onDone: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Save your recovery key")
                .font(.title3.weight(.semibold))
            Text("Catapult cannot recover this for you. Without it, another Mac cannot open this vault.")
                .font(.callout)
                .foregroundStyle(.secondary)

            Text(key)
                .font(.system(.title3, design: .monospaced))
                .textSelection(.enabled)
                .padding(12)
                .frame(maxWidth: .infinity)
                .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))

            HStack(spacing: 10) {
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(key, forType: .string)
                } label: {
                    Label("Copy", systemImage: CatapultIcon.copy)
                }
                Button {
                    save()
                } label: {
                    Label("Save to a file…", systemImage: CatapultIcon.chooseFile)
                }
            }

            Text("If you lose it, you can still start a new vault — your IPAs are also stored on this Mac.")
                .font(.caption)
                .foregroundStyle(.secondary)

            Toggle("I saved my recovery key", isOn: $acknowledged)

            HStack {
                Spacer()
                Button("Done", action: onDone)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!acknowledged)
            }
        }
        .padding(24)
        .frame(width: 460)
    }

    private func save() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "Catapult Recovery Key.txt"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let body = """
        Catapult recovery key
        =====================

        \(key)

        Enter this on another Mac under Settings > Sync to open the same vault.
        Catapult cannot recover this key for you.
        """
        try? body.write(to: url, atomically: true, encoding: .utf8)
    }
}
