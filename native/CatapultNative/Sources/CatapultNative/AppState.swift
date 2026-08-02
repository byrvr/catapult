import Foundation
import Combine
import AppKit

enum AuthenticationPhase: Equatable, Sendable {
    case signedOut
    case signingIn
    case twoFactorRequired
    case signedIn(String)
}

@MainActor
final class AppState: ObservableObject {
    @Published var backend = BackendManager()
    @Published var authPhase: AuthenticationPhase = .signedOut
    @Published var devices: [Device] = []
    @Published var selectedDevice: Device?
    @Published var selectedIPAURL: URL?
    @Published var upload: UploadResponse?
    @Published var accountInfo: AccountInfo?

    @Published var isRefreshingDevices = false
    @Published var isUploading = false
    @Published var isInstalling = false
    @Published var isSettingUpDevice = false
    @Published var isLoadingAccountInfo = false
    @Published var isLoadingActivity = false
    @Published var activityJobs: [ActivityJob] = []
    @Published var reinstallingAppID: String?
    @Published var reinstallMessages: [String: InstallMessage] = [:]

    @Published var setupMessage = ""
    @Published var installMessage = ""
    @Published var installProgress = 0
    @Published var errorMessage: String?
    @Published var pinPromptDevice: Device?
    @Published var sync: SyncInfo?
    @Published var storeSources: [StoreSource] = []
    @Published var storeApps: [StoreApp] = []
    @Published var storeErrors: [StoreSourceError] = []
    @Published var storeFreeTeam = false
    @Published var isLoadingStore = false

    let client = APIClient()
    private var isStarting = false
    private var setupPollTask: Task<Void, Never>?
    private var cancellables: Set<AnyCancellable> = []

    init() {
        backend.objectWillChange
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
    }

    var canInstall: Bool {
        guard upload != nil, let selectedDevice, !isInstalling else {
            return false
        }
        if case .signedIn = authPhase {
            return selectedDevice.canInstallNow
        }
        return false
    }

    var isAuthenticated: Bool {
        if case .signedIn = authPhase {
            return true
        }
        return false
    }

    var signedInAppleID: String? {
        if case .signedIn(let appleID) = authPhase {
            return appleID
        }
        return nil
    }

    var installBlockedReason: String {
        if backend.status != .ready {
            return "Waiting for local engine"
        }
        if upload == nil {
            return "Choose an IPA"
        }
        guard let selectedDevice else {
            return "Select a device"
        }
        if selectedDevice.canRunSetup && !selectedDevice.canInstallNow {
            return "\(selectedDevice.setupActionTitle) \(selectedDevice.name)"
        }
        if !selectedDevice.canInstallNow {
            return "\(selectedDevice.name) is not installable"
        }
        if !isAuthenticated {
            return "Sign in with Apple ID"
        }
        if isInstalling {
            return "Installing"
        }
        return "Ready"
    }

    func start() async {
        guard !isStarting else {
            return
        }
        isStarting = true
        defer { isStarting = false }

        await backend.start()
        guard backend.status == .ready else {
            return
        }
        await checkAuthStatus()
        await refreshDevices()
    }

    func refreshDevices() async {
        isRefreshingDevices = true
        defer { isRefreshingDevices = false }
        do {
            let response = try await withTimeout(seconds: 20) {
                try await self.client.devices()
            }
            devices = response.devices
            if let selected = selectedDevice {
                selectedDevice = matchingDevice(for: selected, in: devices)
            }
        } catch {
            show(error)
        }
    }

    func selectDevice(_ device: Device) {
        selectedDevice = device
    }

    func checkAuthStatus() async {
        do {
            let status = try await client.authStatus()
            if status.authenticated == true {
                authPhase = .signedIn(status.appleID ?? "Apple ID")
                await loadAccountInfo()
            } else {
                authPhase = .signedOut
                accountInfo = nil
            }
        } catch {
            authPhase = .signedOut
        }
    }

    func login(appleID: String, password: String) async {
        authPhase = .signingIn
        errorMessage = nil
        do {
            let response = try await client.login(appleID: appleID, password: password)
            if response.status == "ok" {
                authPhase = .signedIn(appleID)
                await loadAccountInfo()
            } else if response.status == "2fa_required" {
                authPhase = .twoFactorRequired
            } else {
                authPhase = .signedOut
                errorMessage = response.displayMessage
            }
        } catch {
            authPhase = .signedOut
            show(error)
        }
    }

    func submit2FA(code: String) async {
        authPhase = .signingIn
        do {
            let response = try await client.submit2FA(code: code)
            if response.status == "ok" {
                let appleID = (try? await client.authStatus())?.appleID ?? "Apple ID"
                authPhase = .signedIn(appleID)
                await loadAccountInfo()
            } else {
                authPhase = .twoFactorRequired
                errorMessage = response.displayMessage
            }
        } catch {
            authPhase = .twoFactorRequired
            show(error)
        }
    }

    func logout() async {
        do {
            _ = try await client.logout()
        } catch {
            show(error)
        }
        authPhase = .signedOut
        accountInfo = nil
    }

    func loadAccountInfo(clearJobMessages: Bool = false) async {
        if clearJobMessages {
            reinstallMessages = [:]
            errorMessage = nil
        }
        isLoadingAccountInfo = true
        defer { isLoadingAccountInfo = false }
        do {
            accountInfo = try await client.accountInfo()
        } catch {
            accountInfo = nil
        }
    }

    func reloadAccountInfo() async {
        guard reinstallingAppID == nil else { return }
        await loadAccountInfo(clearJobMessages: true)
    }

    // MARK: - Cross-device sync

    func loadSyncStatus() async {
        sync = try? await client.syncStatus()
    }

    func configureSync(provider: String, folder: String?) async throws {
        sync = try await client.configureSync(provider: provider, folder: folder)
    }

    /// Returns the recovery key exactly once, for display. It is never stored
    /// in app state — losing it is recoverable, leaking it is not.
    func createVault() async throws -> String? {
        let response = try await client.createVault()
        await loadSyncStatus()
        return response.recoveryKey
    }

    func unlockVault(recoveryKey: String) async throws {
        _ = try await client.unlockVault(recoveryKey: recoveryKey)
        await loadSyncStatus()
    }

    func runSync() async throws {
        sync = try await client.runSync()
    }

    func loadActivity() async {
        guard backend.status == .ready else { return }
        isLoadingActivity = true
        defer { isLoadingActivity = false }
        do {
            activityJobs = try await client.activity().jobs
        } catch {
            show(error)
        }
    }

    func copyDiagnosticsToClipboard() async {
        guard backend.status == .ready else { return }
        do {
            let text = try await client.diagnosticsText()
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        } catch {
            show(error)
        }
    }

    func deleteProvisionedApp(_ app: ProvisionedApp) async {
        guard !app.appIDID.isEmpty else { return }
        do {
            let response = try await client.deleteAppID(app.appIDID)
            if response.status == "ok" {
                await loadAccountInfo()
            } else {
                errorMessage = response.displayMessage
            }
        } catch {
            show(error)
        }
    }

    func reinstallProvisionedApp(_ app: ProvisionedApp) async {
        guard app.reinstallable, reinstallingAppID == nil else { return }
        reinstallingAppID = app.id
        reinstallMessages[app.id] = InstallMessage(step: "preflight", progress: 0, message: "Checking saved install...")
        errorMessage = nil
        defer { reinstallingAppID = nil }

        do {
            try await client.reinstallApp(app) { [weak self] message in
                self?.reinstallMessages[app.id] = message
                if message.step == "error" {
                    self?.errorMessage = message.message
                }
            }
            if reinstallMessages[app.id]?.step == "done" {
                await loadAccountInfo()
            }
        } catch {
            reinstallMessages[app.id] = InstallMessage(step: "error", progress: 0, message: error.localizedDescription)
            show(error)
        }
    }

    func uploadIPA(_ url: URL) async {
        guard url.pathExtension.lowercased() == "ipa" else {
            errorMessage = "Choose a .ipa file."
            return
        }

        selectedIPAURL = url
        upload = nil
        isUploading = true
        defer { isUploading = false }

        let scoped = url.startAccessingSecurityScopedResource()
        defer {
            if scoped {
                url.stopAccessingSecurityScopedResource()
            }
        }

        do {
            upload = try await withTimeout(seconds: 120, message: "IPA upload timed out. Restart Catapult and try again.") {
                try await self.client.uploadIPA(fileURL: url)
            }
        } catch {
            selectedIPAURL = nil
            show(error)
        }
    }

    func setup(_ device: Device) async {
        isSettingUpDevice = true
        setupMessage = "Connecting to \(device.name)..."
        errorMessage = nil

        setupPollTask?.cancel()
        setupPollTask = Task { [weak self] in
            await self?.pollPairingStatus(for: device)
        }

        do {
            let response = try await client.setupDevice(device)
            setupPollTask?.cancel()
            if response.status == "ok" {
                setupMessage = response.displayMessage.isEmpty ? "Tunnel ready." : response.displayMessage
                markSetupComplete(for: device)
                pinPromptDevice = nil
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                await refreshDevices()
            } else {
                setupMessage = ""
                errorMessage = response.displayMessage
            }
        } catch {
            setupPollTask?.cancel()
            setupMessage = ""
            show(error)
        }

        isSettingUpDevice = false
    }

    private func matchingDevice(for selected: Device, in candidates: [Device]) -> Device? {
        candidates.first { $0.udid == selected.udid }
            ?? candidates.first { $0.host == selected.host && $0.deviceClass == selected.deviceClass }
            ?? candidates.first { $0.host == selected.host }
            ?? candidates.first { $0.name == selected.name && $0.deviceClass == selected.deviceClass }
    }

    private func markSetupComplete(for device: Device) {
        let readyDevice = device.setupComplete
        devices = devices.map { candidate in
            if candidate.udid == device.udid || candidate.host == device.host {
                return candidate.setupComplete
            }
            return candidate
        }
        selectedDevice = matchingDevice(for: readyDevice, in: devices) ?? readyDevice
    }

    func submitPIN(_ pin: String) async {
        do {
            _ = try await client.submitPIN(pin)
            setupMessage = "Pairing..."
            pinPromptDevice = nil
        } catch {
            show(error)
        }
    }

    func installSelected() async {
        guard let device = selectedDevice, let upload else {
            return
        }

        isInstalling = true
        installProgress = 0
        installMessage = "Preparing..."
        errorMessage = nil

        do {
            try await client.install(deviceUDID: device.udid, ipaPath: upload.path) { [weak self] message in
                self?.installProgress = message.progress
                self?.installMessage = message.message
                if message.step == "error" {
                    self?.errorMessage = message.message
                }
            }
            if installProgress >= 100 {
                await loadAccountInfo()
            }
        } catch {
            show(error)
        }

        isInstalling = false
    }

    func clearError() {
        errorMessage = nil
    }

    private func pollPairingStatus(for device: Device) async {
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            guard !Task.isCancelled else { return }

            do {
                let response = try await client.pairStatus()
                switch response.state {
                case "browsing":
                    setupMessage = "Finding \(device.name)..."
                case "pairing":
                    setupMessage = "Pairing..."
                case "waiting_pin":
                    setupMessage = "Waiting for PIN..."
                    if pinPromptDevice == nil {
                        pinPromptDevice = device
                    }
                case "done":
                    setupMessage = "Pairing complete. Creating tunnel..."
                    return
                case "error":
                    return
                default:
                    break
                }
            } catch {
                return
            }
        }
    }

    private func show(_ error: Error) {
        errorMessage = error.localizedDescription
    }
}

private func withTimeout<T: Sendable>(
    seconds: UInt64,
    message: String = "Local network scan timed out.",
    operation: @escaping @Sendable () async throws -> T
) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { group in
        group.addTask {
            try await operation()
        }
        group.addTask {
            try await Task.sleep(nanoseconds: seconds * 1_000_000_000)
            throw CatapultError.requestFailed(message)
        }
        guard let result = try await group.next() else {
            throw CatapultError.invalidResponse
        }
        group.cancelAll()
        return result
    }
}

// MARK: - Store

extension AppState {
    func loadStore(force: Bool = false) async {
        if isLoadingStore && !force { return }
        isLoadingStore = true
        defer { isLoadingStore = false }
        storeSources = (try? await client.storeSources())?.sources ?? []
        if let catalog = try? await client.storeApps(deviceUDID: selectedDevice?.udid) {
            storeApps = catalog.apps
            storeErrors = catalog.errors
            storeFreeTeam = catalog.freeTeam
        }
    }

    func addStoreSource(url: String) async {
        isLoadingStore = true
        do {
            _ = try await client.addStoreSource(url: url)
            isLoadingStore = false
            await loadStore(force: true)
        } catch {
            isLoadingStore = false
            errorMessage = error.localizedDescription
        }
    }

    func removeStoreSource(id: String) async {
        storeSources = (try? await client.removeStoreSource(id: id))?.sources ?? storeSources
        await loadStore(force: true)
    }

    func installFromStore(_ app: StoreApp) async {
        guard let device = selectedDevice else {
            errorMessage = "Select a device first."
            return
        }
        isInstalling = true
        installProgress = 0
        installMessage = "Starting…"
        do {
            try await client.storeInstall(appKey: app.appKey, deviceUDID: device.udid) { message in
                self.installMessage = message.message
                self.installProgress = message.progress
            }
            await loadStore(force: true)
        } catch {
            errorMessage = error.localizedDescription
        }
        isInstalling = false
    }
}
