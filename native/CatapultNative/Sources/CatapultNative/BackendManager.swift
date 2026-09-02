import Foundation
import Combine
import Darwin

private let catapultBackgroundAgentLabel = "com.catapult.server"

enum BackendStatus: Equatable, Sendable {
    case stopped
    case starting
    case ready
    case failed(String)

    var label: String {
        switch self {
        case .stopped: "Stopped"
        case .starting: "Starting"
        case .ready: "Ready"
        case .failed: "Failed"
        }
    }
}

@MainActor
final class BackendManager: ObservableObject {
    @Published private(set) var status: BackendStatus = .stopped
    @Published private(set) var backendRoot: URL?
    @Published private(set) var logLines: [String] = []
    @Published private(set) var startupDetail = "Start it to scan devices and install apps."

    private var process: Process?
    private let baseURL = URL(string: "http://127.0.0.1:9450")!
    private let requiredProtocol = 10

    deinit {
        process?.terminate()
    }

    func start() async {
        if case .starting = status {
            return
        }
        if case .ready = status {
            return
        }

        logLines.removeAll()
        startupDetail = "Checking local engine..."
        status = .starting

        do {
            let root = try locateBackendRoot()
            let uv = try locateUV()
            let bundledBackend = usesBundledBackend(root)
            let supportRoot = bundledBackend ? try appSupportRoot() : nil
            backendRoot = root

            if bundledBackend, let supportRoot {
                startupDetail = "Preparing background engine..."
                if !isBackendPythonReady(supportRoot: supportRoot) {
                    try await prepareBundledBackendEnvironment(root: root, uv: uv, supportRoot: supportRoot)
                }

                do {
                    let updatedAgent = try installOrUpdateBackgroundAgent(root: root, supportRoot: supportRoot)
                    appendLog(updatedAgent ? "Installed Catapult background refresh agent" : "Catapult background refresh agent is installed")

                    if await isTrustedBackendHealthy() {
                        startupDetail = "Engine ready."
                        status = .ready
                        return
                    }

                    if terminateStaleBackendOnPort() {
                        appendLog("Stopping stale Catapult backend on port 9450")
                        try? await Task.sleep(nanoseconds: 600_000_000)
                    }

                    startupDetail = "Starting background engine..."
                    kickstartBackgroundAgent()
                    for _ in 0..<360 {
                        if await isTrustedBackendHealthy() {
                            startupDetail = "Engine ready."
                            status = .ready
                            return
                        }
                        try? await Task.sleep(nanoseconds: 500_000_000)
                    }

                    status = .failed(backgroundAgentFailureMessage("The background backend did not become ready on port 9450."))
                    return
                } catch {
                    appendLog("Could not start background refresh agent: \(error.localizedDescription)")
                    startupDetail = "Starting local engine..."
                }
            }

            if await isTrustedBackendHealthy() {
                startupDetail = "Engine ready."
                status = .ready
                return
            }

            startupDetail = "Starting backend process..."

            if terminateStaleBackendOnPort() {
                appendLog("Stopping stale Catapult backend on port 9450")
                try? await Task.sleep(nanoseconds: 600_000_000)
            }

            let process = Process()
            process.executableURL = uv.executable
            process.arguments = uv.prefixArguments + [
                "run",
                "python",
                "run.py",
                "--serve",
                "--port",
                "9450"
            ]
            process.currentDirectoryURL = root

            var environment = ProcessInfo.processInfo.environment
            environment["PYTHONUNBUFFERED"] = "1"
            if let helper = iconHelperURL() {
                environment["CATAPULT_ICON_HELPER"] = helper.path
            }
            if bundledBackend, let supportRoot {
                environment["UV_PROJECT_ENVIRONMENT"] = supportRoot.appending(path: "BackendEnv").path
                environment["UV_CACHE_DIR"] = supportRoot.appending(path: "uv-cache").path
            }
            process.environment = environment

            let pipe = Pipe()
            process.standardOutput = pipe
            process.standardError = pipe
            pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
                let data = handle.availableData
                guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else {
                    return
                }
                Task { @MainActor [weak self] in
                    self?.appendLog(text)
                }
            }

            try process.run()
            self.process = process

            startupDetail = "Waiting for backend on port 9450..."
            let maxAttempts = bundledBackend ? 360 : 120
            for _ in 0..<maxAttempts {
                if !process.isRunning {
                    status = .failed(backendFailureMessage("The backend exited before it became ready."))
                    return
                }
                if await isTrustedBackendHealthy() {
                    if !bundledBackend {
                        installBackgroundAgentIfPossible(root: root, supportRoot: supportRoot, bundledBackend: bundledBackend)
                    }
                    startupDetail = "Engine ready."
                    status = .ready
                    return
                }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }

            status = .failed(backendFailureMessage("The backend did not become ready on port 9450."))
        } catch {
            status = .failed(error.localizedDescription)
        }
    }

    private func prepareBundledBackendEnvironment(root: URL, uv: UVCommand, supportRoot: URL) async throws {
        startupDetail = "Creating backend environment..."
        appendLog("Creating Catapult backend environment")

        let process = Process()
        process.executableURL = uv.executable
        process.arguments = uv.prefixArguments + [
            "sync",
            "--frozen",
            "--no-dev",
        ]
        process.currentDirectoryURL = root

        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        environment["UV_PROJECT_ENVIRONMENT"] = supportRoot.appending(path: "BackendEnv").path
        environment["UV_CACHE_DIR"] = supportRoot.appending(path: "uv-cache").path
        process.environment = environment

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else {
                return
            }
            Task { @MainActor [weak self] in
                self?.appendLog(text)
            }
        }

        try process.run()
        while process.isRunning {
            try? await Task.sleep(nanoseconds: 250_000_000)
        }
        pipe.fileHandleForReading.readabilityHandler = nil

        guard process.terminationStatus == 0, isBackendPythonReady(supportRoot: supportRoot) else {
            throw CatapultError.backendUnavailable("Could not prepare the backend Python environment.")
        }
    }

    func stop() {
        process?.terminate()
        process = nil
        startupDetail = "Start it to scan devices and install apps."
        status = .stopped
    }

    private func appendLog(_ text: String) {
        let newLines = text
            .split(whereSeparator: \.isNewline)
            .map(String.init)
            .filter { !$0.isEmpty }
        guard !newLines.isEmpty else { return }
        logLines.append(contentsOf: newLines)
        if logLines.count > 200 {
            logLines.removeFirst(logLines.count - 200)
        }
        if case .starting = status, let lastLine = newLines.last {
            startupDetail = userFacingStartupLine(lastLine)
        }
    }

    private func userFacingStartupLine(_ line: String) -> String {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.contains("Creating virtual environment") {
            return "Creating backend environment..."
        }
        if trimmed.contains("Installed ") && trimmed.contains(" packages") {
            return trimmed
        }
        if trimmed.contains("Starting Catapult server") {
            return "Backend started; waiting for health check..."
        }
        if trimmed.contains("Downloading ") || trimmed.contains("Building ") || trimmed.contains("Built ") || trimmed.contains("Downloaded ") {
            return trimmed
        }
        if let range = trimmed.range(of: "INFO: ") {
            return String(trimmed[range.upperBound...])
        }
        return trimmed.count > 140 ? String(trimmed.prefix(137)) + "..." : trimmed
    }

    private func backendFailureMessage(_ prefix: String) -> String {
        let tail = logLines.suffix(4).joined(separator: "\n")
        return tail.isEmpty ? prefix : "\(prefix)\n\n\(tail)"
    }

    private func backgroundAgentFailureMessage(_ prefix: String) -> String {
        let tail = backgroundAgentLogTail()
        return tail.isEmpty ? prefix : "\(prefix)\n\n\(tail)"
    }

    private func installBackgroundAgentIfPossible(root: URL, supportRoot: URL?, bundledBackend: Bool) {
        guard bundledBackend, let supportRoot, isBackendPythonReady(supportRoot: supportRoot) else {
            return
        }
        do {
            _ = try installOrUpdateBackgroundAgent(root: root, supportRoot: supportRoot)
        } catch {
            appendLog("Could not install background refresh agent: \(error.localizedDescription)")
        }
    }

    private nonisolated func isTrustedBackendHealthy() async -> Bool {
        var request = URLRequest(url: baseURL.appending(path: "/api/health"))
        request.timeoutInterval = 2
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                return false
            }
            let health = try JSONDecoder().decode(BackendHealth.self, from: data)
            return health.app == "catapult" && health.protocolVersion == requiredProtocol
        } catch {
            return false
        }
    }

    private nonisolated func terminateStaleBackendOnPort() -> Bool {
        let pids = commandOutput("/usr/sbin/lsof", ["-ti", "tcp:9450"])
            .split(whereSeparator: \.isNewline)
            .compactMap { Int32($0.trimmingCharacters(in: .whitespacesAndNewlines)) }

        var terminated = false
        for pid in pids {
            let command = commandOutput("/bin/ps", ["-p", "\(pid)", "-o", "command="])
            let isCatapultBackend = command.contains("run.py")
                && command.contains("--serve")
                && command.contains("--port")
                && command.contains("9450")
            if isCatapultBackend {
                kill(pid, SIGTERM)
                terminated = true
            }
        }
        return terminated
    }

    private nonisolated func commandOutput(_ executable: String, _ arguments: [String]) -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            return String(data: data, encoding: .utf8) ?? ""
        } catch {
            return ""
        }
    }

    @discardableResult
    private nonisolated func runCommand(_ executable: String, _ arguments: [String]) -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.standardOutput = Pipe()
        process.standardError = Pipe()
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus
        } catch {
            return -1
        }
    }

    private nonisolated func installOrUpdateBackgroundAgent(root: URL, supportRoot: URL) throws -> Bool {
        let fileManager = FileManager.default
        let plistDirectory = fileManager.homeDirectoryForCurrentUser
            .appending(path: "Library")
            .appending(path: "LaunchAgents")
        try fileManager.createDirectory(at: plistDirectory, withIntermediateDirectories: true)

        let logDirectory = fileManager.homeDirectoryForCurrentUser.appending(path: ".catapult")
        try fileManager.createDirectory(at: logDirectory, withIntermediateDirectories: true)

        let plistURL = plistDirectory.appending(path: "\(catapultBackgroundAgentLabel).plist")
        let python = backendPythonURL(supportRoot: supportRoot)
        guard fileManager.isExecutableFile(atPath: python.path) else {
            throw CatapultError.backendUnavailable("Backend Python environment is not ready.")
        }
        let programArguments = [
            python.path,
            "run.py",
            "--serve",
            "--port",
            "9450"
        ]
        var environmentVariables = ["PYTHONUNBUFFERED": "1"]
        if let helper = iconHelperURL() {
            environmentVariables["CATAPULT_ICON_HELPER"] = helper.path
        }
        let plist: [String: Any] = [
            "Label": catapultBackgroundAgentLabel,
            "ProgramArguments": programArguments,
            "WorkingDirectory": root.path,
            "EnvironmentVariables": environmentVariables,
            "RunAtLoad": true,
            "KeepAlive": true,
            "StandardOutPath": logDirectory.appending(path: "agent.log").path,
            "StandardErrorPath": logDirectory.appending(path: "agent.log").path,
        ]
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        let existing = try? Data(contentsOf: plistURL)
        let changed = existing != data
        if changed {
            try data.write(to: plistURL, options: .atomic)
            bootoutBackgroundAgent(plistURL: plistURL)
        }

        let domain = "gui/\(getuid())"
        _ = runCommand("/bin/launchctl", ["bootstrap", domain, plistURL.path])
        _ = runCommand("/bin/launchctl", ["enable", "\(domain)/\(catapultBackgroundAgentLabel)"])
        return changed
    }

    /// The icon extraction helper ships next to the app binary, both inside the
    /// .app bundle and in a `swift build` products directory.
    private nonisolated func iconHelperURL() -> URL? {
        guard let executableURL = Bundle.main.executableURL else {
            return nil
        }
        let helper = executableURL.deletingLastPathComponent().appending(path: "catapult-icon")
        return FileManager.default.isExecutableFile(atPath: helper.path) ? helper : nil
    }

    private nonisolated func backendPythonURL(supportRoot: URL) -> URL {
        supportRoot
            .appending(path: "BackendEnv")
            .appending(path: "bin")
            .appending(path: "python")
    }

    private nonisolated func isBackendPythonReady(supportRoot: URL) -> Bool {
        FileManager.default.isExecutableFile(atPath: backendPythonURL(supportRoot: supportRoot).path)
    }

    private nonisolated func kickstartBackgroundAgent() {
        let domain = "gui/\(getuid())"
        _ = runCommand("/bin/launchctl", ["kickstart", "-k", "\(domain)/\(catapultBackgroundAgentLabel)"])
    }

    private nonisolated func bootoutBackgroundAgent(plistURL: URL? = nil) {
        let domain = "gui/\(getuid())"
        if let plistURL {
            _ = runCommand("/bin/launchctl", ["bootout", domain, plistURL.path])
        } else {
            _ = runCommand("/bin/launchctl", ["bootout", "\(domain)/\(catapultBackgroundAgentLabel)"])
        }
    }

    private nonisolated func backgroundAgentLogTail() -> String {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appending(path: ".catapult")
            .appending(path: "agent.log")
            .path
        return commandOutput("/usr/bin/tail", ["-n", "8", path])
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private nonisolated func locateBackendRoot() throws -> URL {
        for candidate in backendRootCandidates() where containsBackend(at: candidate) {
            return candidate
        }
        throw CatapultError.missingBackendRoot
    }

    private nonisolated func backendRootCandidates() -> [URL] {
        var candidates: [URL] = []
        let fileManager = FileManager.default
        let env = ProcessInfo.processInfo.environment

        if let path = env["CATAPULT_BACKEND_ROOT"], !path.isEmpty {
            candidates.append(URL(fileURLWithPath: path))
        }

        if let resourceURL = Bundle.main.resourceURL {
            candidates.append(resourceURL.appending(path: "backend"))
        }

        candidates.append(URL(fileURLWithPath: fileManager.currentDirectoryPath))

        if let executableURL = Bundle.main.executableURL {
            candidates.append(executableURL.deletingLastPathComponent())
        }

        var expanded: [URL] = []
        for candidate in candidates {
            var current = candidate.standardizedFileURL
            for _ in 0..<8 {
                expanded.append(current)
                let parent = current.deletingLastPathComponent()
                if parent.path == current.path {
                    break
                }
                current = parent
            }
        }

        var seen = Set<String>()
        return expanded.filter { seen.insert($0.path).inserted }
    }

    private nonisolated func containsBackend(at url: URL) -> Bool {
        let fileManager = FileManager.default
        return fileManager.fileExists(atPath: url.appending(path: "run.py").path)
            && fileManager.fileExists(atPath: url.appending(path: "pyproject.toml").path)
            && fileManager.fileExists(atPath: url.appending(path: "catapult/server.py").path)
    }

    private nonisolated func usesBundledBackend(_ root: URL) -> Bool {
        root.standardizedFileURL.path.contains(".app/Contents/Resources/backend")
    }

    private nonisolated func appSupportRoot() throws -> URL {
        let fileManager = FileManager.default
        guard let base = fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first else {
            throw CatapultError.backendUnavailable("Could not locate Application Support.")
        }
        let root = base.appending(path: "Catapult")
        try fileManager.createDirectory(at: root, withIntermediateDirectories: true)
        return root
    }

    private nonisolated func locateUV() throws -> UVCommand {
        let env = ProcessInfo.processInfo.environment
        let fileManager = FileManager.default

        if let resourceURL = Bundle.main.resourceURL {
            let bundledUV = resourceURL.appending(path: "uv").path
            if fileManager.isExecutableFile(atPath: bundledUV) {
                return UVCommand(executable: URL(fileURLWithPath: bundledUV), prefixArguments: [])
            }
        }

        if let path = env["CATAPULT_UV"], fileManager.isExecutableFile(atPath: path) {
            return UVCommand(executable: URL(fileURLWithPath: path), prefixArguments: [])
        }

        let homeDirectory = fileManager.homeDirectoryForCurrentUser.path
        let candidates = [
            "\(homeDirectory)/.local/bin/uv",
            "\(homeDirectory)/.cargo/bin/uv",
            "/opt/homebrew/bin/uv",
            "/usr/local/bin/uv",
            "/usr/bin/uv",
        ]

        for path in candidates {
            if fileManager.isExecutableFile(atPath: path) {
                return UVCommand(executable: URL(fileURLWithPath: path), prefixArguments: [])
            }
        }

        throw CatapultError.missingUV
    }
}

private struct UVCommand: Sendable {
    let executable: URL
    let prefixArguments: [String]
}

private struct BackendHealth: Decodable, Sendable {
    let app: String
    let protocolVersion: Int

    enum CodingKeys: String, CodingKey {
        case app
        case protocolVersion = "protocol"
    }
}
