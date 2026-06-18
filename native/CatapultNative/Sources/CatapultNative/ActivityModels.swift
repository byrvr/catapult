import Foundation

struct ActivityListResponse: Codable, Hashable, Sendable {
    let jobs: [ActivityJob]
}

struct ActivityJob: Codable, Hashable, Identifiable, Sendable {
    let id: String
    let kind: String
    let title: String?
    let target: String?
    let status: String
    let progress: Double?
    let message: String?
    let startedAt: String?
    let finishedAt: String?
    let events: [ActivityEvent]?
    let errorCategory: String?
    let errorDetail: String?

    enum CodingKeys: String, CodingKey {
        case id
        case kind
        case title
        case target
        case status
        case progress
        case message
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case events
        case errorCategory = "error_category"
        case errorDetail = "error_detail"
    }
}

struct ActivityEvent: Codable, Hashable, Identifiable, Sendable {
    let id: String
    let kind: String?
    let title: String?
    let target: String?
    let status: String?
    let progress: Double?
    let message: String?
    let startedAt: String?
    let finishedAt: String?
    let errorCategory: String?
    let errorDetail: String?

    enum CodingKeys: String, CodingKey {
        case id
        case kind
        case title
        case target
        case status
        case progress
        case message
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case errorCategory = "error_category"
        case errorDetail = "error_detail"
    }
}

struct DiagnosticsBundle: Codable, Hashable, Sendable {
    let id: String?
    let kind: String?
    let title: String?
    let target: String?
    let status: String?
    let progress: Double?
    let message: String?
    let startedAt: String?
    let finishedAt: String?
    let events: [ActivityEvent]?
    let errorCategory: String?
    let errorDetail: String?
    let jobs: [ActivityJob]?

    enum CodingKeys: String, CodingKey {
        case id
        case kind
        case title
        case target
        case status
        case progress
        case message
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case events
        case errorCategory = "error_category"
        case errorDetail = "error_detail"
        case jobs
    }
}

extension ActivityJob {
    static let sampleJobs: [ActivityJob] = [
        ActivityJob(
            id: "install-20260618-091243",
            kind: "install",
            title: "Install Runner",
            target: "Living Room Apple TV",
            status: "running",
            progress: 68,
            message: "Installing signed package...",
            startedAt: "2026-06-18T09:12:43Z",
            finishedAt: nil,
            events: [
                ActivityEvent(
                    id: "install-20260618-091243-prepare",
                    kind: "prepare",
                    title: "Prepared IPA",
                    target: "Runner.ipa",
                    status: "done",
                    progress: 20,
                    message: "Read bundle metadata and entitlements.",
                    startedAt: "2026-06-18T09:12:43Z",
                    finishedAt: "2026-06-18T09:12:45Z",
                    errorCategory: nil,
                    errorDetail: nil
                ),
                ActivityEvent(
                    id: "install-20260618-091243-sign",
                    kind: "sign",
                    title: "Signed package",
                    target: "com.example.runner",
                    status: "done",
                    progress: 55,
                    message: "Provisioning profile embedded.",
                    startedAt: "2026-06-18T09:12:45Z",
                    finishedAt: "2026-06-18T09:12:49Z",
                    errorCategory: nil,
                    errorDetail: nil
                ),
                ActivityEvent(
                    id: "install-20260618-091243-device",
                    kind: "device_install",
                    title: "Device install",
                    target: "Living Room Apple TV",
                    status: "running",
                    progress: 68,
                    message: "Transferring app archive.",
                    startedAt: "2026-06-18T09:12:50Z",
                    finishedAt: nil,
                    errorCategory: nil,
                    errorDetail: nil
                )
            ],
            errorCategory: nil,
            errorDetail: nil
        ),
        ActivityJob(
            id: "setup-20260618-084508",
            kind: "device_setup",
            title: "Connect tunnel",
            target: "Office Apple TV",
            status: "failed",
            progress: 42,
            message: "Pairing session stopped before tunnel creation.",
            startedAt: "2026-06-18T08:45:08Z",
            finishedAt: "2026-06-18T08:46:31Z",
            events: [
                ActivityEvent(
                    id: "setup-20260618-084508-discover",
                    kind: "discover",
                    title: "Found target",
                    target: "Office Apple TV",
                    status: "done",
                    progress: 25,
                    message: "Resolved remote pairing service.",
                    startedAt: "2026-06-18T08:45:09Z",
                    finishedAt: "2026-06-18T08:45:12Z",
                    errorCategory: nil,
                    errorDetail: nil
                ),
                ActivityEvent(
                    id: "setup-20260618-084508-pair",
                    kind: "pair",
                    title: "Pairing",
                    target: "Office Apple TV",
                    status: "failed",
                    progress: 42,
                    message: "PIN entry expired.",
                    startedAt: "2026-06-18T08:45:12Z",
                    finishedAt: "2026-06-18T08:46:31Z",
                    errorCategory: "pairing_timeout",
                    errorDetail: "The pairing window closed before a PIN was submitted."
                )
            ],
            errorCategory: "pairing_timeout",
            errorDetail: "The pairing window closed before a PIN was submitted."
        )
    ]
}
