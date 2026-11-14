import Foundation

/// Limits returned by the relay (spec 6.3).
public struct Limits: Codable, Equatable {
    public var max_batch_bytes: Int
    public var device_daily_bytes: Int
    public var user_total_bytes: Int
    public var seq_window: Int
    public static let `default` = Limits(max_batch_bytes: 8 << 20, device_daily_bytes: 512 << 20,
                                         user_total_bytes: 20 << 30, seq_window: 64)
}

public struct StoredGrant: Codable, Equatable {
    public var grantSeq: Int
    public var signed: Signed
    public var processors: [Processor]
    public var acked: Bool
}

/// Why uploading stopped. `.paused` is retried daily; `.halted` needs the member (spec 4.3 device rules).
public enum Stop: Codable, Equatable {
    case paused(reason: String, since: Date)
    case halted(reason: String, since: Date)
}

/// Everything the app persists, apart from the Secure Enclave key and the ciphertext files.
public struct DeviceState: Codable, Equatable {
    public var server: String
    public var userID: String
    public var userName: String
    public var deviceID: String
    public var deviceName: String
    /// The device public key (b64u), kept so the fingerprint can be shown without touching the keychain.
    public var devicePub: String?
    public var enrolledAt: Date
    public var limits: Limits = .default

    public var grants: [StoredGrant] = []
    /// Last sealed batch: the chain head belongs to the device (spec 4.3).
    public var lastSealedSeq: Int = 0
    public var lastSealedManifestSHA: String?
    /// Acknowledged seqs above the contiguous prefix.
    public var ackedAbove: [Int] = []
    public var ackedContiguous: Int = 0
    public var highestAcked: Int { max(ackedContiguous, ackedAbove.max() ?? 0) }

    /// HealthKit anchors (archived HKQueryAnchor) per type identifier, committed only once every
    /// batch that read past them is acknowledged.
    public var committedAnchors: [String: Data] = [:]
    public var stop: Stop?
    public var rollbackSuspectedAt: Date?
    public var lastResendAt: Date?
    public var lastCensusAt: Date?
    public var lastProfileAt: Date?
    public var lastSyncAt: Date?
    public var lastUploadAt: Date?
    public var lastError: String?
    // Fields added after the first release are optional so older state.json files still decode.
    /// Per-type read failures from the last sync (type identifier -> message).
    public var typeErrors: [String: String]?
    /// highestAcked when a rollback was first suspected (spec 4.3).
    public var rollbackAckedAtSuspicion: Int?
    /// Set by "re-send history": the next pass reads every type from the beginning.
    public var resendRequested: Bool?
    public var lastProfileSHA: String?
    /// The last sealed seq when a re-send was requested, and the anchors the re-send has committed.
    public var resendFromSeq: Int?
    /// Incremented on every re-send request; a pass clears the request only if no new one arrived.
    public var resendGeneration: Int?
    public var resendCommittedAnchors: [String: Data]?

    public var deviceFingerprint: String? { devicePub.flatMap(Encoding.b64uDecode).map(Fingerprint.of) }

    public var currentGrant: StoredGrant? { grants.max(by: { $0.grantSeq < $1.grantSeq }) }

    public init(server: String, userID: String, userName: String, deviceID: String, deviceName: String, enrolledAt: Date) {
        self.server = server; self.userID = userID; self.userName = userName
        self.deviceID = deviceID; self.deviceName = deviceName; self.enrolledAt = enrolledAt
    }
}

/// A sealed batch waiting for its 200. Ciphertext lives next to it as `<seq>.age`.
public struct OutboxEntry: Codable, Equatable {
    public var seq: Int
    public var manifest: Signed
    public var grantSeq: Int
    public var size: Int
    /// Anchors reached after this batch, per type: committed once this batch and all before it are acked.
    public var anchors: [String: Data]
    public var reason: String
    public var createdAt: Date
    public var attempts: Int = 0
    public var notBefore: Date?
    public var lastError: String?
}

/// Files under one directory, written atomically, protected until first unlock so background
/// uploads can read them while the phone is locked.
public final class DeviceStore: @unchecked Sendable {  // immutable root; every write is an atomic file replace
    public let root: URL
    let fm = FileManager.default

    public init(root: URL) throws {
        self.root = root
        try fm.createDirectory(at: outboxDir, withIntermediateDirectories: true)
    }

    var stateURL: URL { root.appendingPathComponent("state.json") }
    public var outboxDir: URL { root.appendingPathComponent("outbox", isDirectory: true) }

    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        e.outputFormatting = [.sortedKeys, .prettyPrinted]
        return e
    }()
    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }()

    func write(_ data: Data, to url: URL) throws {
        #if os(iOS)
        try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
        #else
        try data.write(to: url, options: [.atomic])
        #endif
    }

    /// nil when the device is not enrolled; throws when the file exists but cannot be read
    /// (for example before the first unlock), so the caller does not mistake it for "not enrolled".
    public func loadState() throws -> DeviceState? {
        guard fm.fileExists(atPath: stateURL.path) else { return nil }
        return try Self.decoder.decode(DeviceState.self, from: Data(contentsOf: stateURL))
    }

    public func saveState(_ state: DeviceState) throws {
        try write(Self.encoder.encode(state), to: stateURL)
    }

    /// Forget everything (new device after a restore or a recovery re-enrolment).
    public func wipe() throws {
        if fm.fileExists(atPath: root.path) { try fm.removeItem(at: root) }
        try fm.createDirectory(at: outboxDir, withIntermediateDirectories: true)
    }

    // MARK: outbox

    public func ciphertextURL(_ seq: Int) -> URL { outboxDir.appendingPathComponent(String(format: "%010d.age", seq)) }
    func entryURL(_ seq: Int) -> URL { outboxDir.appendingPathComponent(String(format: "%010d.json", seq)) }

    /// Ciphertext first, entry second: an entry always has its file.
    public func addToOutbox(_ entry: OutboxEntry, ciphertext: Data) throws {
        try write(ciphertext, to: ciphertextURL(entry.seq))
        try write(Self.encoder.encode(entry), to: entryURL(entry.seq))
    }

    public func updateEntry(_ entry: OutboxEntry) throws {
        try write(Self.encoder.encode(entry), to: entryURL(entry.seq))
    }

    public func outbox() throws -> [OutboxEntry] {
        let files = try fm.contentsOfDirectory(at: outboxDir, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
        return try files.map { try Self.decoder.decode(OutboxEntry.self, from: Data(contentsOf: $0)) }
            .sorted { $0.seq < $1.seq }
    }

    public func removeFromOutbox(_ seq: Int) throws {
        try? fm.removeItem(at: entryURL(seq))
        try? fm.removeItem(at: ciphertextURL(seq))
    }
}
