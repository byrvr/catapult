import Foundation

struct APIClient: Sendable {
    let baseURL: URL
    private let session: URLSession

    init(baseURL: URL = URL(string: "http://127.0.0.1:9450")!) {
        self.baseURL = baseURL
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 60
        config.timeoutIntervalForResource = 600
        self.session = URLSession(configuration: config)
    }

    func authStatus() async throws -> StatusResponse {
        try await get("/api/auth/status", as: StatusResponse.self)
    }

    func devices() async throws -> DeviceListResponse {
        try await get("/api/devices", as: DeviceListResponse.self, timeout: 18)
    }

    func pairStatus() async throws -> StatusResponse {
        try await get("/api/devices/pair-status", as: StatusResponse.self)
    }

    func accountInfo() async throws -> AccountInfo {
        try await get("/api/account/info", as: AccountInfo.self)
    }

    func activity() async throws -> ActivityListResponse {
        try await get("/api/activity", as: ActivityListResponse.self)
    }

    func syncStatus() async throws -> SyncInfo {
        try await get("/api/sync/status", as: SyncInfo.self)
    }

    func configureSync(provider: String, folder: String?) async throws -> SyncInfo {
        struct Body: Encodable {
            let provider: String
            let folder: String?
        }
        return try await postJSON(
            "/api/sync/configure",
            body: Body(provider: provider, folder: folder),
            as: SyncInfo.self
        )
    }

    func createVault() async throws -> RecoveryKeyResponse {
        try await postJSON("/api/sync/create-vault", body: [String: String](), as: RecoveryKeyResponse.self)
    }

    func unlockVault(recoveryKey: String) async throws -> StatusResponse {
        try await postJSON(
            "/api/sync/unlock",
            body: ["recovery_key": recoveryKey],
            as: StatusResponse.self
        )
    }

    func runSync() async throws -> SyncInfo {
        try await postJSON("/api/sync/run", body: [String: String](), as: SyncInfo.self)
    }

    func wakeCommand(hour: Int, minute: Int) async throws -> WakeCommandResponse {
        try await get("/api/power/wake-command?hour=\(hour)&minute=\(minute)", as: WakeCommandResponse.self)
    }

    func diagnosticsText() async throws -> String {
        var request = URLRequest(url: endpoint("/api/diagnostics"))
        request.httpMethod = "GET"
        request.timeoutInterval = 20
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw CatapultError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            throw CatapultError.requestFailed(errorMessage(from: data, fallback: "HTTP \(http.statusCode)"))
        }
        if let object = try? JSONSerialization.jsonObject(with: data),
           let pretty = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]),
           let text = String(data: pretty, encoding: .utf8) {
            return text
        }
        return String(data: data, encoding: .utf8) ?? ""
    }

    func login(appleID: String, password: String) async throws -> StatusResponse {
        try await postJSON(
            "/api/auth/login",
            body: ["apple_id": appleID, "password": password],
            as: StatusResponse.self
        )
    }

    func submit2FA(code: String) async throws -> StatusResponse {
        try await postJSON("/api/auth/2fa", body: ["code": code], as: StatusResponse.self)
    }

    func logout() async throws -> StatusResponse {
        try await postJSON("/api/auth/logout", body: [String: String](), as: StatusResponse.self)
    }

    func setupDevice(_ device: Device) async throws -> StatusResponse {
        try await postJSON(
            "/api/devices/setup",
            body: [
                "name": device.name,
                "udid": device.udid,
                "host": device.host,
            ],
            as: StatusResponse.self
        )
    }

    func submitPIN(_ pin: String) async throws -> StatusResponse {
        try await postJSON("/api/devices/pin", body: ["pin": pin], as: StatusResponse.self)
    }

    func deleteAppID(_ appIDID: String) async throws -> StatusResponse {
        try await postJSON(
            "/api/account/delete-app",
            body: ["app_id_id": appIDID],
            as: StatusResponse.self
        )
    }

    func reinstallApp(_ app: ProvisionedApp, onMessage: @MainActor @escaping (InstallMessage) -> Void) async throws {
        try await streamJob(
            path: "/ws/reinstall",
            payload: [
                "app_id_id": app.appIDID,
                "identifier": app.identifier,
            ],
            onMessage: onMessage
        )
    }

    func uploadIPA(fileURL: URL) async throws -> UploadResponse {
        var request = URLRequest(url: endpoint("/api/upload/raw"))
        request.httpMethod = "POST"
        request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        request.setValue(safeHeaderFilename(fileURL.lastPathComponent), forHTTPHeaderField: "X-Catapult-Filename")
        request.timeoutInterval = 120

        let (data, response) = try await session.upload(for: request, fromFile: fileURL)
        return try decode(UploadResponse.self, data: data, response: response)
    }

    func install(deviceUDID: String, ipaPath: String, onMessage: @MainActor @escaping (InstallMessage) -> Void) async throws {
        try await streamJob(
            path: "/ws/install",
            payload: [
                "device_udid": deviceUDID,
                "ipa_path": ipaPath,
            ],
            onMessage: onMessage
        )
    }

    private func streamJob(path: String, payload: [String: String], onMessage: @MainActor @escaping (InstallMessage) -> Void) async throws {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw CatapultError.invalidResponse
        }
        components.scheme = components.scheme == "https" ? "wss" : "ws"
        components.path = path
        guard let url = components.url else { throw CatapultError.invalidResponse }

        let socket = URLSession.shared.webSocketTask(with: url)
        socket.resume()
        defer {
            socket.cancel(with: .goingAway, reason: nil)
        }

        let payloadData = try JSONSerialization.data(withJSONObject: payload)
        guard let payloadText = String(data: payloadData, encoding: .utf8) else {
            throw CatapultError.invalidResponse
        }
        socket.send(.string(payloadText)) { error in
            if let error {
                Task { @MainActor in
                    onMessage(InstallMessage(step: "error", progress: 0, message: error.localizedDescription))
                }
            }
        }

        while true {
            let incoming = try await socket.receive()
            let data: Data
            switch incoming {
            case .data(let value):
                data = value
            case .string(let value):
                data = Data(value.utf8)
            @unknown default:
                throw CatapultError.invalidResponse
            }

            let message = try JSONDecoder().decode(InstallMessage.self, from: data)
            await onMessage(message)
            if message.step == "done" || message.step == "error" {
                break
            }
        }
    }

    private func get<T: Decodable>(_ path: String, as type: T.Type, timeout: TimeInterval? = nil) async throws -> T {
        var request = URLRequest(url: endpoint(path))
        request.httpMethod = "GET"
        if let timeout {
            request.timeoutInterval = timeout
        }
        let (data, response) = try await session.data(for: request)
        return try decode(type, data: data, response: response)
    }

    private func postJSON<T: Decodable, Body: Encodable>(_ path: String, body: Body, as type: T.Type) async throws -> T {
        var request = URLRequest(url: endpoint(path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONEncoder().encode(body)
        let (data, response) = try await session.data(for: request)
        return try decode(type, data: data, response: response)
    }

    private func endpoint(_ path: String) -> URL {
        URL(string: path, relativeTo: baseURL)!.absoluteURL
    }

    private func decode<T: Decodable>(_ type: T.Type, data: Data, response: URLResponse) throws -> T {
        guard let http = response as? HTTPURLResponse else {
            throw CatapultError.invalidResponse
        }
        guard (200..<300).contains(http.statusCode) else {
            throw CatapultError.requestFailed(errorMessage(from: data, fallback: "HTTP \(http.statusCode)"))
        }
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            let message = errorMessage(from: data, fallback: "")
            if !message.isEmpty {
                throw CatapultError.requestFailed(message)
            }
            throw error
        }
    }

    private func errorMessage(from data: Data, fallback: String) -> String {
        if let payload = try? JSONDecoder().decode(APIErrorPayload.self, from: data) {
            let message = payload.displayMessage
            if message != "Request failed" {
                return message
            }
        }
        if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            for key in ["message", "error", "detail", "status"] {
                if let value = object[key] as? String, !value.isEmpty {
                    return value
                }
            }
            if let details = object["detail"] as? [[String: Any]],
               let first = details.first,
               let message = first["msg"] as? String {
                return message
            }
        }
        if let text = String(data: data, encoding: .utf8), !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return text
        }
        return fallback
    }

    private func safeHeaderFilename(_ filename: String) -> String {
        let cleaned = filename
            .replacingOccurrences(of: "\r", with: "")
            .replacingOccurrences(of: "\n", with: "")
        return cleaned.isEmpty ? "upload.ipa" : cleaned
    }
}

extension APIClient {
    func storeSources() async throws -> StoreSourceList {
        try await get("/api/store/sources", as: StoreSourceList.self)
    }

    func addStoreSource(url: String) async throws -> StatusResponse {
        try await postJSON("/api/store/sources", body: ["url": url], as: StatusResponse.self)
    }

    func removeStoreSource(id: String) async throws -> StoreSourceList {
        try await postJSON("/api/store/sources/remove", body: ["id": id], as: StoreSourceList.self)
    }

    func storeApps(deviceUDID: String?) async throws -> StoreCatalog {
        let query = (deviceUDID?.isEmpty == false)
            ? "?device_udid=\(deviceUDID!.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? "")"
            : ""
        return try await get("/api/store/apps\(query)", as: StoreCatalog.self, timeout: 90)
    }

    func storeInstall(
        appKey: String,
        deviceUDID: String,
        onMessage: @MainActor @escaping (InstallMessage) -> Void
    ) async throws {
        try await streamJob(
            path: "/ws/store-install",
            payload: ["app_key": appKey, "device_udid": deviceUDID],
            onMessage: onMessage
        )
    }
}
