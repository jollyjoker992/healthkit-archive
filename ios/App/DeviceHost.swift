import Foundation
import HKACore
import HealthKit
import UIKit

/// Owns the device key, the store and the `Device` actor. The device never leaves this actor:
/// callers go through its methods, so there is one `Device` instance per process.
actor DeviceHost {
    static let shared = DeviceHost()

    private var device: Device?
    private var started = false
    private var starting: Task<Void, Error>?
    let store: DeviceStore
    let health = HKHealthStore()
    let root: URL

    private init() {
        root = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("hka", isDirectory: true)
        store = try! DeviceStore(root: root)
    }

    var appVersion: String {
        let info = Bundle.main.infoDictionary ?? [:]
        return "\(info["CFBundleShortVersionString"] ?? "?") (\(info["CFBundleVersion"] ?? "?"))"
    }

    nonisolated var osVersion: String {
        let v = ProcessInfo.processInfo.operatingSystemVersion
        return "iOS \(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
    }

    /// Marker of a completed first launch. Not protected, so it is readable before the first unlock
    /// (UserDefaults can read as empty then, which would look like a fresh install).
    var installMarker: URL {
        FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("hka-installed")
    }

    /// Spec 4.3: on first launch after install delete any key left in the keychain; on any launch, a
    /// missing or mismatched key means this is a new device and all state is discarded.
    /// Single-flight: concurrent callers share one start. It also works while the phone is locked
    /// after the first unlock (key and files are AfterFirstUnlock), so background relaunches can
    /// record uploads; before the first unlock it leaves the host unstarted and is retried later.
    func start(protectedDataAvailable: Bool) async throws {
        if started { return }
        if let starting { return try await starting.value }
        let task = Task { try await self.doStart(protectedDataAvailable: protectedDataAvailable) }
        starting = task
        defer { starting = nil }
        try await task.value
    }

    private func doStart(protectedDataAvailable: Bool) async throws {
        guard !started else { return }
        let marker = FileManager.default.fileExists(atPath: installMarker.path)
        if !marker && !protectedDataAvailable { return }  // cannot tell a fresh install from a locked phone
        if !marker {
            try Keys.delete()
            try store.wipe()
            FileManager.default.createFile(atPath: installMarker.path, contents: Data(),
                                           attributes: [.protectionKey: FileProtectionType.none])
        }
        let key: DeviceSigner?
        let saved: DeviceState?
        do {
            key = try Keys.load()
            saved = try store.loadState()
        } catch {
            return  // before the first unlock: try again on the next start; never regenerate or wipe
        }
        if let state = saved {
            guard let key, key.deviceID == state.deviceID else {
                try store.wipe()
                started = true
                return
            }
            let d = Device(store: store, state: state, signer: key, appVersion: appVersion, osVersion: osVersion)
            try await d.retryPauseOnLaunch()
            device = d
        }
        started = true
    }

    func enroll(invite: Invite, deviceName: String) async throws {
        let key = try Keys.load() ?? Keys.create()
        let state = try await Device.enroll(invite: invite, deviceName: deviceName, signer: key, store: store)
        device = Device(store: store, state: state, signer: key, appVersion: appVersion, osVersion: osVersion)
        started = true
    }

    /// After a halt: forget this device (key, state, outbox) so the member can enrol again. The admin
    /// retires the old device on the relay; its data there stays readable to processors.
    func startOver() async throws {
        device = nil
        await BackgroundUploader.shared.cancelAll()  // results of the old device must not reach the new one
        try Keys.delete()
        try store.wipe()
    }

    var deviceID: String? { get async { await device?.state.deviceID } }

    func setProcessors(_ processors: [Processor]) async throws {
        guard let device else { throw SyncError.notEnrolled }
        try await device.setProcessors(processors)
    }

    func source(readable: @escaping @Sendable () -> Bool) -> HealthKitSource {
        HealthKitSource(store: health, readable: readable)
    }

    /// Read, seal and upload in the foreground.
    @discardableResult
    func sync(readable: Bool) async throws -> Int {
        guard let device else { throw SyncError.notEnrolled }
        try? await device.checkRelay()
        let sealed = try await device.sync(source: source(readable: { readable }))
        try? await device.checkRelay()  // so the status shows what the relay now holds
        return sealed
    }

    /// Read and seal only (background wake); uploads go through the background session.
    func sealOnly(readable: Bool) async throws {
        guard let device, readable else { return }
        _ = try await device.sync(source: source(readable: { readable }), uploadInForeground: false)
    }

    func checkRelay() async { try? await device?.checkRelay() }

    func pendingUploads() async -> [(seq: Int, request: URLRequest, file: URL)] {
        (try? await device?.pendingUploads()) ?? []
    }

    func state() async -> DeviceState? { await device?.state }
    func outbox() -> [OutboxEntry] { (try? store.outbox()) ?? [] }

    func acknowledge(_ seq: Int, deviceID: String) async throws {
        guard let device, await device.state.deviceID == deviceID else { return }
        try await device.acknowledge(seq)
    }
    func uploadFailed(_ seq: Int, deviceID: String, error: RelayError) async throws {
        guard let device, await device.state.deviceID == deviceID else { return }
        try await device.handleUploadError(error, seq: seq, now: Date())
    }

    func resendHistory() async throws { try await device?.resendHistory() }
}

/// The device key: Secure Enclave on a phone, a keychain-stored software key in the simulator.
enum Keys {
    static func create() throws -> DeviceSigner {
        #if targetEnvironment(simulator)
        return try SimulatorSigner.create()
        #else
        return try SecureEnclaveSigner.create()
        #endif
    }

    static func load() throws -> DeviceSigner? {
        #if targetEnvironment(simulator)
        return try SimulatorSigner.load()
        #else
        return try SecureEnclaveSigner.load()
        #endif
    }

    static func delete() throws {
        #if targetEnvironment(simulator)
        try SimulatorSigner.delete()
        #else
        try SecureEnclaveSigner.delete()
        #endif
    }
}

#if targetEnvironment(simulator)
import CryptoKit

enum SimulatorSigner {
    static let service = "hka.simulator-key"
    static func create() throws -> DeviceSigner {
        try delete()
        let s = SoftwareSigner()
        let status = SecItemAdd([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                 kSecValueData as String: s.key.rawRepresentation] as CFDictionary, nil)
        guard status == errSecSuccess else { throw KeychainError.status(status) }
        return s
    }
    static func load() throws -> DeviceSigner? {
        var item: CFTypeRef?
        guard SecItemCopyMatching([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
                                   kSecReturnData as String: true] as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return nil }
        return SoftwareSigner(key: try P256.Signing.PrivateKey(rawRepresentation: data))
    }
    static func delete() throws {
        SecItemDelete([kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service] as CFDictionary)
    }
}
#endif
