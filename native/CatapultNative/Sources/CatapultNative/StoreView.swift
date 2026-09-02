import SwiftUI

/// Browse and install apps published by sources you add.
///
/// The catalog is filtered to the selected device, so an Apple TV never sees an
/// iOS build. Installs run through the same pipeline as the Install tab; the
/// update badge comes from comparing the installed version with the source.
private enum StoreFilter: Hashable {
    case all, installed, new
}

struct StoreView: View {
    @EnvironmentObject private var state: AppState
    @State private var newSourceURL = ""
    @State private var showingSources = false
    @State private var filter: StoreFilter = .all
    @State private var searchText = ""

    private var query: String {
        searchText.trimmingCharacters(in: .whitespaces)
    }

    /// Apps that pass the All / Installed / New filter and the search field.
    /// "Installed" includes anything installed before, on any device, not
    /// only Store installs.
    private var visibleApps: [StoreApp] {
        state.storeApps.filter { app in
            passesFilter(app) && matchesQuery(app)
        }
    }

    private func passesFilter(_ app: StoreApp) -> Bool {
        switch filter {
        case .all: true
        case .installed: app.wasInstalledBefore
        case .new: !app.wasInstalledBefore
        }
    }

    private func matchesQuery(_ app: StoreApp) -> Bool {
        query.isEmpty
            || app.name.localizedCaseInsensitiveContains(query)
            || app.developer.localizedCaseInsensitiveContains(query)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            Divider()
            if state.isInstalling || state.installProgress > 0 {
                installProgress
                Divider()
            }
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
                    .lineLimit(1)
            }
            Spacer()
            searchField
            Picker("Show", selection: $filter) {
                Text("All").tag(StoreFilter.all)
                Text("Installed").tag(StoreFilter.installed)
                Text("New").tag(StoreFilter.new)
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .frame(width: 220)
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

    private var searchField: some View {
        HStack(spacing: 5) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(.secondary)
            TextField("Search apps", text: $searchText)
                .textFieldStyle(.plain)
            if !searchText.isEmpty {
                Button {
                    searchText = ""
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.borderless)
            }
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 4)
        .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color(nsColor: .separatorColor)))
        .frame(width: 200)
    }

    /// Same progress strip as the Install tab. A Store install used to run with
    /// no feedback on this tab at all.
    private var installProgress: some View {
        VStack(alignment: .leading, spacing: 6) {
            ProgressView(value: Double(state.installProgress), total: 100)
            Text(state.installMessage.isEmpty ? "Preparing…" : state.installMessage)
                .font(.caption)
                .foregroundStyle(state.installProgress >= 100 ? .green : .secondary)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
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
        } else if visibleApps.isEmpty {
            filterEmptyState
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
                    ForEach(visibleApps) { app in
                        StoreAppRow(app: app)
                    }
                }
                .padding(16)
            }
        }
    }

    @ViewBuilder
    private var filterEmptyState: some View {
        if query.isEmpty {
            EmptyState(
                icon: "line.3.horizontal.decrease.circle",
                title: filter == .installed ? "Nothing installed before" : "Nothing new",
                detail: "No apps match the current filter."
            )
        } else {
            EmptyState(
                icon: "magnifyingglass",
                title: "No matches",
                detail: "No apps match “\(query)”."
            )
        }
    }

    private func addSource() async {
        let url = newSourceURL
        newSourceURL = ""
        await state.addStoreSource(url: url)
    }
}

// MARK: - Row

private struct StoreAppRow: View {
    @EnvironmentObject private var state: AppState
    let app: StoreApp

    var body: some View {
        HStack(spacing: 12) {
            StoreAppIcon(name: app.name, icon: app.icon)

            VStack(alignment: .leading, spacing: 3) {
                FlowLayout(spacing: 6) {
                    Text(app.name)
                        .font(.callout.weight(.semibold))
                        .lineLimit(1)
                    pills
                }
                Text(app.metaLabel)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            // Without this the stack offers the text half the row before the
            // spacer yields, and pills wrap in a window that has room.
            .layoutPriority(1)

            Spacer(minLength: 12)

            VStack(alignment: .trailing, spacing: 4) {
                installButton
                if app.isInstalled {
                    // Checks daily, installs when the device is connected.
                    Toggle("Update automatically", isOn: Binding(
                        get: { app.autoUpdate ?? false },
                        set: { enabled in Task { await state.setStoreAutoUpdate(app, enabled: enabled) } }
                    ))
                    .toggleStyle(.checkbox)
                    .controlSize(.small)
                    .font(.caption2)
                    .disabled(state.isInstalling)
                }
            }
        }
        .padding(12)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
    }

    @ViewBuilder
    private var pills: some View {
        if app.isInstalled, let installed = app.installedVersion {
            StorePill("Installed \(installed)", tint: .green)
        }
        if app.updateAvailable == true {
            StorePill("Update available", tint: .orange)
        }
        if app.installedBefore == true, !app.isInstalled {
            // Matched to an install record from any device, including hand
            // installs that predate the Store.
            let devices = (app.installedOn ?? []).joined(separator: ", ")
            StorePill(devices.isEmpty ? "Installed before" : "Installed before on \(devices)")
        }
        if app.prerelease {
            StorePill("pre-release", tint: .orange, outlined: true)
        }
    }

    private var installButton: some View {
        Button {
            Task { await state.installFromStore(app) }
        } label: {
            Text(app.updateAvailable == true ? "Update" : (app.isInstalled ? "Reinstall" : "Install"))
                .frame(minWidth: 64)
        }
        .buttonStyle(.borderedProminent)
        // Mirror the Install tab: a signed-in user and a device that can
        // take an install right now. An Apple TV without its tunnel, or an
        // untrusted iPad, goes through Setup first instead of failing here.
        .disabled(!state.isAuthenticated || state.isInstalling
                  || !(state.selectedDevice?.canInstallNow ?? false))
    }
}

private struct StorePill: View {
    let title: String
    let tint: Color
    let outlined: Bool

    init(_ title: String, tint: Color = .secondary, outlined: Bool = false) {
        self.title = title
        self.tint = tint
        self.outlined = outlined
    }

    var body: some View {
        Text(title)
            .font(.caption2)
            .lineLimit(1)
            .foregroundStyle(tint)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background {
                if outlined {
                    Capsule().strokeBorder(tint.opacity(0.6))
                } else {
                    Capsule().fill(tint.opacity(0.15))
                }
            }
    }
}

// MARK: - Icon

/// An app tile: the resolved icon when there is one, a monogram otherwise.
///
/// The monogram also stands in while the image loads and when the load fails,
/// so a row never shows a blank square.
private struct StoreAppIcon: View {
    @EnvironmentObject private var state: AppState
    let name: String
    let icon: String?
    var size: CGFloat = 44

    var body: some View {
        if let url = iconURL {
            AsyncImage(url: url) { phase in
                if let image = phase.image {
                    image
                        .resizable()
                        .interpolation(.high)
                        .scaledToFill()
                        .frame(width: size, height: size)
                        .clipShape(tile)
                } else {
                    monogram
                }
            }
            .frame(width: size, height: size)
        } else {
            monogram
        }
    }

    /// An absolute URL passes through untouched; a backend-relative path such
    /// as "/api/store/icon?sha=…" resolves against the API client's base.
    private var iconURL: URL? {
        guard let icon, !icon.isEmpty else {
            return nil
        }
        return URL(string: icon, relativeTo: state.client.baseURL)?.absoluteURL
    }

    private var tile: RoundedRectangle {
        RoundedRectangle(cornerRadius: size * 0.23)
    }

    private var monogram: some View {
        Text(initials)
            .font(.system(size: size * 0.4, weight: .semibold, design: .rounded))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(Color(hue: hue, saturation: 0.55, brightness: 0.72), in: tile)
    }

    private var initials: String {
        name.split(whereSeparator: \.isWhitespace)
            .prefix(2)
            .compactMap { $0.first }
            .map { String($0).uppercased() }
            .joined()
    }

    /// FNV-1a over the name's scalars. Swift's own `hashValue` is seeded per
    /// launch, and a tile must keep its colour from one run to the next.
    private var hue: Double {
        var hash: UInt32 = 2_166_136_261
        for scalar in name.unicodeScalars {
            hash ^= scalar.value
            hash = hash &* 16_777_619
        }
        return Double(hash % 360) / 360
    }
}

// MARK: - Layout

/// Lays children out left to right and wraps like text, so the pills on the
/// name line drop to a second line in a narrow window instead of squeezing
/// the name.
private struct FlowLayout: Layout {
    var spacing: CGFloat = 6

    private struct Line {
        var items: [(index: Int, size: CGSize)] = []
        var width: CGFloat = 0
        var height: CGFloat = 0
    }

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let lines = arrange(proposal: proposal, subviews: subviews)
        let width = lines.map(\.width).max() ?? 0
        let height = lines.map(\.height).reduce(0, +) + spacing * CGFloat(max(lines.count - 1, 0))
        return CGSize(width: width, height: height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var y = bounds.minY
        for line in arrange(proposal: proposal, subviews: subviews) {
            var x = bounds.minX
            for item in line.items {
                let origin = CGPoint(x: x, y: y + (line.height - item.size.height) / 2)
                subviews[item.index].place(at: origin, proposal: ProposedViewSize(item.size))
                x += item.size.width + spacing
            }
            y += line.height + spacing
        }
    }

    private func arrange(proposal: ProposedViewSize, subviews: Subviews) -> [Line] {
        let maxWidth = proposal.width ?? .infinity
        var lines: [Line] = []
        var line = Line()
        for (index, subview) in subviews.enumerated() {
            // Capping each child at the full width lets a long name truncate
            // rather than run past the row.
            let size = subview.sizeThatFits(ProposedViewSize(width: maxWidth, height: nil))
            let joined = line.width + spacing + size.width
            if joined > maxWidth, !line.items.isEmpty {
                lines.append(line)
                line = Line()
            }
            line.items.append((index, size))
            line.width = line.items.count == 1 ? size.width : joined
            line.height = max(line.height, size.height)
        }
        if !line.items.isEmpty {
            lines.append(line)
        }
        return lines
    }
}
