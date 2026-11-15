import Foundation

/// An error answer from the relay (spec 6.2), or a transport failure (status 0).
public struct RelayError: Error, Equatable {
    public let status: Int
    public let code: String
    public let message: String
    public let extra: [String: Int]

    public enum Kind: Equatable { case retryable, afterAdmin, permanent }

    /// Classification from the error table. Unknown codes are treated as retryable so nothing is
    /// abandoned on an error the app does not understand.
    public var kind: Kind {
        switch code {
        case "quota_total_exceeded": return .afterAdmin
        case "bad_request", "bad_signature", "unknown_key", "device_inactive", "device_purged", "not_granted",
             "forbidden", "invite_invalid", "not_found", "invite_used", "device_exists", "user_purged",
             "grant_conflict", "grant_mismatch", "seq_conflict", "batch_too_large":
            return .permanent
        default: return .retryable
        }
    }

    public static func transport(_ error: Error) -> RelayError {
        RelayError(status: 0, code: "network", message: error.localizedDescription, extra: [:])
    }

    public static func from(status: Int, body: Data) -> RelayError {
        let obj = Encoding.parseObject(body) ?? [:]
        var extra: [String: Int] = [:]
        for (k, v) in obj where k != "error" && k != "message" { if let i = jsonInt(v) { extra[k] = i } }
        return RelayError(status: status, code: obj["error"] as? String ?? "http_\(status)",
                          message: obj["message"] as? String ?? "", extra: extra)
    }
}

/// Foreground calls to the relay. Batch uploads in the background use `batchRequest` with a
/// background URLSession instead (headers are fixed when the task is created).
public final class RelayClient {
    public let origin: String
    let signer: DeviceSigner
    let session: URLSession
    /// Learned from `server_time` on a 401 so signed requests survive a wrong phone clock.
    public var clockSkew: TimeInterval = 0

    public init(origin: String, signer: DeviceSigner, session: URLSession = .shared) {
        self.origin = origin; self.signer = signer; self.session = session
    }

    func send(_ request: URLRequest, body: Data? = nil) async throws -> (Int, Data) {
        do {
            let (data, response): (Data, URLResponse)
            if let body {
                (data, response) = try await session.upload(for: request, from: body)
            } else {
                (data, response) = try await session.data(for: request)
            }
            return ((response as? HTTPURLResponse)?.statusCode ?? 0, data)
        } catch {
            throw RelayError.transport(error)
        }
    }

    func checked(_ result: (Int, Data)) throws -> [String: Any] {
        guard result.0 == 200 else { throw RelayError.from(status: result.0, body: result.1) }
        return Encoding.parseObject(result.1) ?? [:]
    }

    // MARK: enrolment (spec 4.1)

    public struct Enrolled: Equatable {
        public let userID: String
        public let deviceID: String
        public let limits: Limits
    }

    public func enroll(_ body: Signed) async throws -> Enrolled {
        var req = URLRequest(url: URL(string: origin + "/v1/enroll")!)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue(body.sig, forHTTPHeaderField: "HKA-Signature")
        let obj = try checked(await send(req, body: body.doc))
        guard let user = obj["user_id"] as? String, let device = obj["device_id"] as? String else {
            throw RelayError(status: 200, code: "bad_response", message: "enrolment response", extra: [:])
        }
        return Enrolled(userID: user, deviceID: device, limits: Self.limits(obj["limits"]) ?? .default)
    }

    static func limits(_ v: Any?) -> Limits? {
        guard let d = v as? [String: Any], let a = jsonInt(d["max_batch_bytes"]), let b = jsonInt(d["device_daily_bytes"]),
              let c = jsonInt(d["user_total_bytes"]), let w = jsonInt(d["seq_window"]) else { return nil }
        return Limits(max_batch_bytes: a, device_daily_bytes: b, user_total_bytes: c, seq_window: w)
    }

    // MARK: grant (spec 4.2)

    public func putGrant(_ grant: Signed) async throws {
        var req = URLRequest(url: URL(string: origin + "/v1/devices/\(signer.deviceID)/grant")!)
        req.httpMethod = "PUT"
        let body = try Encoding.json(["doc": Encoding.b64u(grant.doc), "sig": grant.sig])
        _ = try checked(await send(req, body: body))
    }

    // MARK: batch (spec 4.3)

    public func batchRequest(seq: Int, manifest: Signed, size: Int) -> URLRequest {
        var req = URLRequest(url: URL(string: origin + "/v1/devices/\(signer.deviceID)/batches/\(seq)")!)
        req.httpMethod = "PUT"
        req.setValue(Encoding.b64u(manifest.doc), forHTTPHeaderField: "HKA-Manifest")
        req.setValue(manifest.sig, forHTTPHeaderField: "HKA-Signature")
        req.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
        req.setValue(String(size), forHTTPHeaderField: "Content-Length")
        return req
    }

    public func putBatch(seq: Int, manifest: Signed, ciphertext: Data) async throws {
        _ = try checked(await send(batchRequest(seq: seq, manifest: manifest, size: ciphertext.count), body: ciphertext))
    }

    // MARK: signed requests (spec 5)

    public struct State: Equatable {
        public let status: String
        public let contiguousSeq: Int
        public let maxSeq: Int
        public let grantSeq: Int?
        public let limits: Limits?
        /// What the relay stores (spec 6.1); nil from a relay that predates these fields.
        public var usage: RelayUsage? = nil
    }

    func signed(_ method: String, _ target: String, retry: Bool = true) async throws -> [String: Any] {
        var req = URLRequest(url: URL(string: origin + target)!)
        req.httpMethod = method
        let now = Date().addingTimeInterval(clockSkew)
        for (k, v) in try Documents.requestHeaders(signer: signer, origin: origin, method: method, target: target, now: now) {
            req.setValue(v, forHTTPHeaderField: k)
        }
        do {
            return try checked(await send(req))
        } catch let e as RelayError where e.code == "stale_timestamp" && retry {
            if let serverTime = e.extra["server_time"] {
                clockSkew = TimeInterval(serverTime) - Date().timeIntervalSince1970
                return try await signed(method, target, retry: false)
            }
            throw e
        }
    }

    public func state() async throws -> State {
        let o = try await signed("GET", "/v1/devices/\(signer.deviceID)/state")
        guard let status = o["status"] as? String, let c = jsonInt(o["contiguous_seq"]), let m = jsonInt(o["max_seq"]) else {
            throw RelayError(status: 200, code: "bad_response", message: "state response", extra: [:])
        }
        var usage: RelayUsage?
        if let batches = jsonInt(o["batches"]), let bytes = jsonInt(o["bytes"]), let userBytes = jsonInt(o["user_bytes"]) {
            usage = RelayUsage(batches: batches, bytes: bytes, userBytes: userBytes,
                               lastReceivedAt: (o["last_received_at"] as? String).flatMap(Encoding.parseInstant), checkedAt: Date())
        }
        return State(status: status, contiguousSeq: c, maxSeq: m, grantSeq: jsonInt(o["grant_seq"]), limits: Self.limits(o["limits"]),
                     usage: usage)
    }

    public func purge() async throws {
        _ = try await signed("POST", "/v1/devices/\(signer.deviceID)/purge")
    }
}
