import SwiftUI

/// Browse and install apps published by sources you add.
///
/// The catalog is filtered to the selected device, so an Apple TV never sees an
/// iOS build. Auto-update is opt-in per app and only installs when the device is
/// actually reachable.
struct StoreView: View {
    @EnvironmentObject private var state: AppState
    @State private var newSourceURL = ""
    @State private var showingSources = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            if showingSources {
                sourcesPane
                Divider()
            }
            content
        }
        .task { await state.loadStore() }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text("Store")
                    .font(.title3.weight(.semibold))
                Text(subtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                showingSources.toggle()
            } label: {
                Label("Sources", systemImage: "link")
            }
            Button {
                Task { await state.loadStore(force: true) }
            } label: {
                Label("Refresh", systemImage: CatapultIcon.refresh)
            }
            .disabled(state.isLoadingStore)
        }
        .padding(16)
    }

    private var subtitle: String {
        if state.storeSources.isEmpty {
            return "Add a GitHub repository or an AltStore source to get started."
        }
        if let device = state.selectedDevice {
            return "Showing apps for \(device.name)"
        }
        return "Select a device to see what fits it"
    }

    // MARK: - Sources

    private var sourcesPane: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                TextField("VortXTV/VortX  or  https://…/apps.json", text: $newSourceURL)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await addSource() } }
                Button("Add") { Task { await addSource() } }
                    .disabled(newSourceURL.isEmpty || state.isLoadingStore)
            }
            if state.storeSources.isEmpty {
                Text("Catapult ships with no apps of its own. You choose where they come from.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ForEach(state.storeSources) { source in
                HStack {
                    Image(systemName: source.kind == "github" ? "chevron.left.forwardslash.chevron.right" : "shippingbox")
                        .foregroundStyle(.secondary)
                    Text(source.displayName)
                        .font(.caption.monospaced())
                    Spacer()
                    Button(role: .destructive) {
                        Task { await state.removeStoreSource(id: source.id) }
                    } label: {
                        Image(systemName: CatapultIcon.delete)
                    }
                    .buttonStyle(.borderless)
                }
            }
            ForEach(state.storeErrors, id: \.sourceId) { error in
                Label("\(error.sourceId): \(error.message)", systemImage: CatapultIcon.warning)
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
        }
        .padding(16)
    }

    // MARK: - Catalog

    @ViewBuilder
    private var content: some View {
        if state.isLoadingStore && state.storeApps.isEmpty {
            ProgressView("Loading catalog…")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if state.storeSources.isEmpty {
            EmptyState(
                icon: "shippingbox",
                title: "No sources yet",
                detail: "Add a GitHub repository that publishes .ipa files in its releases, or an AltStore source URL."
            )
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if state.storeApps.isEmpty {
            let detail = state.selectedDevice == nil
                ? "Select a device first — Catapult only shows builds that fit it."
                : "This source publishes no build for the selected device."
            EmptyState(icon: "shippingbox", title: "Nothing to install here", detail: detail)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ScrollView {
                if state.storeFreeTeam {
                    Label(
                        "Your Apple account is free-tier: 10 App IDs per 7 days and 3 installed apps. A store will exhaust that quickly.",
                        systemImage: CatapultIcon.warning
                    )
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .padding(.horizontal, 16)
                    .padding(.top, 12)
                }
                LazyVStack(spacing: 8) {
                    ForEach(state.storeApps) { app in
                        StoreAppRow(app: app)
                    }
                }
                .padding(16)
            }
        }
    }

    private func addSource() async {
        let url = newSourceURL
        newSourceURL = ""
        await state.addStoreSource(url: url)
    }
}

private struct StoreAppRow: View {
    @EnvironmentObject private var state: AppState
    let app: StoreApp

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: app.platform == "tvos" ? "appletv" : "iphone")
                .font(.title3)
                .foregroundStyle(.secondary)
                .frame(width: 34)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(app.name)
                        .font(.callout.weight(.semibold))
                    if app.prerelease {
                        Text("pre-release")
                            .font(.caption2)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Color.orange.opacity(0.18), in: Capsule())
                    }
                }
                Text("\(app.version) · \(app.platformLabel) · \(app.sizeLabel)")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                if app.isInstalled, let installed = app.installedVersion {
                    Text(app.updateAvailable == true
                         ? "Installed \(installed) — update available"
                         : "Installed \(installed)")
                        .font(.caption2)
                        .foregroundStyle(app.updateAvailable == true ? Color.orange : .secondary)
                }
            }

            Spacer()

            Button {
                Task { await state.installFromStore(app) }
            } label: {
                Text(app.updateAvailable == true ? "Update" : (app.isInstalled ? "Reinstall" : "Install"))
                    .frame(minWidth: 64)
            }
            .buttonStyle(.borderedProminent)
            .disabled(state.selectedDevice == nil || state.isInstalling)
        }
        .padding(12)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
    }
}
