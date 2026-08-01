import Foundation

struct Device: Codable, Hashable, Identifiable, Sendable {
    let name: String
    let model: String?
    let udid: String
    let host: String
    let port: Int
    let service: String
    let deviceClass: String
    let connection: String?
    let installable: Bool
    let needsSetup: Bool
    let paired: Bool?
    let requiresTunnel: Bool?
    let tunnelActive: Bool?

    var id: String { udid }

    var platformLabel: String {
        switch deviceClass {
        case "ios": "iPhone"
        case "ipados": "iPad"
        case "tvos": "Apple TV"
        case "macos": "Mac"
        case "homepod": "HomePod"
        default: "Apple Device"
        }
    }

    var serviceLabel: String {
        if canRunSetup && !canInstallNow {
            return needsFirstSetup ? "Setup required" : "Connect tunnel"
        }
        if service.contains("remotepairing") {
            return needsFirstSetup ? "Setup required" : "Tunnel ready"
        }
        if service.contains("mobdev2") {
            return "Direct install"
        }
        if isPhysicalUSB {
            return "USB install"
        }
        if service == "usbmux" {
            return "Wi-Fi install"
        }
        if service.contains("airplay") {
            return "AirPlay only"
        }
        if service.contains("companion-link") {
            return "Nearby only"
        }
        return "Not installable"
    }

    var canInstallNow: Bool {
        installable && !needsSetup
    }

    var isPaired: Bool {
        paired == true || tunnelActive == true
    }

    var needsFirstSetup: Bool {
        needsSetup && !isPaired
    }

    var needsTunnelConnection: Bool {
        canRunSetup && !canInstallNow && !needsFirstSetup
    }

    var setupActionTitle: String {
        needsFirstSetup ? "Setup" : "Connect"
    }

    var canRunSetup: Bool {
        needsSetup || isPaired || service.contains("remotepairing") || isAppleTVSetupCandidate
    }

    var isRelevantForInstall: Bool {
        canInstallNow || canRunSetup
    }

    var statusLabel: String {
        if canInstallNow {
            if isPhysicalUSB {
                return "Ready · USB"
            }
            if service == "usbmux" {
                return "Ready · Wi-Fi"
            }
            return service.contains("mobdev2") ? "Ready · Direct" : "Ready"
        }
        if needsFirstSetup {
            return "Needs setup"
        }
        if needsTunnelConnection {
            return "Paired"
        }
        return serviceLabel
    }

    var displayDetail: String {
        "\(platformLabel) · \(displayEndpoint)"
    }

    private var displayEndpoint: String {
        if host.hasPrefix("usb:") {
            return isPhysicalUSB ? "USB" : "Wi-Fi"
        }
        return host
    }

    private var isPhysicalUSB: Bool {
        connection?.lowercased() == "usb"
    }

    private var isAppleTVSetupCandidate: Bool {
        guard deviceClass == "tvos" || (model?.contains("AppleTV") ?? false) else {
            return false
        }
        return service.contains("companion-link") || service.contains("airplay")
    }

    var setupComplete: Device {
        Device(
            name: name,
            model: model,
            udid: udid,
            host: host,
            port: port,
            service: service,
            deviceClass: deviceClass,
            connection: connection,
            installable: true,
            needsSetup: false,
            paired: true,
            requiresTunnel: requiresTunnel,
            tunnelActive: true
        )
    }

    enum CodingKeys: String, CodingKey {
        case name
        case model
        case udid
        case host
        case port
        case service
        case deviceClass = "device_class"
        case connection
        case installable
        case needsSetup = "needs_setup"
        case paired
        case requiresTunnel = "requires_tunnel"
        case tunnelActive = "tunnel_active"
    }
}

struct DeviceListResponse: Decodable, Sendable {
    let devices: [Device]
    let error: String?
}

struct StatusResponse: Codable, Sendable {
    let status: String?
    let message: String?
    let error: String?
    let state: String?
    let authenticated: Bool?
    let appleID: String?
    let authType: String?

    enum CodingKeys: String, CodingKey {
        case status
        case message
        case error
        case state
        case authenticated
        case appleID = "apple_id"
        case authType = "auth_type"
    }

    var displayMessage: String {
        message ?? error ?? status ?? state ?? ""
    }
}

struct IPAInfo: Codable, Hashable, Sendable {
    let bundleID: String
    let bundleName: String
    let version: String
    let build: String
    let minOS: String
    let executable: String

    enum CodingKeys: String, CodingKey {
        case bundleID = "bundle_id"
        case bundleName = "bundle_name"
        case version
        case build
        case minOS = "min_os"
        case executable
    }
}

struct UploadResponse: Codable, Hashable, Sendable {
    let path: String
    let info: IPAInfo
}

struct AccountInfo: Codable, Sendable {
    let team: TeamInfo
    let apps: [ProvisionedApp]
    let appCount: Int
    let appLimit: Int
    let autoRefreshWindowHours: Int?
    let appleID: String
    let sync: SyncInfo?

    enum CodingKeys: String, CodingKey {
        case team
        case apps
        case appCount = "app_count"
        case appLimit = "app_limit"
        case autoRefreshWindowHours = "auto_refresh_window_hours"
        case appleID = "apple_id"
        case sync
    }
}

struct SyncInfo: Codable, Hashable, Sendable {
    let status: String?
    let provider: String
    let configured: Bool
    let portableKey: Bool?
    let folder: String?
    let r2Endpoint: String?
    let r2Bucket: String?
    let uploadedIPAs: Int?
    let downloadedIPAs: Int?
    let installCount: Int?
    /// disabled | needs_setup | needs_icloud | locked | ok | wrong_key
    let vaultState: String?
    let vaultBytes: Int?
    let icloudAvailable: Bool?
    let icloudPath: String?
    let haveRecoveryKey: Bool?

    enum CodingKeys: String, CodingKey {
        case status
        case provider
        case configured
        case portableKey = "portable_key"
        case folder
        case r2Endpoint = "r2_endpoint"
        case r2Bucket = "r2_bucket"
        case uploadedIPAs = "uploaded_ipas"
        case downloadedIPAs = "downloaded_ipas"
        case installCount = "install_count"
        case vaultState = "vault_state"
        case vaultBytes = "vault_bytes"
        case icloudAvailable = "icloud_available"
        case icloudPath = "icloud_path"
        case haveRecoveryKey = "have_recovery_key"
    }

    /// Falls back to the pre-vault `status` field so an older backend still renders.
    var resolvedState: String {
        vaultState ?? status ?? (configured ? "locked" : "disabled")
    }
}

struct RecoveryKeyResponse: Codable, Hashable, Sendable {
    let status: String
    let recoveryKey: String?
    let message: String?

    enum CodingKeys: String, CodingKey {
        case status
        case recoveryKey = "recovery_key"
        case message
    }
}

struct WakeCommandResponse: Codable, Hashable, Sendable {
    let status: String
    let command: String
    let note: String
}

struct TeamInfo: Codable, Sendable {
    let name: String
    let teamID: String
    let type: String
    let isFree: Bool

    enum CodingKeys: String, CodingKey {
        case name
        case teamID = "team_id"
        case type
        case isFree = "is_free"
    }
}

struct ProvisionedApp: Codable, Hashable, Identifiable, Sendable {
    let rowID: String?
    let name: String
    let identifier: String
    let appIDID: String
    let isCatapult: Bool
    let isExtension: Bool?
    let parentIdentifier: String?
    let parentName: String?
    let extensionName: String?
    let expiry: String?
    let expiresAt: String?
    let timeLeft: String?
    let daysLeft: Int?
    let installed: String?
    let installedDevice: String?
    let autoRefreshAfter: String?
    let autoRefreshEligible: Bool?
    let canReinstall: Bool?
    let savedDeviceName: String?
    let savedIPAExists: Bool?
    let reinstallBlockedReason: String?
    let accountSlotExists: Bool?
    let historyOnly: Bool?
    let isExpired: Bool?

    var id: String { rowID?.isEmpty == false ? rowID! : (appIDID.isEmpty ? identifier : appIDID) }
    var extensionSlot: Bool { isExtension == true }
    var reinstallable: Bool { canReinstall == true }
    var hasAccountSlot: Bool { accountSlotExists != false }
    var historyOnlyRow: Bool { historyOnly == true }
    var expired: Bool { isExpired == true }

    enum CodingKeys: String, CodingKey {
        case rowID = "row_id"
        case name
        case identifier
        case appIDID = "app_id_id"
        case isCatapult = "is_catapult"
        case isExtension = "is_extension"
        case parentIdentifier = "parent_identifier"
        case parentName = "parent_name"
        case extensionName = "extension_name"
        case expiry
        case expiresAt = "expires_at"
        case timeLeft = "time_left"
        case daysLeft = "days_left"
        case installed
        case installedDevice = "installed_device"
        case autoRefreshAfter = "auto_refresh_after"
        case autoRefreshEligible = "auto_refresh_eligible"
        case canReinstall = "can_reinstall"
        case savedDeviceName = "saved_device_name"
        case savedIPAExists = "saved_ipa_exists"
        case reinstallBlockedReason = "reinstall_blocked_reason"
        case accountSlotExists = "account_slot_exists"
        case historyOnly = "history_only"
        case isExpired = "is_expired"
    }
}

struct InstallMessage: Codable, Hashable, Sendable {
    let step: String
    let progress: Int
    let message: String
}

struct APIErrorPayload: Codable, Sendable {
    let status: String?
    let message: String?
    let error: String?
    let detail: String?

    var displayMessage: String {
        message ?? error ?? detail ?? status ?? "Request failed"
    }
}

enum CatapultError: LocalizedError {
    case backendUnavailable(String)
    case requestFailed(String)
    case invalidResponse
    case missingBackendRoot
    case missingUV

    var errorDescription: String? {
        switch self {
        case .backendUnavailable(let message): message
        case .requestFailed(let message): message
        case .invalidResponse: "The backend returned an invalid response."
        case .missingBackendRoot: "Could not locate the Catapult backend."
        case .missingUV: "Could not find uv. Install uv or set CATAPULT_UV."
        }
    }
}
