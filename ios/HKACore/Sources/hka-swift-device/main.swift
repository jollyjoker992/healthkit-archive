// A command-line stand-in for the iOS app, built from the same HKACore code, for end-to-end
// tests against a relay and the Python processor (cli/tests/test_swift_e2e.py).
//
//   hka-swift-device <dir> enroll <invite> [name]
//   hka-swift-device <dir> processors <card> [<card> ...]
//   hka-swift-device <dir> upload <records.ndjson>     seal the records as one or more batches and upload
//   hka-swift-device <dir> info
import CryptoKit
import Foundation
import HKACore

struct FileSource: HealthSource {
    let records: [[String: Any]]
    func syncTypes() -> [String] { ["file"] }
    func changes(type: String, anchor: Data?, limit: Int) async throws -> HealthPage {
        anchor == nil ? HealthPage(records: records, anchor: Data("done".utf8), more: false)
                      : HealthPage(records: [], anchor: anchor, more: false)
    }
    func recent(type: String, since: Date) async throws -> [[String: Any]] { [] }
    func census(days: Int, now: Date) async throws -> [[String: Any]] { [] }
    func profile(now: Date) -> [String: Any]? { nil }
    var isReadable: Bool { true }
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func signer(at dir: URL) throws -> SoftwareSigner {
    let path = dir.appendingPathComponent("device.key")
    if let raw = try? Data(contentsOf: path) { return SoftwareSigner(key: try P256.Signing.PrivateKey(rawRepresentation: raw)) }
    let s = SoftwareSigner()
    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
    try s.key.rawRepresentation.write(to: path)
    return s
}

func device(at dir: URL) throws -> Device {
    let store = try DeviceStore(root: dir.appendingPathComponent("store"))
    guard let state = try store.loadState() else { fail("not enrolled") }
    return Device(store: store, state: state, signer: try signer(at: dir), tz: { "Asia/Ho_Chi_Minh" }, appVersion: "swift-device")
}

let args = CommandLine.arguments
guard args.count >= 3 else { fail("usage: hka-swift-device <dir> enroll|processors|upload|info …") }
let dir = URL(fileURLWithPath: args[1])

do {
    switch args[2] {
    case "enroll":
        let s = try signer(at: dir)
        let store = try DeviceStore(root: dir.appendingPathComponent("store"))
        let state = try await Device.enroll(invite: try Invite.parse(args[3]), deviceName: args.count > 4 ? args[4] : "Swift device",
                                            signer: s, store: store)
        print(state.deviceID)
        print(s.fingerprint)
    case "processors":
        let d = try device(at: dir)
        try await d.setProcessors(try args.dropFirst(3).map { try Processor.parseCard($0) })
        print("grant \(await d.state.currentGrant!.grantSeq)")
    case "upload":
        let d = try device(at: dir)
        let text = try String(contentsOf: URL(fileURLWithPath: args[3]), encoding: .utf8)
        let records = text.split(separator: "\n").compactMap { Encoding.parseObject(Data($0.utf8)) }
        await d.configure(softBatchBytes: Int(ProcessInfo.processInfo.environment["HKA_SOFT_BATCH"] ?? ""))
        // Skip the daily re-send and census: this tool uploads exactly the given records.
        let sealed = try await d.sync(source: FileSource(records: records), now: Date())
        print("sealed \(sealed), outbox \(try d.store.outbox().count), acked \(await d.state.ackedContiguous)")
    case "info":
        let state = await try device(at: dir).state
        print(String(data: try JSONSerialization.data(withJSONObject: [
            "device_id": state.deviceID, "acked": state.ackedContiguous, "sealed": state.lastSealedSeq,
            "stop": String(describing: state.stop),
        ], options: .sortedKeys), encoding: .utf8)!)
    default:
        fail("unknown command \(args[2])")
    }
} catch {
    fail("error: \(error)")
}
