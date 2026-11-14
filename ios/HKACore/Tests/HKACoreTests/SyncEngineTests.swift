import CryptoKit
import Foundation
import XCTest
@testable import HKACore

/// A relay stub: records requests and answers from a script.
final class StubRelay: URLProtocol {
    nonisolated(unsafe) static var requests: [(method: String, path: String, headers: [String: String], body: Data)] = []
    nonisolated(unsafe) static var answer: (String, String, Data) -> (Int, [String: Any]) = { _, _, _ in (200, [:]) }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        var body = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open()
            var buf = [UInt8](repeating: 0, count: 65536)
            while stream.hasBytesAvailable {
                let n = stream.read(&buf, maxLength: buf.count)
                if n <= 0 { break }
                body.append(buf, count: n)
            }
            stream.close()
        }
        let path = request.url!.path + (request.url!.query.map { "?\($0)" } ?? "")
        Self.requests.append((request.httpMethod!, path, request.allHTTPHeaderFields ?? [:], body))
        let (status, json) = Self.answer(request.httpMethod!, path, body)
        let data = try! JSONSerialization.data(withJSONObject: json)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil,
                                                              headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

struct FakeSource: HealthSource {
    var pages: [String: [HealthPage]] = [:]
    var delay: Double = 0
    var readable = true
    var recentRecords: [[String: Any]] = []

    func syncTypes() -> [String] { pages.keys.sorted() }
    func changes(type: String, anchor: Data?, limit: Int) async throws -> HealthPage {
        if delay > 0 { try await Task.sleep(nanoseconds: UInt64(delay * 1e9)) }
        let all = pages[type] ?? []
        let index = anchor.flatMap { Int(String(data: $0, encoding: .utf8)!) } ?? 0
        guard index < all.count else { return HealthPage(records: [], anchor: anchor, more: false) }
        var page = all[index]
        page.anchor = Data(String(index + 1).utf8)
        page.more = index + 1 < all.count
        return page
    }
    func recent(type: String, since: Date) async throws -> [[String: Any]] { recentRecords }
    func census(days: Int, now: Date) async throws -> [[String: Any]] { [] }
    func profile(now: Date) -> [String: Any]? { nil }
    var isReadable: Bool { readable }
}

func hr(_ i: Int) -> [String: Any] {
    ["k": "sample", "kind": "quantity", "type": "HKQuantityTypeIdentifierHeartRate", "uuid": UUID().uuidString,
     "start": "2026-11-12T01:00:00.000Z", "end": "2026-11-12T01:00:00.000Z", "value": 60 + i, "unit": "count/min",
     "source": ["bundle_id": "x", "name": "x"], "device": NSNull(), "metadata": [:]]
}

final class SyncEngineTests: XCTestCase {
    var dir: URL!
    var device: Device!
    var signer: SoftwareSigner!
    var session: URLSession!
    let now = Date(timeIntervalSince1970: 1794556800)

    override func setUp() async throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        StubRelay.requests = []
        StubRelay.answer = { _, _, _ in (200, [:]) }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubRelay.self]
        session = URLSession(configuration: config)
        let store = try DeviceStore(root: dir)
        signer = SoftwareSigner()
        let state = DeviceState(server: "https://relay.example.test", userID: "u_" + String(repeating: "a", count: 32),
                                userName: "Mom", deviceID: signer.deviceID, deviceName: "iPhone", enrolledAt: now)
        try store.saveState(state)
        device = Device(store: store, state: state, signer: signer, session: session, tz: { "Asia/Ho_Chi_Minh" })
        let card = try (vectors("documents.json")["card"] as! [String: String])["text"]!
        try await device.setProcessors([try Processor.parseCard(card)], now: now)
    }

    override func tearDown() { try? FileManager.default.removeItem(at: dir) }

    func skipDaily() async throws {
        try await device.mutate { $0.lastResendAt = self.now; $0.lastCensusAt = self.now; $0.lastProfileAt = self.now }
    }

    var batchPuts: [(seq: Int, manifest: [String: Any])] {
        StubRelay.requests.filter { $0.method == "PUT" && $0.path.contains("/batches/") }.map {
            (Int($0.path.split(separator: "/").last!)!,
             Encoding.parseObject(Encoding.b64uDecode($0.headers["HKA-Manifest"]!)!)!)
        }
    }

    func one(_ records: [[String: Any]] = [hr(1)], type: String = "T") -> FakeSource {
        FakeSource(pages: [type: [HealthPage(records: records, anchor: nil, more: false)]])
    }

    func testGrantIsSavedBeforeUploadAndUploadedFirst() async throws {
        XCTAssertEqual(StubRelay.requests.first?.path, "/v1/devices/\(signer.deviceID)/grant")
        let acked = await device.state.grants.map(\.acked)
        XCTAssertEqual(acked, [true])
    }

    func testSyncSealsChainsAndCommitsAnchorsAfterAck() async throws {
        try await skipDaily()
        try await device.sync(source: one([hr(1), hr(2)], type: "HKQuantityTypeIdentifierHeartRate"), now: now)
        XCTAssertEqual(batchPuts.map(\.seq), [1])
        XCTAssertTrue(batchPuts[0].manifest["prev"] is NSNull)
        let state = await device.state
        XCTAssertEqual(state.ackedContiguous, 1)
        XCTAssertEqual(state.committedAnchors["HKQuantityTypeIdentifierHeartRate"], Data("1".utf8))
        XCTAssertTrue(try device.store.outbox().isEmpty)
    }

    func testChainLinksAcrossBatchesAndDailyResendIsAHeartbeat() async throws {
        try await device.mutate { $0.lastCensusAt = self.now }
        try await device.sync(source: one(), now: now)
        XCTAssertEqual(batchPuts.map(\.seq), [1, 2])  // changes, then the daily re-send (empty: a heartbeat)
        let first = StubRelay.requests.first { $0.path.hasSuffix("/batches/1") }!
        XCTAssertEqual(batchPuts[1].manifest["prev"] as? String, Encoding.sha256Hex(Encoding.b64uDecode(first.headers["HKA-Manifest"]!)!))
    }

    func testRetryableErrorKeepsTheBatchAndPermanentErrorHalts() async throws {
        try await skipDaily()
        StubRelay.answer = { _, path, _ in path.contains("/batches/") ? (409, ["error": "grant_unknown", "grant_seq": 0]) : (200, [:]) }
        try await device.sync(source: one(), now: now)
        XCTAssertEqual(try device.store.outbox().count, 1)
        var state = await device.state
        XCTAssertNil(state.stop)
        XCTAssertNil(state.committedAnchors["T"])

        StubRelay.answer = { _, path, _ in path.contains("/batches/") ? (409, ["error": "seq_conflict"]) : (200, [:]) }
        try await device.uploadOutbox(now: now.addingTimeInterval(3600))
        state = await device.state
        guard case .halted = state.stop else { return XCTFail("expected halted") }
        do {
            try await device.sync(source: one(), now: now.addingTimeInterval(7200))
            XCTFail("sync must refuse while halted")
        } catch SyncError.stopped {}
    }

    func testOutOfOrderAcksCommitOnlyTheContiguousPrefix() async throws {
        let src = FakeSource(pages: ["T": [HealthPage(records: [hr(1)], anchor: nil, more: false),
                                           HealthPage(records: [hr(2)], anchor: nil, more: false)]])
        try await skipDaily()
        await device.configure(softBatchBytes: 1)  // one batch per page
        StubRelay.answer = { _, _, _ in (503, ["error": "internal"]) }
        try await device.sync(source: src, now: now)
        XCTAssertEqual(try device.store.outbox().map(\.seq), [1, 2])
        try await device.acknowledge(2, now: now)
        var state = await device.state
        XCTAssertEqual(state.ackedContiguous, 0)
        XCTAssertNil(state.committedAnchors["T"])
        try await device.acknowledge(1, now: now)
        state = await device.state
        XCTAssertEqual(state.ackedContiguous, 2)
        XCTAssertEqual(state.committedAnchors["T"], Data("2".utf8))
    }

    func testAcknowledgedEntriesAreNotUploadedAgain() async throws {
        try await skipDaily()
        await device.configure(softBatchBytes: 1)
        StubRelay.answer = { _, _, _ in (503, ["error": "internal"]) }
        try await device.sync(source: FakeSource(pages: ["T": [HealthPage(records: [hr(1)], anchor: nil, more: false),
                                                               HealthPage(records: [hr(2)], anchor: nil, more: false)]]), now: now)
        try await device.acknowledge(2, now: now)
        let pending = try await device.pendingUploads(now: now.addingTimeInterval(7200)).map(\.seq)
        XCTAssertEqual(pending, [1])
    }

    func testLateErrorAfterAckDoesNotResurrectTheEntry() async throws {
        try await skipDaily()
        try await device.sync(source: one(), now: now)
        XCTAssertTrue(try device.store.outbox().isEmpty)
        try await device.handleUploadError(RelayError(status: 0, code: "network", message: "", extra: [:]), seq: 1, now: now)
        XCTAssertTrue(try device.store.outbox().isEmpty)
    }

    func testLockedPhoneSealsNothingButStillUploads() async throws {
        var src = one()
        src.readable = false
        StubRelay.requests = []
        try await device.sync(source: src, now: now)
        XCTAssertTrue(batchPuts.isEmpty)
        let sealed = await device.state.lastSealedSeq
        XCTAssertEqual(sealed, 0)
    }

    func testTotalQuotaPausesAndRetriesNextDayOrOnLaunch() async throws {
        try await skipDaily()
        StubRelay.answer = { _, path, _ in path.contains("/batches/") ? (413, ["error": "quota_total_exceeded"]) : (200, [:]) }
        try await device.sync(source: one(), now: now)
        var state = await device.state
        guard case .paused = state.stop else { return XCTFail("expected paused") }
        StubRelay.requests = []
        try await device.uploadOutbox(now: now.addingTimeInterval(3600))
        XCTAssertTrue(batchPuts.isEmpty)
        StubRelay.answer = { _, _, _ in (200, [:]) }
        try await device.retryPauseOnLaunch()
        try await device.uploadOutbox(now: now.addingTimeInterval(3700))
        state = await device.state
        XCTAssertNil(state.stop)
        XCTAssertEqual(state.ackedContiguous, 1)
    }

    func stateAnswer(maxSeq: Int) -> (String, String, Data) -> (Int, [String: Any]) {
        { _, path, _ in
            path.hasSuffix("/state") ? (200, ["status": "active", "contiguous_seq": maxSeq, "max_seq": maxSeq, "grant_seq": 1]) : (200, [:])
        }
    }

    func testSuspectedRollbackStopsUploadsAndIsConfirmedAgainstTheFirstReading() async throws {
        try await device.mutate { $0.ackedContiguous = 10; $0.lastSealedSeq = 10 }
        StubRelay.answer = stateAnswer(maxSeq: 3)
        try await device.checkRelay(now: now)
        var state = await device.state
        XCTAssertNotNil(state.rollbackSuspectedAt)
        XCTAssertNil(state.stop)
        // Uploads are stopped while suspected, so they cannot hide the rollback.
        try await skipDaily()
        StubRelay.requests = []
        try await device.sync(source: one(), now: now.addingTimeInterval(60))
        XCTAssertTrue(batchPuts.isEmpty)
        // Even if the relay now claims a high max_seq, the first reading decides.
        StubRelay.answer = stateAnswer(maxSeq: 5)
        try await device.checkRelay(now: now.addingTimeInterval(700))
        state = await device.state
        guard case .halted = state.stop else { return XCTFail("expected halted") }
    }

    func testATransientMismatchClears() async throws {
        try await device.mutate { $0.ackedContiguous = 4; $0.lastSealedSeq = 4 }
        StubRelay.answer = stateAnswer(maxSeq: 2)
        try await device.checkRelay(now: now)
        StubRelay.answer = stateAnswer(maxSeq: 4)
        try await device.checkRelay(now: now.addingTimeInterval(700))
        let state = await device.state
        XCTAssertNil(state.stop)
        XCTAssertNil(state.rollbackSuspectedAt)
    }

    func testOversizedBatchIsSplit() async throws {
        try await device.mutate { $0.limits.max_batch_bytes = 2000 }
        try await skipDaily()
        try await device.sync(source: one((0..<200).map(hr)), now: now)
        XCTAssertGreaterThan(batchPuts.count, 1)
        XCTAssertTrue(StubRelay.requests.filter { $0.path.contains("/batches/") }.allSatisfy { $0.body.count <= 2000 })
        let anchors = await device.state.committedAnchors
        XCTAssertEqual(anchors["T"], Data("1".utf8))
    }

    func testCrashBetweenOutboxWriteAndStateSaveNeverResignsASeq() async throws {
        try await skipDaily()
        StubRelay.answer = { _, _, _ in (503, ["error": "internal"]) }
        try await device.sync(source: one(), now: now)
        let entry = try device.store.outbox()[0]
        // Simulate the crash: state on disk still says nothing was sealed.
        var stale = await device.state
        stale.lastSealedSeq = 0
        stale.lastSealedManifestSHA = nil
        try device.store.saveState(stale)
        let restarted = Device(store: device.store, state: try device.store.loadState()!, signer: signer, session: session)
        let state = await restarted.state
        XCTAssertEqual(state.lastSealedSeq, 1)
        XCTAssertEqual(state.lastSealedManifestSHA, entry.manifest.sha256)
        try await restarted.mutate { $0.lastResendAt = Date.distantFuture }
        let more = FakeSource(pages: ["T": [HealthPage(records: [hr(1)], anchor: nil, more: false),
                                            HealthPage(records: [hr(2)], anchor: nil, more: false)]])
        try await restarted.sync(source: more, now: now.addingTimeInterval(10))
        XCTAssertEqual(try device.store.outbox().map(\.seq), [1, 2])
        XCTAssertEqual(try device.store.outbox()[0].manifest, entry.manifest)
    }

    func testConcurrentSyncsAreCoalesced() async throws {
        try await skipDaily()
        await device.configure(softBatchBytes: 1)
        let src = FakeSource(pages: ["T": (0..<5).map { HealthPage(records: [hr($0)], anchor: nil, more: false) }])
        try await withThrowingTaskGroup(of: Int.self) { group in
            for _ in 0..<20 { group.addTask { try await self.device.sync(source: src, now: self.now) } }
            for try await _ in group {}
        }
        let seqs = batchPuts.map(\.seq)
        XCTAssertEqual(Set(seqs).count, seqs.count, "no seq uploaded twice with different content")
        let manifests = StubRelay.requests.filter { $0.path.contains("/batches/") }.map { $0.headers["HKA-Manifest"]! }
        XCTAssertEqual(Set(manifests).count, manifests.count)
        let state = await device.state
        XCTAssertEqual(state.ackedContiguous, state.lastSealedSeq)
        XCTAssertEqual(state.committedAnchors["T"], Data("5".utf8))
    }

    func testNonFiniteNumbersBecomeNull() async throws {
        try await skipDaily()
        var bad = hr(1)
        bad["value"] = Double.nan
        bad["metadata"] = ["x": Double.infinity]
        try await device.sync(source: one([bad]), now: now)
        XCTAssertEqual(batchPuts.count, 1)
        XCTAssertThrowsError(try Encoding.json(["v": Double.nan]))
    }

    func testResendHistoryReadsFromTheBeginningOnce() async throws {
        try await skipDaily()
        let src = FakeSource(pages: ["T": [HealthPage(records: [hr(1)], anchor: nil, more: false)]])
        try await device.sync(source: src, now: now)
        try await device.sync(source: src, now: now)
        XCTAssertEqual(batchPuts.count, 1)
        try await device.resendHistory()
        try await device.sync(source: src, now: now)
        XCTAssertEqual(batchPuts.count, 2)
        let flag = await device.state.resendRequested
        XCTAssertNil(flag)
    }

    func testResendLargerThanTheWindowCompletesAcrossPasses() async throws {
        try await skipDaily()
        try await device.mutate { $0.limits.seq_window = 2 }
        await device.configure(softBatchBytes: 1)
        let src = FakeSource(pages: ["T": (0..<5).map { HealthPage(records: [hr($0)], anchor: nil, more: false) }])
        try await device.sync(source: src, now: now)          // initial read: 2 per pass (window)
        for _ in 0..<3 { try await device.sync(source: src, now: now) }
        var state = await device.state
        XCTAssertEqual(state.committedAnchors["T"], Data("5".utf8))
        let before = batchPuts.count
        try await device.resendHistory()
        for _ in 0..<5 { try await device.sync(source: src, now: now) }
        state = await device.state
        XCTAssertNil(state.resendRequested, "the re-send finished")
        XCTAssertEqual(batchPuts.count - before, 5, "each page re-sent exactly once")
        XCTAssertEqual(state.committedAnchors["T"], Data("5".utf8))
    }

    func testStaleAcknowledgementsAreIgnored() async throws {
        try await device.acknowledge(1, now: now)  // nothing sealed yet: a result from an earlier enrolment
        var state = await device.state
        XCTAssertEqual(state.ackedContiguous, 0)
        try await skipDaily()
        try await device.sync(source: one(), now: now)
        state = await device.state
        XCTAssertEqual(state.ackedContiguous, 1)
        XCTAssertEqual(batchPuts.map(\.seq), [1])
    }

    func testSanitizeKeepsBooleansAndIntegers() throws {
        let rec = Encoding.parseObject(Data(#"{"b":true,"i":3,"d":1.5}"#.utf8))!
        let json = String(data: try Encoding.json(Encoding.sanitize(rec)), encoding: .utf8)!
        XCTAssertEqual(json, #"{"b":true,"d":1.5,"i":3}"#)
    }

    func testCoalescedCallerWaitsForThePass() async throws {
        try await skipDaily()
        let src = one()
        async let a = device.sync(source: src, now: now, uploadInForeground: false)
        async let b = device.sync(source: src, now: now)  // foreground request joins the running pass
        _ = try await (a, b)
        let acked = await device.state.ackedContiguous
        XCTAssertEqual(acked, 1, "the joined foreground request made the pass upload")
    }

    func testResendRequestedDuringARunningPassStillResendsEverything() async throws {
        try await skipDaily()
        await device.configure(softBatchBytes: 1)
        var src = FakeSource(pages: ["A": (0..<3).map { HealthPage(records: [hr($0)], anchor: nil, more: false) },
                                     "B": (0..<3).map { HealthPage(records: [hr($0)], anchor: nil, more: false) }])
        try await device.sync(source: src, now: now)   // everything read once
        let before = batchPuts.count
        src.delay = 0.03
        let slow = src
        async let pass = device.sync(source: slow, now: now)  // a normal pass in flight (reads nothing new)
        try await Task.sleep(nanoseconds: 10_000_000)
        try await device.resendHistory()
        _ = try await pass
        try await device.sync(source: slow, now: now)
        let state = await device.state
        XCTAssertNil(state.resendRequested)
        XCTAssertEqual(batchPuts.count - before, 6, "all six pages re-sent")
    }

    func testOlderStateFilesStillDecode() throws {
        let json = #"{"server":"https://r.example.test","userID":"u_x","userName":"M","deviceID":"d_x","deviceName":"P","enrolledAt":"2026-11-13T08:00:00Z","limits":{"max_batch_bytes":1,"device_daily_bytes":1,"user_total_bytes":1,"seq_window":1},"grants":[],"lastSealedSeq":0,"ackedAbove":[],"ackedContiguous":0,"committedAnchors":{}}"#
        XCTAssertNoThrow(try DeviceStore.decoder.decode(DeviceState.self, from: Data(json.utf8)))
    }
}

/// Round 4 review: tests that fail if the re-send pinning or generation logic is removed.
final class Round4Tests: XCTestCase {
    var dir: URL!
    var device: Device!
    let now = Date(timeIntervalSince1970: 1794556800)
    override func setUp() async throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        StubRelay.requests = []
        StubRelay.answer = { _, _, _ in (200, [:]) }
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubRelay.self]
        let session = URLSession(configuration: config)
        let store = try DeviceStore(root: dir)
        let signer = SoftwareSigner()
        let state = DeviceState(server: "https://relay.example.test", userID: "u_" + String(repeating: "a", count: 32),
                                userName: "Mom", deviceID: signer.deviceID, deviceName: "iPhone", enrolledAt: now)
        try store.saveState(state)
        device = Device(store: store, state: state, signer: signer, session: session, tz: { "UTC" })
        let card = try (vectors("documents.json")["card"] as! [String: String])["text"]!
        try await device.setProcessors([try Processor.parseCard(card)], now: now)
        try await device.mutate { $0.lastResendAt = self.now; $0.lastCensusAt = self.now; $0.lastProfileAt = self.now }
        await device.configure(softBatchBytes: 1)
    }
    var puts: Int { StubRelay.requests.filter { $0.method == "PUT" && $0.path.contains("/batches/") }.count }
    func pages(_ n: Int) -> [HealthPage] { (0..<n).map { HealthPage(records: [hr($0)], anchor: nil, more: false) } }

    // A normal pass that seals NEW data after the request must not count as re-send progress.
    func testRequestDuringPassThatSealsNewData() async throws {
        var src = FakeSource(pages: ["A": pages(3), "B": pages(3)])
        try await device.sync(source: src, now: now)
        let before = puts
        src = FakeSource(pages: ["A": pages(5), "B": pages(5)]); src.delay = 0.03
        async let pass = device.sync(source: src, now: now)
        try await Task.sleep(nanoseconds: 10_000_000)
        try await device.resendHistory()
        _ = try await pass
        let mid = puts - before
        XCTAssertEqual(mid, 4)
        try await device.sync(source: src, now: now)
        XCTAssertEqual(puts - before - mid, 10, "all ten pages re-sent")
        let st = await device.state
        XCTAssertNil(st.resendRequested)
    }

    // A second request during a running re-send pass must cause another full re-send.
    func testRequestDuringRunningResend() async throws {
        var src = FakeSource(pages: ["A": pages(3), "B": pages(3)])
        try await device.sync(source: src, now: now)
        try await device.resendHistory()
        let before = puts
        src.delay = 0.03
        async let pass = device.sync(source: src, now: now)
        try await Task.sleep(nanoseconds: 50_000_000)
        try await device.resendHistory()
        _ = try await pass
        XCTAssertEqual(puts - before, 6)
        var st = await device.state
        XCTAssertEqual(st.resendRequested, true, "newer request survives")
        try await device.sync(source: src, now: now)
        XCTAssertEqual(puts - before, 12)
        st = await device.state
        XCTAssertNil(st.resendRequested)
        XCTAssertEqual(st.committedAnchors["A"], Data("3".utf8))
    }

    func testWaitersReceiveTheHaltError() async throws {
        StubRelay.answer = { _, p, _ in p.contains("/batches/") ? (409, ["error": "seq_conflict"]) : (200, [:]) }
        var src = FakeSource(pages: ["A": pages(2)]); src.delay = 0.03
        let d = device!
        let first = Task { try await d.sync(source: src, now: self.now) }
        try await Task.sleep(nanoseconds: 10_000_000)
        let second = Task { try await d.sync(source: src, now: self.now) }
        for result in [await first.result, await second.result] {
            guard case .failure(SyncError.stopped) = result else { return XCTFail("expected stopped, got \(result)") }
        }
        guard case .halted = await device.state.stop else { return XCTFail("expected halted") }
    }
}
