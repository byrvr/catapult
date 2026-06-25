import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @EnvironmentObject private var state: AppState
    @State private var showSignIn = false
    @State private var showAccount = false
    @State private var showActivity = false

    var body: some View {
        VStack(spacing: 0) {
            TopBar(showSignIn: $showSignIn, showAccount: $showAccount, showActivity: $showActivity)
            Divider()
            BackendBanner()
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    InstallWorkspace(showSignIn: $showSignIn)
                    DeviceBrowser()
                }
                .padding(22)
                .frame(maxWidth: 980)
                .frame(maxWidth: .infinity)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(Color(nsColor: .windowBackgroundColor))
        .sheet(isPresented: $showSignIn) {
            SignInSheet()
                .environmentObject(state)
        }
        .sheet(isPresented: $showAccount) {
            AccountSheet()
                .environmentObject(state)
        }
        .sheet(isPresented: $showActivity) {
            ActivitySheet(
                jobs: state.activityJobs,
                isLoading: state.isLoadingActivity
            ) {
                Task { await state.loadActivity() }
            } onCopyDiagnostics: { _ in
                Task { await state.copyDiagnosticsToClipboard() }
            }
            .environmentObject(state)
            .task { await state.loadActivity() }
        }
        .sheet(item: Binding(
            get: { state.pinPromptDevice },
            set: { state.pinPromptDevice = $0 }
        )) { device in
            PinEntrySheet(device: device)
                .environmentObject(state)
        }
        .alert("Catapult", isPresented: Binding(
            get: { state.errorMessage != nil },
            set: { if !$0 { state.clearError() } }
        )) {
            Button("OK") { state.clearError() }
        } message: {
            Text(state.errorMessage ?? "")
        }
        .onReceive(state.$authPhase) { phase in
            if case .signedIn = phase {
                showSignIn = false
            }
        }
    }
}

private struct TopBar: View {
    @EnvironmentObject private var state: AppState
    @Binding var showSignIn: Bool
    @Binding var showAccount: Bool
    @Binding var showActivity: Bool

    var body: some View {
        HStack(spacing: 12) {
            CatapultBrandIcon(size: 28)

            Text("Catapult")
                .font(.headline.weight(.semibold))

            BackendStatusPill()

            Spacer()

            Button {
                showActivity = true
                Task { await state.loadActivity() }
            } label: {
                Label("Activity", systemImage: CatapultIcon.activity)
            }
            .buttonStyle(.bordered)
            .disabled(state.backend.status != .ready)

            Button {
                if state.isAuthenticated {
                    showAccount = true
                } else {
                    showSignIn = true
                }
            } label: {
                Label(accountLabel, systemImage: CatapultIcon.account)
            }
            .buttonStyle(.bordered)

            Button {
                Task { await state.refreshDevices() }
            } label: {
                Label("Refresh", systemImage: CatapultIcon.refresh)
            }
            .disabled(state.backend.status != .ready || state.isRefreshingDevices)
        }
        .padding(.horizontal, 18)
        .frame(height: 52)
    }

    private var accountLabel: String {
        if let info = state.accountInfo {
            return info.compactAppIDUsageSummary
        }
        if let appleID = state.signedInAppleID {
            return appleID
        }
        return "Sign In"
    }
}

private struct BackendStatusPill: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        switch state.backend.status {
        case .ready:
            CatapultStatusPill(title: "Engine ready", color: .green)
        case .starting:
            HStack(spacing: 6) {
                ProgressView()
                    .controlSize(.small)
                Text("Starting engine")
                    .font(.caption.weight(.medium))
            }
            .foregroundStyle(.secondary)
        case .failed:
            CatapultStatusPill(title: "Engine failed", color: .red)
        case .stopped:
            CatapultStatusPill(title: "Engine stopped", color: .secondary)
        }
    }
}

private struct BackendBanner: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        switch state.backend.status {
        case .ready:
            EmptyView()
        case .starting:
            StatusBanner(style: .info, title: "Starting local engine", detail: state.backend.startupDetail, actionTitle: nil, action: nil)
        case .stopped:
            StatusBanner(style: .neutral, title: "Local engine stopped", detail: "Start it to scan devices and install apps.", actionTitle: "Start") {
                Task { await state.start() }
            }
        case .failed(let message):
            StatusBanner(style: .error, title: "Local engine failed", detail: message, actionTitle: "Retry") {
                Task { await state.start() }
            }
        }
    }
}

private struct InstallWorkspace: View {
    @EnvironmentObject private var state: AppState
    @Binding var showSignIn: Bool
    @State private var showImporter = false
    @State private var dropTargeted = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Install an app")
                        .font(.title2.weight(.semibold))
                    Text(state.installBlockedReason)
                        .font(.callout)
                        .foregroundStyle(state.canInstall ? .green : .secondary)
                }
                Spacer()
                Button {
                    Task { await state.installSelected() }
                } label: {
                    Label(state.isInstalling ? "Installing" : "Install", systemImage: CatapultIcon.install)
                        .frame(minWidth: 108)
                }
                .controlSize(.large)
                .buttonStyle(.borderedProminent)
                .disabled(!state.canInstall)
            }

            VStack(spacing: 0) {
                IPAWorkflowRow(showImporter: $showImporter, dropTargeted: $dropTargeted)
                Divider().padding(.leading, 48)
                DeviceWorkflowRow()
                if !state.isAuthenticated {
                    Divider().padding(.leading, 48)
                    AuthPrerequisiteRow(showSignIn: $showSignIn)
                }
            }
            .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 12))
            .overlay {
                RoundedRectangle(cornerRadius: 12)
                    .stroke(dropTargeted ? Color.accentColor : Color.secondary.opacity(0.16), lineWidth: 1)
            }
            .fileImporter(
                isPresented: $showImporter,
                allowedContentTypes: [UTType(filenameExtension: "ipa") ?? .data],
                allowsMultipleSelection: false
            ) { result in
                if case .success(let urls) = result, let url = urls.first {
                    Task { await state.uploadIPA(url) }
                }
            }
            .onDrop(of: [UTType.fileURL.identifier], isTargeted: $dropTargeted) { providers in
                guard let provider = providers.first else { return false }
                provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                    let url: URL?
                    if let data = item as? Data {
                        url = URL(dataRepresentation: data, relativeTo: nil)
                    } else {
                        url = item as? URL
                    }
                    if let url {
                        Task { @MainActor in
                            await state.uploadIPA(url)
                        }
                    }
                }
                return true
            }

            if state.isInstalling || state.installProgress > 0 {
                VStack(alignment: .leading, spacing: 6) {
                    ProgressView(value: Double(state.installProgress), total: 100)
                    Text(state.installMessage.isEmpty ? "Preparing..." : state.installMessage)
                        .font(.caption)
                        .foregroundStyle(state.installProgress >= 100 ? .green : .secondary)
                }
            }
        }
        .padding(18)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .overlay {
            RoundedRectangle(cornerRadius: 14)
                .stroke(Color.secondary.opacity(0.12), lineWidth: 1)
        }
    }
}

private struct IPAWorkflowRow: View {
    @EnvironmentObject private var state: AppState
    @Binding var showImporter: Bool
    @Binding var dropTargeted: Bool

    var body: some View {
        WorkflowRow(
            icon: state.upload == nil ? CatapultIcon.ipaFile : CatapultIcon.appID,
            title: state.uploadTitle,
            detail: state.uploadDetail,
            status: state.uploadStatus
        ) {
            Button {
                showImporter = true
            } label: {
                Label(state.upload == nil ? "Choose" : "Change", systemImage: CatapultIcon.chooseFile)
            }
            .disabled(state.isUploading)
        }
    }
}

private struct DeviceWorkflowRow: View {
    @EnvironmentObject private var state: AppState

    var body: some View {
        WorkflowRow(
            icon: state.selectedDevice == nil ? CatapultIcon.device(for: nil) : CatapultIcon.device(for: state.selectedDevice),
            title: state.selectedDevice?.name ?? "Target device",
            detail: selectedDeviceDetail,
            status: state.selectedDevice?.statusLabel
        ) {
            if let device = state.selectedDevice, device.canRunSetup && !device.canInstallNow {
                Button {
                    Task { await state.setup(device) }
                } label: {
                    Label(device.setupActionTitle, systemImage: CatapultIcon.setup)
                }
                .disabled(state.isSettingUpDevice)
            } else {
                Text("Select below")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var selectedDeviceDetail: String {
        guard let device = state.selectedDevice else {
            return "Choose a ready device from the list below."
        }
        return device.displayDetail
    }
}

private struct AuthPrerequisiteRow: View {
    @Binding var showSignIn: Bool

    var body: some View {
        WorkflowRow(
            icon: CatapultIcon.account,
            title: "Apple ID required",
            detail: "Sign in only when you are ready to provision and install.",
            status: nil
        ) {
            Button {
                showSignIn = true
            } label: {
                Label("Sign In", systemImage: CatapultIcon.account)
            }
            .buttonStyle(.borderedProminent)
        }
    }
}

private struct WorkflowRow<Trailing: View>: View {
    let icon: String
    let title: String
    let detail: String
    let status: String?
    @ViewBuilder let trailing: Trailing

    var body: some View {
        HStack(spacing: 14) {
            CatapultIconTile(systemName: icon)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 8) {
                    Text(title)
                        .font(.callout.weight(.semibold))
                        .lineLimit(1)
                    if let status, !status.isEmpty {
                        CatapultStatusPill(title: status, color: .secondary, showsDot: false)
                    }
                }
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }

            Spacer(minLength: 10)
            trailing
        }
        .padding(14)
    }
}

private struct DeviceBrowser: View {
    @EnvironmentObject private var state: AppState
    @State private var showOtherDevices = false

    private var primaryDevices: [Device] {
        state.devices
            .filter(\.isRelevantForInstall)
            .sorted(by: deviceSort)
    }

    private var otherDevices: [Device] {
        state.devices
            .filter { !$0.isRelevantForInstall }
            .sorted(by: deviceSort)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Devices")
                    .font(.headline)
                Spacer()
                if state.isRefreshingDevices {
                    ProgressView()
                        .controlSize(.small)
                }
            }

            if state.isRefreshingDevices && state.devices.isEmpty {
                LoadingRow("Scanning local network...")
                    .padding(.vertical, 18)
            } else if state.devices.isEmpty {
                EmptyState(icon: CatapultIcon.noDevices, title: "No devices found", detail: "Keep the target device on this network and enable Developer Mode for Apple TV.")
                    .padding(.vertical, 12)
            } else {
                VStack(spacing: 8) {
                    ForEach(primaryDevices) { device in
                        DeviceChoiceRow(device: device, isPrimary: true)
                    }
                }

                if !otherDevices.isEmpty {
                    DisclosureGroup(isExpanded: $showOtherDevices) {
                        VStack(spacing: 8) {
                            ForEach(otherDevices) { device in
                                DeviceChoiceRow(device: device, isPrimary: false)
                            }
                        }
                        .padding(.top, 8)
                    } label: {
                        Text("Other devices (\(otherDevices.count))")
                            .font(.callout.weight(.medium))
                    }
                    .padding(.top, 4)
                }
            }

            if !state.setupMessage.isEmpty {
                Label(state.setupMessage, systemImage: CatapultIcon.setup)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .padding(.top, 2)
            }
        }
        .padding(18)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 14))
        .overlay {
            RoundedRectangle(cornerRadius: 14)
                .stroke(Color.secondary.opacity(0.12), lineWidth: 1)
        }
    }
}

private struct DeviceChoiceRow: View {
    @EnvironmentObject private var state: AppState
    let device: Device
    let isPrimary: Bool

    private var selected: Bool {
        state.selectedDevice?.udid == device.udid
    }

    var body: some View {
        HStack(spacing: 12) {
            CatapultIconTile(
                systemName: CatapultIcon.device(for: device),
                tint: selected ? Color.accentColor : Color.secondary,
                dimension: 30,
                font: .callout
            )

            VStack(alignment: .leading, spacing: 2) {
                Text(device.name)
                    .font(.callout.weight(.medium))
                    .lineLimit(1)
                Text(device.displayDetail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Spacer(minLength: 10)

            StatusBadge(device: device)

            if device.canRunSetup && !device.canInstallNow {
                Button {
                    state.selectDevice(device)
                    Task { await state.setup(device) }
                } label: {
                    Label(device.setupActionTitle, systemImage: CatapultIcon.setup)
                }
                .disabled(state.isSettingUpDevice)
            } else if device.canInstallNow {
                Button(selected ? "Selected" : "Select") {
                    state.selectDevice(device)
                }
                .buttonStyle(.bordered)
                .tint(selected ? Color.accentColor : nil)
            }
        }
        .padding(12)
        .background(rowBackground, in: RoundedRectangle(cornerRadius: 10))
        .overlay {
            RoundedRectangle(cornerRadius: 10)
                .stroke(selected ? Color.accentColor.opacity(0.75) : Color.secondary.opacity(0.12), lineWidth: 1)
        }
        .opacity(isPrimary ? 1 : 0.72)
        .contentShape(RoundedRectangle(cornerRadius: 10))
        .onTapGesture {
            if device.isRelevantForInstall {
                state.selectDevice(device)
            }
        }
    }

    private var rowBackground: Color {
        if selected {
            return Color.accentColor.opacity(0.11)
        }
        return Color(nsColor: .controlBackgroundColor)
    }
}

private struct StatusBadge: View {
    let device: Device

    var body: some View {
        CatapultStatusPill(title: device.statusLabel, color: color)
    }

    private var color: Color {
        if device.needsFirstSetup {
            return .orange
        }
        if device.needsTunnelConnection {
            return .blue
        }
        if device.canInstallNow {
            return .green
        }
        return .secondary
    }
}

private struct SignInSheet: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var appleID = ""
    @State private var password = ""
    @State private var code = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Apple ID")
                        .font(.title2.weight(.semibold))
                    Text("Used to create the signing certificate and provisioning profile.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }

            switch state.authPhase {
            case .signedIn(let name):
                Label("Signed in as \(name)", systemImage: CatapultIcon.ready)
                    .foregroundStyle(.green)
                HStack {
                    Spacer()
                    Button("Done") { dismiss() }
                        .buttonStyle(.borderedProminent)
                }
            case .signingIn:
                LoadingRow("Signing in...")
            case .twoFactorRequired:
                VStack(alignment: .leading, spacing: 10) {
                    TextField("Verification code", text: $code)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { submitCode() }
                    HStack {
                        Spacer()
                        Button("Verify") { submitCode() }
                            .buttonStyle(.borderedProminent)
                            .disabled(code.count < 4)
                    }
                }
            case .signedOut:
                VStack(spacing: 10) {
                    TextField("Apple ID", text: $appleID)
                        .textFieldStyle(.roundedBorder)
                    SecureField("Password", text: $password)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { signIn() }
                    HStack {
                        Button("Cancel") { dismiss() }
                        Spacer()
                        Button("Sign In") { signIn() }
                            .buttonStyle(.borderedProminent)
                            .disabled(appleID.isEmpty || password.isEmpty)
                    }
                }
            }
        }
        .padding(24)
        .frame(width: 420)
    }

    private func signIn() {
        Task { await state.login(appleID: appleID, password: password) }
    }

    private func submitCode() {
        Task { await state.submit2FA(code: code) }
    }
}

private struct AccountSheet: View {
    @EnvironmentObject private var state: AppState
    @Environment(\.dismiss) private var dismiss
    @State private var pendingDelete: ProvisionedApp?

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Developer Account")
                        .font(.title2.weight(.semibold))
                    Text(state.signedInAppleID ?? "Not signed in")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer()
                Button("Close") { dismiss() }
            }

            if let info = state.accountInfo {
                HStack(spacing: 16) {
                    AccountMetric(title: "Team", value: info.team.name)
                    AccountMetric(title: "App IDs", value: info.appIDUsageSummary)
                    AccountMetric(title: "Plan", value: info.team.isFree ? "Free" : info.team.type)
                }

                if let sync = info.sync {
                    SyncSummaryRow(sync: sync)
                }

                Divider()

                if info.apps.isEmpty {
                    EmptyState(icon: CatapultIcon.emptyAppIDs, title: "No provisioned apps", detail: "Installed apps will appear here.")
                        .frame(maxWidth: .infinity)
                } else {
                    ScrollView {
                        LazyVStack(spacing: 8) {
                            ForEach(info.apps) { app in
                                ProvisionedAppRow(
                                    app: app,
                                    reinstallStatus: state.reinstallMessages[app.id],
                                    isReinstalling: state.reinstallingAppID == app.id,
                                    reinstallDisabled: state.reinstallingAppID != nil
                                ) {
                                    Task { await state.reinstallProvisionedApp(app) }
                                } onDelete: {
                                    pendingDelete = app
                                }
                            }
                        }
                    }
                    .frame(minHeight: 220, maxHeight: 360)
                }
            } else if state.isAuthenticated {
                LoadingRow("Loading account...")
                    .task { await state.loadAccountInfo() }
            } else {
                EmptyState(icon: CatapultIcon.account, title: "Not signed in", detail: "Sign in before viewing account slots.")
            }

            HStack {
                Button(role: .destructive) {
                    Task {
                        await state.logout()
                        dismiss()
                    }
                } label: {
                    Label("Sign Out", systemImage: CatapultIcon.signOut)
                }
                .disabled(!state.isAuthenticated)
                Spacer()
                Button {
                    Task { await state.reloadAccountInfo() }
                } label: {
                    if state.isLoadingAccountInfo {
                        HStack(spacing: 8) {
                            ProgressView()
                                .controlSize(.small)
                            Text("Reloading")
                        }
                    } else {
                        Label("Reload", systemImage: CatapultIcon.refresh)
                    }
                }
                .disabled(!state.isAuthenticated || state.isLoadingAccountInfo || state.reinstallingAppID != nil)
            }
        }
        .padding(24)
        .frame(width: 620, height: 600)
        .confirmationDialog("Delete App ID?", isPresented: Binding(
            get: { pendingDelete != nil },
            set: { if !$0 { pendingDelete = nil } }
        )) {
            Button("Delete", role: .destructive) {
                if let pendingDelete {
                    Task { await state.deleteProvisionedApp(pendingDelete) }
                }
            }
        } message: {
            Text("This removes the App ID and its provisioning profiles from your Apple developer account.")
        }
    }
}

private struct SyncSummaryRow: View {
    let sync: SyncInfo

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .font(.callout.weight(.semibold))
                .foregroundStyle(color)
                .frame(width: 22)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.caption.weight(.semibold))
                Text(detail)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer()
        }
        .padding(10)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 9))
    }

    private var title: String {
        if sync.status == "ok" {
            return "Sync ready"
        }
        if sync.status == "needs_key" {
            return "Sync needs recovery key"
        }
        if sync.status == "wrong_key" {
            return "Sync key is wrong"
        }
        if sync.configured {
            return "Sync configured"
        }
        return "Sync disabled"
    }

    private var detail: String {
        if sync.status == "ok" {
            let uploaded = sync.uploadedIPAs ?? 0
            let downloaded = sync.downloadedIPAs ?? 0
            let installs = sync.installCount ?? 0
            let portability = sync.portableKey == true ? "portable key" : "local-only key"
            return "\(providerName) · \(installs) installs · \(uploaded) uploaded · \(downloaded) downloaded · \(portability)"
        }
        if sync.status == "needs_key" {
            return "\(providerName) configured · set CATAPULT_SYNC_KEY on each Mac before remote sync can run"
        }
        if sync.status == "wrong_key" {
            return "\(providerName) configured · this Mac cannot decrypt the remote vault"
        }
        if sync.configured {
            let portability = sync.portableKey == true ? "portable across Macs" : "needs shared CATAPULT_SYNC_KEY for another Mac"
            return "\(providerName) configured · \(portability)"
        }
        return "Set sync settings in ~/.catapult/config.env to recover IPAs on another Mac."
    }

    private var providerName: String {
        switch sync.provider {
        case "r2": return "Cloudflare R2"
        case "folder": return "Sync folder"
        default: return sync.provider.isEmpty ? "No provider" : sync.provider
        }
    }

    private var icon: String {
        sync.configured ? "externaldrive.connected.to.line.below" : "externaldrive.badge.xmark"
    }

    private var color: Color {
        if sync.status == "ok" {
            return .green
        }
        if sync.status == "wrong_key" {
            return .red
        }
        return sync.configured ? .orange : .secondary
    }
}

private struct AccountMetric: View {
    let title: String
    let value: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(value.isEmpty ? "—" : value)
                .font(.callout.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.82)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct ProvisionedAppRow: View {
    let app: ProvisionedApp
    let reinstallStatus: InstallMessage?
    let isReinstalling: Bool
    let reinstallDisabled: Bool
    let onReinstall: () -> Void
    let onDelete: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                CatapultIconTile(
                    systemName: icon,
                    tint: app.isCatapult ? Color.accentColor : Color.secondary,
                    dimension: 28,
                    font: .callout
                )
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.callout.weight(.medium))
                        .lineLimit(1)
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                    if let expiryDetail {
                        Text(expiryDetail)
                            .font(.caption2)
                            .foregroundStyle(expiryColor)
                            .lineLimit(1)
                    }
                    if let autoRefreshDetail {
                        Text(autoRefreshDetail)
                            .font(.caption2)
                            .foregroundStyle(app.autoRefreshEligible == true ? Color.green : Color.secondary)
                            .lineLimit(1)
                    }
                }
                Spacer()
                if app.extensionSlot {
                    InlineStatusPill(title: "Parent-managed", color: .secondary)
                } else if app.expired {
                    InlineStatusPill(title: "Expired", color: .red)
                } else if app.historyOnlyRow {
                    InlineStatusPill(title: "History", color: .secondary)
                } else if app.reinstallBlockedReason != nil && !app.reinstallable {
                    InlineStatusPill(title: "Blocked", color: .orange)
                }
                if !app.extensionSlot {
                    Button {
                        onReinstall()
                    } label: {
                        if isReinstalling {
                            ProgressView()
                                .controlSize(.small)
                        } else {
                            Image(systemName: CatapultIcon.refresh)
                        }
                    }
                    .buttonStyle(.borderless)
                    .help(app.reinstallBlockedReason ?? "Reinstall to reset expiration")
                    .disabled(reinstallDisabled || !app.reinstallable)
                }
                Button(role: .destructive) {
                    onDelete()
                } label: {
                    Image(systemName: CatapultIcon.delete)
                }
                .buttonStyle(.borderless)
                .help("Delete App ID")
                .disabled(app.appIDID.isEmpty)
            }

            if let reinstallStatus {
                HStack(spacing: 8) {
                    if showsProgress(for: reinstallStatus) {
                        ProgressView(value: Double(max(0, min(100, reinstallStatus.progress))), total: 100)
                            .controlSize(.small)
                            .frame(width: 92)
                    } else {
                        Image(systemName: statusIcon(for: reinstallStatus))
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(statusColor(for: reinstallStatus))
                            .frame(width: 14)
                    }
                    Text(statusText(for: reinstallStatus))
                        .font(.caption2)
                        .foregroundStyle(statusColor(for: reinstallStatus))
                        .lineLimit(2)
                }
                .padding(.leading, 32)
            } else if let blocked = app.reinstallBlockedReason {
                HStack(spacing: 6) {
                    Image(systemName: app.extensionSlot ? CatapultIcon.extensionAppID : CatapultIcon.warning)
                        .font(.caption2.weight(.semibold))
                    Text(blocked)
                        .lineLimit(2)
                }
                .font(.caption2)
                .foregroundStyle(app.extensionSlot ? Color.secondary : Color.orange)
                .padding(.leading, 32)
            }
        }
        .padding(10)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 9))
    }

    private var icon: String {
        app.extensionSlot ? CatapultIcon.extensionAppID : CatapultIcon.appID
    }

    private var title: String {
        if app.extensionSlot {
            let parent = app.parentName?.isEmpty == false ? app.parentName! : "App"
            let extensionName = app.extensionName?.isEmpty == false ? app.extensionName! : "Extension"
            if extensionName.localizedCaseInsensitiveContains("widget") {
                return "\(parent) Widget Extension"
            }
            return "\(parent) \(extensionName)"
        }
        return app.name.isEmpty ? app.identifier : app.name
    }

    private var detail: String {
        if app.extensionSlot {
            let parent = app.parentName?.isEmpty == false ? app.parentName! : "the parent app"
            return "Extension App ID · refreshes with \(parent)"
        }
        if let installed = app.installed, !installed.isEmpty {
            let location = app.installedDevice?.isEmpty == false ? "\(installed) · \(app.installedDevice!)" : installed
            return app.historyOnlyRow ? "\(location) · not in current App IDs" : location
        }
        return app.identifier
    }

    private var expiryDetail: String? {
        if let expiresAt = app.expiresAt, let expiryDate = Self.parseExpiryDate(expiresAt) {
            let expiry = Self.expiryDateFormatter.string(from: expiryDate)
            if expiryDate.timeIntervalSinceNow <= 0 {
                return "Expired \(expiry)"
            }
            return "Expires \(expiry) · \(preciseTimeLeft(until: expiryDate))"
        }
        if let expiry = app.expiry, !expiry.isEmpty {
            if let timeLeft = app.timeLeft, !timeLeft.isEmpty {
                if app.expired {
                    return "Expired \(expiry)"
                }
                return "Expires \(expiry) · \(timeLeft)"
            }
            return "Expires \(expiry)"
        }
        return nil
    }

    private var autoRefreshDetail: String? {
        guard !app.extensionSlot else {
            return nil
        }
        if app.autoRefreshEligible == true {
            return "Auto-refresh eligible now"
        }
        if let autoRefreshAfter = app.autoRefreshAfter,
           let refreshDate = Self.parseExpiryDate(autoRefreshAfter) {
            return "Auto-refresh starts \(Self.expiryDateFormatter.string(from: refreshDate))"
        }
        return nil
    }

    private var expiryColor: Color {
        if app.expired {
            return .red
        }
        guard let daysLeft = app.daysLeft else {
            return .secondary
        }
        return daysLeft <= 1 ? .red : daysLeft <= 3 ? .orange : .secondary
    }

    private func statusText(for message: InstallMessage) -> String {
        message.step == "error" ? "Reinstall blocked: \(message.message)" : message.message
    }

    private func showsProgress(for message: InstallMessage) -> Bool {
        message.step != "done" && message.step != "error"
    }

    private func statusIcon(for message: InstallMessage) -> String {
        switch message.step {
        case "done":
            return CatapultIcon.ready
        case "error":
            return CatapultIcon.warning
        default:
            return CatapultIcon.activity
        }
    }

    private func statusColor(for message: InstallMessage) -> Color {
        switch message.step {
        case "done":
            return .green
        case "error":
            return .red
        default:
            return .secondary
        }
    }

    private func preciseTimeLeft(until date: Date) -> String {
        let seconds = max(0, Int(date.timeIntervalSinceNow.rounded(.down)))
        guard seconds > 0 else {
            return "expired"
        }

        let days = seconds / 86_400
        let hours = (seconds % 86_400) / 3_600
        let minutes = (seconds % 3_600) / 60

        if days > 0 {
            return "\(days)d \(hours)h \(minutes)m remaining"
        }
        if hours > 0 {
            return "\(hours)h \(minutes)m remaining"
        }
        return "\(max(1, minutes))m remaining"
    }

    private static func parseExpiryDate(_ value: String) -> Date? {
        if let date = isoDateFormatter.date(from: value) {
            return date
        }
        return fractionalISODateFormatter.date(from: value)
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

    private static let expiryDateFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateFormat = "MMM d, yyyy h:mm a z"
        formatter.locale = .current
        formatter.timeZone = .current
        return formatter
    }()
}

private struct InlineStatusPill: View {
    let title: String
    let color: Color

    var body: some View {
        CatapultStatusPill(title: title, color: color)
    }
}

private extension AccountInfo {
    var appIDUsageSummary: String {
        let appText = "\(nonExtensionAppCount) \(nonExtensionAppCount == 1 ? "app" : "apps")"
        let appIDText = "\(appCount) \(appCount == 1 ? "App ID" : "App IDs") used / \(appLimit)"
        return "\(appText) · \(appIDText)"
    }

    var compactAppIDUsageSummary: String {
        if extensionAppIDCount > 0 {
            return "\(nonExtensionAppCount) \(nonExtensionAppCount == 1 ? "app" : "apps") · \(appCount)/\(appLimit) App IDs"
        }
        return "\(appCount)/\(appLimit) App IDs"
    }

    private var nonExtensionAppCount: Int {
        apps.filter { !$0.extensionSlot }.count
    }

    private var extensionAppIDCount: Int {
        apps.filter(\.extensionSlot).count
    }
}

private struct PinEntrySheet: View {
    @EnvironmentObject private var state: AppState
    let device: Device
    @State private var pin = ""

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: CatapultIcon.device(for: device))
                .font(.largeTitle)
                .foregroundStyle(Color.accentColor)
            VStack(spacing: 4) {
                Text("Enter Pairing PIN")
                    .font(.title3.weight(.semibold))
                Text("Use the code shown on \(device.name).")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            TextField("000000", text: $pin)
                .font(.system(size: 28, weight: .semibold, design: .monospaced))
                .multilineTextAlignment(.center)
                .textFieldStyle(.roundedBorder)
                .frame(width: 180)
                .onSubmit {
                    Task { await state.submitPIN(pin) }
                }
            HStack {
                Button("Cancel") {
                    state.pinPromptDevice = nil
                }
                Button("Pair") {
                    Task { await state.submitPIN(pin) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(pin.isEmpty)
            }
        }
        .padding(28)
        .frame(width: 360)
    }
}

private struct StatusBanner: View {
    enum Style {
        case info
        case neutral
        case error
    }

    let style: Style
    let title: String
    let detail: String
    let actionTitle: String?
    let action: (() -> Void)?

    var body: some View {
        HStack(spacing: 12) {
            if style == .info {
                ProgressView()
                    .controlSize(.small)
                    .frame(width: 16, height: 16)
            } else {
                Image(systemName: icon)
                    .foregroundStyle(color)
                    .frame(width: 16, height: 16)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.callout.weight(.semibold))
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(style == .error ? 4 : 2)
            }
            Spacer()
            if let actionTitle, let action {
                Button(actionTitle, action: action)
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 10)
        .background(color.opacity(0.09))
    }

    private var icon: String {
        switch style {
        case .info: "clock"
        case .neutral: CatapultIcon.stopped
        case .error: CatapultIcon.warning
        }
    }

    private var color: Color {
        switch style {
        case .info: .blue
        case .neutral: .secondary
        case .error: .red
        }
    }
}

private struct EmptyState: View {
    let icon: String
    let title: String
    let detail: String

    var body: some View {
        VStack(spacing: 7) {
            Image(systemName: icon)
                .font(.title2)
                .foregroundStyle(.secondary)
            Text(title)
                .font(.callout.weight(.medium))
            Text(detail)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .lineLimit(3)
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 18)
    }
}

private struct LoadingRow: View {
    let text: String

    init(_ text: String) {
        self.text = text
    }

    var body: some View {
        HStack(spacing: 10) {
            ProgressView()
                .controlSize(.small)
            Text(text)
                .font(.callout)
                .foregroundStyle(.secondary)
            Spacer()
        }
    }
}

private func deviceSort(_ lhs: Device, _ rhs: Device) -> Bool {
    if lhs.sortRank != rhs.sortRank {
        return lhs.sortRank < rhs.sortRank
    }
    return lhs.name.localizedStandardCompare(rhs.name) == .orderedAscending
}

private extension Device {
    var sortRank: Int {
        if canInstallNow { return 0 }
        if needsTunnelConnection { return 1 }
        if needsFirstSetup { return 2 }
        return 3
    }
}

private extension AppState {
    var uploadTitle: String {
        if isUploading {
            return "Uploading IPA"
        }
        if let upload {
            return upload.info.bundleName.isEmpty ? selectedIPAURL?.lastPathComponent ?? "IPA selected" : upload.info.bundleName
        }
        return "App"
    }

    var uploadDetail: String {
        if let upload {
            var parts = [upload.info.bundleID]
            if !upload.info.version.isEmpty {
                parts.append("v\(upload.info.version)")
            }
            if !upload.info.minOS.isEmpty {
                parts.append("iOS \(upload.info.minOS)+")
            }
            return parts.filter { !$0.isEmpty }.joined(separator: " · ")
        }
        return selectedIPAURL?.lastPathComponent ?? "Choose or drop an .ipa file."
    }

    var uploadStatus: String? {
        if isUploading {
            return "Uploading"
        }
        return upload == nil ? nil : "Ready"
    }
}
