import Foundation

/// One page of anchored changes for a type (records already in spec/records.md form).
public struct HealthPage {
    public var records: [[String: Any]]
    public var anchor: Data?
    public var more: Bool
    public init(records: [[String: Any]], anchor: Data?, more: Bool) {
        self.records = records; self.anchor = anchor; self.more = more
    }
}

/// What the sync engine needs from HealthKit. The app implements it with HKHealthStore; tests fake it.
public protocol HealthSource {
    /// Type identifiers to sync, in a stable order.
    func syncTypes() -> [String]
    /// New and deleted objects after `anchor`, at most about `limit` records.
    func changes(type: String, anchor: Data?, limit: Int) async throws -> HealthPage
    /// Every object of `type` whose start is at or after `since` (daily re-send, spec records.md `resend`).
    func recent(type: String, since: Date) async throws -> [[String: Any]]
    /// Census records for the last `days` local days (spec records.md `census`).
    func census(days: Int, now: Date) async throws -> [[String: Any]]
    /// Characteristics as a `profile` record, or nil.
    func profile(now: Date) -> [String: Any]?
    /// False while the phone is locked: HealthKit cannot be read then.
    var isReadable: Bool { get }
}

public enum SyncError: Error, Equatable {
    case stopped(String)
    case notEnrolled
    case noProcessors
}

/// Seals batches from HealthKit changes and uploads them, following the device rules of spec 4.2
/// and 4.3: grants and manifests are signed once, the chain is local, anchors advance only after
/// acknowledgement. An actor: every read and write of the state and outbox is serialized, and
/// concurrent sync requests (many HealthKit wakes at once) are coalesced into one pass.
public actor Device {
    public nonisolated let store: DeviceStore
    public internal(set) var state: DeviceState
    let signer: DeviceSigner
    let client: RelayClient
    let tz: @Sendable () -> String
    let appVersion: String
    let osVersion: String

    /// Uncompressed bytes per batch before sealing; the sealed size is checked against the limit.
    var softBatchBytes = 24 << 20
    /// HealthKit objects per anchored page. Types whose objects carry series are read in small pages.
    var pageLimit = 2000
    static let seriesPageLimit = 25
    /// Acknowledged uploads kept for the member's upload list.
    public static let recentUploadLimit = 20
    static let seriesTypes: Set<String> = ["HKWorkoutTypeIdentifier", "HKDataTypeIdentifierElectrocardiogram",
                                           "HKDataTypeIdentifierHeartbeatSeries"]

    private var syncing = false
    private var rerun = false
    private var rerunUploadsInForeground = false
    private var waiters: [CheckedContinuation<Result<Int, Error>, Never>] = []

    public init(store: DeviceStore, state: DeviceState, signer: DeviceSigner, session: URLSession = .shared,
                tz: @escaping @Sendable () -> String = { TimeZone.current.identifier }, appVersion: String = "dev",
                osVersion: String = ProcessInfo.processInfo.operatingSystemVersionString) {
        // Recover the chain head if the app stopped between writing a sealed batch and saving state:
        // a seq already in the outbox must never be signed again.
        var state = state
        if let newest = (try? store.outbox())?.last, newest.seq > state.lastSealedSeq {
            state.lastSealedSeq = newest.seq
            state.lastSealedManifestSHA = newest.manifest.sha256
            try? store.saveState(state)
        }
        self.store = store; self.state = state; self.signer = signer
        self.client = RelayClient(origin: state.server, signer: signer, session: session)
        self.tz = tz; self.appVersion = appVersion; self.osVersion = osVersion
    }

    public func configure(softBatchBytes: Int? = nil, pageLimit: Int? = nil) {
        if let softBatchBytes { self.softBatchBytes = softBatchBytes }
        if let pageLimit { self.pageLimit = pageLimit }
    }

    /// For tests and recovery tools: change the persisted state directly.
    func mutate(_ change: (inout DeviceState) -> Void) throws {
        change(&state)
        try save()
    }

    func save() throws { try store.saveState(state) }

    // MARK: enrolment

    /// Enrol with a scanned invite. Checks the relay's answer against the invite and the key (spec 4.1).
    public static func enroll(invite: Invite, deviceName: String, signer: DeviceSigner, store: DeviceStore,
                              session: URLSession = .shared, now: Date = Date()) async throws -> DeviceState {
        let client = RelayClient(origin: invite.server, signer: signer, session: session)
        let body = try Documents.enrollBody(invite: invite, signer: signer, deviceName: deviceName, now: now)
        var enrolled: RelayClient.Enrolled
        do {
            enrolled = try await client.enroll(body)
        } catch let e as RelayError where e.code == "stale_timestamp" {
            guard let serverTime = e.extra["server_time"] else { throw e }
            let fixed = Date(timeIntervalSince1970: TimeInterval(serverTime))
            enrolled = try await client.enroll(try Documents.enrollBody(invite: invite, signer: signer,
                                                                         deviceName: deviceName, now: fixed))
        }
        guard enrolled.userID == invite.userID, enrolled.deviceID == signer.deviceID else {
            throw RelayError(status: 200, code: "bad_response", message: "the relay answered for another user or key", extra: [:])
        }
        var state = DeviceState(server: invite.server, userID: invite.userID, userName: invite.userName,
                                deviceID: signer.deviceID, deviceName: deviceName, enrolledAt: now)
        state.devicePub = signer.publicKeyB64u
        state.limits = enrolled.limits
        try store.saveState(state)
        return state
    }

    // MARK: processors and grants (spec 4.2)

    /// Sign a new grant for `processors`, save it, then upload pending grants in order.
    public func setProcessors(_ processors: [Processor], now: Date = Date()) async throws {
        let next = (state.grants.map(\.grantSeq).max() ?? 0) + 1
        let signed = try Documents.grant(userID: state.userID, signer: signer, grantSeq: next, processors: processors, now: now)
        state.grants.append(StoredGrant(grantSeq: next, signed: signed, processors: processors, acked: false))
        try save()  // durable before the first upload: a grant is never re-signed
        try await uploadPendingGrants()
    }

    func uploadPendingGrants() async throws {
        while let g = state.grants.filter({ !$0.acked }).min(by: { $0.grantSeq < $1.grantSeq }) {
            do {
                try await client.putGrant(g.signed)
            } catch let e as RelayError {
                try record(e, what: "grant \(g.grantSeq)")
                throw e
            }
            if let i = state.grants.firstIndex(where: { $0.grantSeq == g.grantSeq }) { state.grants[i].acked = true }
            try save()
        }
    }

    // MARK: sealing

    func seal(_ records: [[String: Any]], reason: String, anchors: [String: Data], now: Date) throws -> [OutboxEntry] {
        guard let grant = state.currentGrant else { throw SyncError.noProcessors }
        let records = records.map(Encoding.sanitize)
        var counts: [String: Int] = [:]
        for r in records where ["sample", "workout", "ecg", "heartbeats"].contains(r["k"] as? String ?? "") {
            if let t = r["type"] as? String, (r["part"] as? Int ?? 0) == 0 { counts[t, default: 0] += 1 }
        }
        let header: [String: Any] = [
            "k": "header", "v": 1, "user_id": state.userID, "device_id": state.deviceID,
            "created_at": Encoding.instant(now), "tz": tz(), "app_version": appVersion, "os_version": osVersion,
            "reason": reason, "counts": counts,
        ]
        var plain = Data()
        for r in [header] + records { plain += try Encoding.json(r) + Data([0x0A]) }
        let ciphertext = try AgeWriter.encrypt(try Gzip.compress(plain), to: grant.processors.map(\.ageRecipient))
        if ciphertext.count > state.limits.max_batch_bytes {
            guard records.count > 1 else { throw SyncError.stopped("a single record exceeds the batch limit") }
            let half = records.count / 2
            return try seal(Array(records[..<half]), reason: reason, anchors: [:], now: now)
                + seal(Array(records[half...]), reason: reason, anchors: anchors, now: now)
        }
        let seq = state.lastSealedSeq + 1
        let manifest = try Documents.manifest(userID: state.userID, signer: signer, seq: seq, prev: state.lastSealedManifestSHA,
                                              ciphertext: ciphertext, grantSeq: grant.grantSeq, grant: grant.signed,
                                              processors: grant.processors, now: now)
        let entry = OutboxEntry(seq: seq, manifest: manifest, grantSeq: grant.grantSeq, size: ciphertext.count,
                                anchors: anchors, reason: reason, createdAt: now)
        // Entry on disk first; `init` recovers the chain head from it if state is not saved below.
        try store.addToOutbox(entry, ciphertext: ciphertext)
        state.lastSealedSeq = seq
        state.lastSealedManifestSHA = manifest.sha256
        try save()
        return [entry]
    }

    /// Anchors as of the newest sealed batch, falling back to the committed ones.
    func sealedAnchors() throws -> [String: Data] {
        if state.resendRequested == true {
            // A re-send reads from the beginning, then continues from its own sealed progress.
            let base = state.resendFromSeq ?? state.lastSealedSeq
            var anchors = state.resendCommittedAnchors ?? [:]
            for entry in try store.outbox() where entry.seq > base { anchors.merge(entry.anchors) { _, new in new } }
            return anchors
        }
        var anchors = state.committedAnchors
        for entry in try store.outbox() { anchors.merge(entry.anchors) { _, new in new } }
        return anchors
    }

    var outboxFull: Bool { ((try? store.outbox().count) ?? 0) >= state.limits.seq_window }

    // MARK: sync

    /// Read HealthKit changes into sealed batches, then upload. Safe to call from any trigger: a call
    /// while a pass is running only asks that pass to run once more.
    /// With `uploadInForeground: false` only grants are uploaded here; the caller hands the sealed
    /// batches to a background URLSession (see `pendingUploads`).
    @discardableResult
    public func sync(source: HealthSource, now: Date = Date(), uploadInForeground: Bool = true) async throws -> Int {
        if case .halted(let reason, _) = state.stop { throw SyncError.stopped(reason) }
        guard state.currentGrant != nil else { throw SyncError.noProcessors }
        if syncing {
            // Coalesce: the running pass runs once more (with foreground uploads if anyone asked),
            // and this caller returns when it is done.
            rerun = true
            rerunUploadsInForeground = rerunUploadsInForeground || uploadInForeground
            return try await withCheckedContinuation { waiters.append($0) }.get()
        }
        syncing = true
        var outcome: Result<Int, Error> = .success(0)
        defer {
            syncing = false
            rerun = false
            rerunUploadsInForeground = false
            let done = waiters
            waiters = []
            done.forEach { $0.resume(returning: outcome) }
        }
        do {
            let sealed = try await syncPasses(source: source, now: now, uploadInForeground: uploadInForeground)
            outcome = .success(sealed)
            return sealed
        } catch {
            outcome = .failure(error)
            throw error
        }
    }

    private func syncPasses(source: HealthSource, now: Date, uploadInForeground: Bool) async throws -> Int {
        var sealed = 0
        var uploadInForeground = uploadInForeground
        repeat {
            if case .halted(let reason, _) = state.stop { throw SyncError.stopped(reason) }
            if rerun { uploadInForeground = uploadInForeground || rerunUploadsInForeground }
            rerun = false
            rerunUploadsInForeground = false
            if source.isReadable && !outboxFull {
                do {
                    sealed += try await sealChanges(source: source, now: now)
                    if !outboxFull { sealed += try await sealDaily(source: source, now: now) }
                    state.lastSyncAt = now
                } catch let e as SyncError {
                    throw e
                } catch {
                    // A HealthKit failure must not keep already sealed batches from uploading.
                    state.lastError = "reading Health data: \(error.localizedDescription)"
                }
                try save()
            }
            if uploadInForeground {
                try await uploadOutbox(now: now)
            } else {
                try await uploadPendingGrants()
            }
        } while rerun
        return sealed
    }

    func sealChanges(source: HealthSource, now: Date) async throws -> Int {
        // A re-send is pinned when its own pass starts, not when it is requested: batches sealed by a
        // pass already running at request time must not count as re-send progress.
        if state.resendRequested == true && state.resendFromSeq == nil {
            state.resendFromSeq = state.lastSealedSeq
            state.resendCommittedAnchors = [:]
            try save()
        }
        let generation = state.resendGeneration
        var anchors = try sealedAnchors()
        let fullPass = state.resendRequested == true
        var pending: [[String: Any]] = []
        var pendingBytes = 0
        var pendingAnchors: [String: Data] = [:]
        var count = 0
        func flush() throws {
            guard !pending.isEmpty else { return }
            count += try seal(pending, reason: fullPass ? "backfill" : "sync", anchors: pendingAnchors, now: now).count
            pending = []; pendingBytes = 0; pendingAnchors = [:]
        }
        var errors = state.typeErrors ?? [:]
        for type in source.syncTypes() {
            let limit = Self.seriesTypes.contains(type) ? min(pageLimit, Self.seriesPageLimit) : pageLimit
            var more = true
            while more {
                let page: HealthPage
                do {
                    page = try await source.changes(type: type, anchor: anchors[type], limit: limit)
                    errors.removeValue(forKey: type)
                } catch {
                    // One unreadable type (not authorized, unavailable) must not stop the others.
                    errors[type] = error.localizedDescription
                    break
                }
                pending += page.records
                for r in page.records { pendingBytes += (try? Encoding.json(Encoding.sanitize(r)).count) ?? 0 }
                if let a = page.anchor { anchors[type] = a; pendingAnchors[type] = a }
                more = page.more
                if pendingBytes >= softBatchBytes { try flush() }
                if outboxFull {
                    try flush()
                    state.typeErrors = errors
                    return count  // continue next time, from the anchors sealed so far
                }
            }
        }
        state.typeErrors = errors
        if !pending.isEmpty { try flush() } else if !pendingAnchors.isEmpty {
            // Anchors moved without records (nothing new): remember them without a batch.
            try commitAnchorsIfIdle(pendingAnchors)
        }
        if fullPass {
            if state.resendGeneration == generation {
                state.resendRequested = nil
                state.resendCommittedAnchors = nil
            }
            state.resendFromSeq = nil  // a newer request starts its own pass from here
        }
        return count
    }

    /// With an empty outbox, anchors that moved without producing records can be committed directly.
    func commitAnchorsIfIdle(_ anchors: [String: Data]) throws {
        if try store.outbox().isEmpty {
            state.committedAnchors.merge(anchors) { _, new in new }
            try save()
        }
    }

    /// Daily re-send of the last 48 hours (also the heartbeat), weekly census, and the profile when it
    /// changes (at least weekly).
    func sealDaily(source: HealthSource, now: Date) async throws -> Int {
        var count = 0
        var records: [[String: Any]] = []
        let resendDue = state.lastResendAt.map { now.timeIntervalSince($0) >= 20 * 3600 } ?? true
        if let p = source.profile(now: now) {
            var fields = p
            fields.removeValue(forKey: "observed_at")
            let sha = Encoding.sha256Hex((try? Encoding.json(Encoding.sanitize(fields))) ?? Data())
            let weekly = state.lastProfileAt.map { now.timeIntervalSince($0) >= 7 * 86400 } ?? true
            if sha != state.lastProfileSHA || weekly {
                records.append(p)
                state.lastProfileAt = now
                state.lastProfileSHA = sha
            }
        }
        if resendDue {
            for type in source.syncTypes() {
                if let r = try? await source.recent(type: type, since: now.addingTimeInterval(-48 * 3600)) { records += r }
            }
        }
        if resendDue || !records.isEmpty {
            count += try seal(records, reason: "resend", anchors: [:], now: now).count
            if resendDue { state.lastResendAt = now }
        }
        if state.lastCensusAt.map({ now.timeIntervalSince($0) >= 7 * 86400 }) ?? true, !outboxFull {
            let census = try await source.census(days: 30, now: now)
            if !census.isEmpty { count += try seal(census, reason: "census", anchors: [:], now: now).count }
            state.lastCensusAt = now
        }
        try save()
        return count
    }

    // MARK: upload

    /// Whether uploads are allowed now (spec 4.3 device rules and 6.2 classes).
    func uploadsAllowed(now: Date) -> Bool {
        if case .halted = state.stop { return false }
        if case .paused(_, let since) = state.stop, now.timeIntervalSince(since) < 20 * 3600 { return false }
        if state.rollbackSuspectedAt != nil { return false }  // stop until the second read decides
        return true
    }

    /// Entries that still need a 200: not acknowledged, retry time reached, within the relay's window.
    func uploadable(now: Date) throws -> [OutboxEntry] {
        let acked = Set(state.ackedAbove)
        let limit = state.ackedContiguous + state.limits.seq_window
        return try store.outbox().filter {
            $0.seq > state.ackedContiguous && !acked.contains($0.seq) && $0.seq <= limit && ($0.notBefore ?? .distantPast) <= now
        }
    }

    public func uploadOutbox(now: Date = Date()) async throws {
        if case .halted(let reason, _) = state.stop { throw SyncError.stopped(reason) }
        guard uploadsAllowed(now: now) else { return }
        try await uploadPendingGrants()
        for entry in try uploadable(now: now) {
            guard let data = try? Data(contentsOf: store.ciphertextURL(entry.seq)) else { continue }  // acked meanwhile
            do {
                try await client.putBatch(seq: entry.seq, manifest: entry.manifest, ciphertext: data)
                try acknowledge(entry.seq, now: now)
            } catch let e as RelayError {
                try handleUploadError(e, seq: entry.seq, now: now)
                if e.kind != .retryable || e.code == "network" { return }
            }
        }
    }

    /// Outbox entries ready to upload now, with the request to send and the ciphertext file, for a
    /// background URLSession. Nothing is returned while uploads are stopped or a grant is unacknowledged.
    public func pendingUploads(now: Date = Date()) throws -> [(seq: Int, request: URLRequest, file: URL)] {
        guard uploadsAllowed(now: now), state.grants.allSatisfy(\.acked) else { return [] }
        return try uploadable(now: now).map {
            ($0.seq, client.batchRequest(seq: $0.seq, manifest: $0.manifest, size: $0.size), store.ciphertextURL($0.seq))
        }
    }

    /// Record a 200 for `seq`, advance the contiguous prefix, and commit anchors behind it.
    public func acknowledge(_ seq: Int, now: Date = Date()) throws {
        let outbox = try store.outbox()
        // Only a batch this device sealed and still holds can be acknowledged (a stale result from an
        // earlier enrolment must not mark a new batch as stored).
        guard seq <= state.lastSealedSeq, seq <= state.ackedContiguous || outbox.contains(where: { $0.seq == seq }) else { return }
        if !state.ackedAbove.contains(seq) && seq > state.ackedContiguous {
            state.ackedAbove.append(seq)
            if let e = outbox.first(where: { $0.seq == seq }) {
                let record = UploadRecord(seq: seq, size: e.size, reason: e.reason, createdAt: e.createdAt, uploadedAt: now)
                state.recentUploads = Array(([record] + (state.recentUploads ?? [])).prefix(Self.recentUploadLimit))
            }
        }
        while state.ackedAbove.contains(state.ackedContiguous + 1) {
            state.ackedContiguous += 1
            let reached = state.ackedContiguous
            state.ackedAbove.removeAll { $0 == reached }
            if let e = outbox.first(where: { $0.seq == reached }) {
                state.committedAnchors.merge(e.anchors) { _, new in new }
                if let base = state.resendFromSeq, reached > base {
                    state.resendCommittedAnchors = (state.resendCommittedAnchors ?? [:]).merging(e.anchors) { _, new in new }
                }
            }
        }
        state.lastUploadAt = now
        if case .paused = state.stop { state.stop = nil }
        try save()
        for e in outbox where e.seq <= state.ackedContiguous { try store.removeFromOutbox(e.seq) }
    }

    public func handleUploadError(_ e: RelayError, seq: Int, now: Date) throws {
        // A late failure for a batch already acknowledged (two uploads of one seq) changes nothing.
        guard seq > state.ackedContiguous, !state.ackedAbove.contains(seq),
              FileManager.default.fileExists(atPath: store.ciphertextURL(seq).path),
              var entry = try store.outbox().first(where: { $0.seq == seq }) else { return }
        entry.attempts += 1
        entry.lastError = "\(e.code): \(e.message)"
        switch e.kind {
        case .permanent:
            state.stop = .halted(reason: "batch \(entry.seq) rejected (\(e.code)). Ask the admin to retire this device, "
                                 + "then start over as a new device.", since: now)
        case .afterAdmin:
            state.stop = .paused(reason: "storage quota reached; the admin must raise it", since: now)
        case .retryable:
            let wait = TimeInterval(e.extra["retry_after"] ?? min(3600, 30 << min(entry.attempts, 7)))
            entry.notBefore = now.addingTimeInterval(e.code == "seq_out_of_window" || e.code == "grant_unknown" ? 60 : wait)
        }
        state.lastError = entry.lastError
        try store.updateEntry(entry)
        try save()
    }

    func record(_ e: RelayError, what: String) throws {
        state.lastError = "\(what): \(e.code)"
        if e.kind == .permanent {
            state.stop = .halted(reason: "\(what) rejected (\(e.code)). Ask the admin to retire this device, "
                                 + "then start over as a new device.", since: Date())
        }
        try save()
    }

    /// An "after admin action" pause is retried on every app launch (spec 4.3 device rules).
    public func retryPauseOnLaunch() throws {
        if case .paused(let reason, _) = state.stop {
            state.stop = .paused(reason: reason, since: .distantPast)
            try save()
        }
    }

    // MARK: relay health (spec 4.3 rollback rule)

    /// Compare the relay's view with what was acknowledged. A first mismatch stops uploads; one
    /// confirmed at least 10 minutes later halts them, compared with the acknowledged seq recorded
    /// at the first observation so that uploads in between cannot hide it.
    public func checkRelay(now: Date = Date()) async throws {
        let ackedBefore = state.highestAcked  // an ack arriving during the read must not look like a rollback
        let s = try await client.state()
        if let l = s.limits { state.limits = l }
        if var usage = s.usage {
            usage.checkedAt = now
            state.relayUsage = usage
        }
        recordReceipts(s.receipts)
        if let since = state.rollbackSuspectedAt, let acked = state.rollbackAckedAtSuspicion {
            if now.timeIntervalSince(since) >= 600 {
                if s.maxSeq < acked {
                    state.stop = .halted(reason: "the relay lost data it had acknowledged (has \(s.maxSeq), "
                                         + "acknowledged \(acked)). Start over as a new device.", since: now)
                }
                state.rollbackSuspectedAt = nil
                state.rollbackAckedAtSuspicion = nil
            }
        } else if s.maxSeq < ackedBefore {
            state.rollbackSuspectedAt = now
            state.rollbackAckedAtSuspicion = ackedBefore
        }
        if s.status != "active", state.stop == nil {
            state.stop = .halted(reason: "this device is \(s.status) on the relay", since: now)
        }
        try save()
    }

    /// Keep the highest receipt per processor that verifies against this device's own grants.
    func recordReceipts(_ signed: [Signed]) {
        var known: [String: Processor] = [:]
        for g in state.grants { for p in g.processors { known[p.id] = p } }
        var kept = state.receipts ?? [:]
        for s in signed {
            guard let r = Receipt.verified(doc: s.doc, sig: s.sig, deviceID: state.deviceID, processors: known,
                                           lastSealedSeq: state.lastSealedSeq) else { continue }
            if r.seq > kept[r.processorID]?.seq ?? 0 { kept[r.processorID] = r }
        }
        if !kept.isEmpty { state.receipts = kept }
    }

    /// Re-read everything: the next pass reads every type from the beginning (for a newly added processor).
    public func resendHistory() throws {
        state.resendRequested = true
        state.resendFromSeq = nil  // pinned when the re-send pass starts (sealChanges)
        state.resendGeneration = (state.resendGeneration ?? 0) + 1
        try save()
    }

    public func purgeOnRelay() async throws { try await client.purge() }
}
