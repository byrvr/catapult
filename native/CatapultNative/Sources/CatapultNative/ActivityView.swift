import SwiftUI

struct ActivitySheet: View {
    @Environment(\.dismiss) private var dismiss

    let jobs: [ActivityJob]
    let isLoading: Bool
    let onRefresh: () -> Void
    let onCopyDiagnostics: (ActivityJob) -> Void

    init(
        jobs: [ActivityJob],
        isLoading: Bool = false,
        onRefresh: @escaping () -> Void = {},
        onCopyDiagnostics: @escaping (ActivityJob) -> Void = { _ in }
    ) {
        self.jobs = jobs
        self.isLoading = isLoading
        self.onRefresh = onRefresh
        self.onCopyDiagnostics = onCopyDiagnostics
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Activity")
                        .font(.title2.weight(.semibold))
                    Text("Recent jobs and diagnostics")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button {
                    onRefresh()
                } label: {
                    if isLoading {
                        HStack(spacing: 8) {
                            ProgressView()
                                .controlSize(.small)
                            Text("Loading")
                        }
                    } else {
                        Label("Reload", systemImage: CatapultIcon.refresh)
                    }
                }
                .disabled(isLoading)
                Button("Close") { dismiss() }
            }

            if isLoading && jobs.isEmpty {
                ActivityLoadingState("Loading activity...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if jobs.isEmpty {
                ActivityEmptyState()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(spacing: 10) {
                        ForEach(jobs) { job in
                            ActivityJobRow(job: job) {
                                onCopyDiagnostics(job)
                            }
                        }
                    }
                    .padding(.vertical, 2)
                }
            }
        }
        .padding(24)
        .frame(width: 680, height: 560)
    }
}

struct ActivityJobRow: View {
    let job: ActivityJob
    let onCopyDiagnostics: () -> Void

    @State private var showEvents = false

    private var events: [ActivityEvent] {
        job.events ?? []
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 12) {
                CatapultIconTile(
                    systemName: icon,
                    tint: statusColor(for: job.status),
                    dimension: 30,
                    font: .callout
                )

                VStack(alignment: .leading, spacing: 5) {
                    HStack(spacing: 8) {
                        Text(job.title.activityText ?? job.kind.activityDisplayLabel)
                            .font(.callout.weight(.semibold))
                            .lineLimit(1)

                        CatapultStatusPill(title: job.status.activityDisplayLabel, color: statusColor(for: job.status))
                    }

                    if let detail {
                        Text(detail)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                }

                Spacer(minLength: 10)

                Button {
                    onCopyDiagnostics()
                } label: {
                    Label("Copy", systemImage: CatapultIcon.copy)
                }
                .buttonStyle(.borderless)
                .help("Copy diagnostics")
            }

            if let progressValue {
                HStack(spacing: 8) {
                    ProgressView(value: progressValue, total: 100)
                        .controlSize(.small)
                    Text("\(Int(progressValue.rounded()))%")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .frame(width: 38, alignment: .trailing)
                }
            }

            if let message = job.message.activityText {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(messageColor)
                    .lineLimit(3)
            }

            if hasFinalError {
                VStack(alignment: .leading, spacing: 4) {
                    Label(errorTitle, systemImage: CatapultIcon.warning)
                        .font(.caption.weight(.semibold))
                    if let errorDetail = job.errorDetail.activityText {
                        Text(errorDetail)
                            .font(.caption)
                            .lineLimit(4)
                    }
                }
                .foregroundStyle(.red)
            }

            if !events.isEmpty {
                Divider()
                DisclosureGroup(isExpanded: $showEvents) {
                    VStack(alignment: .leading, spacing: 8) {
                        ForEach(events) { event in
                            ActivityEventLine(event: event)
                        }
                    }
                    .padding(.top, 6)
                } label: {
                    Text("\(events.count) \(events.count == 1 ? "event" : "events")")
                        .font(.caption.weight(.medium))
                }
            }
        }
        .padding(12)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
        .overlay {
            RoundedRectangle(cornerRadius: 10)
                .stroke(statusColor(for: job.status).opacity(hasFinalError ? 0.35 : 0.14), lineWidth: 1)
        }
    }

    private var detail: String? {
        let target = job.target.activityText
        let timeRange = timestampRange(startedAt: job.startedAt, finishedAt: job.finishedAt)

        switch (target, timeRange) {
        case (.some(let target), .some(let timeRange)):
            return "\(target) - \(timeRange)"
        case (.some(let target), .none):
            return target
        case (.none, .some(let timeRange)):
            return timeRange
        case (.none, .none):
            return nil
        }
    }

    private var icon: String {
        CatapultIcon.activityKind(job.kind)
    }

    private var progressValue: Double? {
        guard let progress = job.progress else {
            return nil
        }
        return min(max(progress, 0), 100)
    }

    private var hasFinalError: Bool {
        job.errorCategory.activityText != nil || job.errorDetail.activityText != nil || isErrorStatus(job.status)
    }

    private var errorTitle: String {
        job.errorCategory.activityText?.activityDisplayLabel ?? "Job failed"
    }

    private var messageColor: Color {
        isErrorStatus(job.status) ? .red : .secondary
    }
}

private struct ActivityEventLine: View {
    let event: ActivityEvent

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Circle()
                .fill(statusColor(for: event.status ?? event.kind ?? "event"))
                .frame(width: 7, height: 7)
                .padding(.top, 5)

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(event.title.activityText ?? event.kind?.activityDisplayLabel ?? "Event")
                        .font(.caption.weight(.semibold))
                        .lineLimit(1)

                    if let status = event.status.activityText {
                        Text(status.activityDisplayLabel)
                            .font(.caption2.weight(.medium))
                            .foregroundStyle(statusColor(for: status))
                    }

                    Spacer(minLength: 6)

                    if let progress = event.progress {
                        Text("\(Int(min(max(progress, 0), 100).rounded()))%")
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }

                if let message = event.message.activityText {
                    Text(message)
                        .font(.caption2)
                        .foregroundStyle(isErrorStatus(event.status) ? .red : .secondary)
                        .lineLimit(2)
                }

                if let timeRange = timestampRange(startedAt: event.startedAt, finishedAt: event.finishedAt) {
                    Text(timeRange)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
        }
    }
}

private struct ActivityEmptyState: View {
    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: CatapultIcon.activity)
                .font(.title2)
                .foregroundStyle(.secondary)
            Text("No activity yet")
                .font(.callout.weight(.medium))
            Text("Upload, setup, install, reinstall, and refresh jobs will appear here.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .lineLimit(2)
        }
        .padding(.vertical, 28)
    }
}

private struct ActivityLoadingState: View {
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
        }
    }
}

private func statusColor(for status: String?) -> Color {
    switch status?.lowercased() {
    case "done", "complete", "completed", "success", "succeeded":
        return .green
    case "failed", "failure", "error":
        return .red
    case "running", "active", "installing", "signing", "pairing":
        return .blue
    case "queued", "pending", "waiting":
        return .orange
    case "cancelled", "canceled":
        return .secondary
    default:
        return .secondary
    }
}

private func isErrorStatus(_ status: String?) -> Bool {
    switch status?.lowercased() {
    case "failed", "failure", "error":
        return true
    default:
        return false
    }
}

private func timestampRange(startedAt: String?, finishedAt: String?) -> String? {
    let started = formattedTimestamp(startedAt)
    let finished = formattedTimestamp(finishedAt)

    switch (started, finished) {
    case (.some(let started), .some(let finished)):
        return "\(started) - \(finished)"
    case (.some(let started), .none):
        return "Started \(started)"
    case (.none, .some(let finished)):
        return "Finished \(finished)"
    case (.none, .none):
        return nil
    }
}

private func formattedTimestamp(_ value: String?) -> String? {
    guard let value = value.activityText else {
        return nil
    }

    let fractionalParser = ISO8601DateFormatter()
    fractionalParser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    let standardParser = ISO8601DateFormatter()
    standardParser.formatOptions = [.withInternetDateTime]

    let date = fractionalParser.date(from: value) ?? standardParser.date(from: value)
    guard let date else {
        return value
    }

    let formatter = DateFormatter()
    formatter.dateStyle = .none
    formatter.timeStyle = .short
    return formatter.string(from: date)
}

private extension Optional where Wrapped == String {
    var activityText: String? {
        guard let value = self?.trimmingCharacters(in: .whitespacesAndNewlines), !value.isEmpty else {
            return nil
        }
        return value
    }
}

private extension String {
    var activityDisplayLabel: String {
        replacingOccurrences(of: "_", with: " ").localizedCapitalized
    }
}
