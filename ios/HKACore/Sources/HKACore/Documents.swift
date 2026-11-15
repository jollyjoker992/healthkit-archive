import CryptoKit
import Foundation

public enum DocumentError: Error, Equatable {
    case malformed(String)
}

private func bad(_ what: String) -> DocumentError { .malformed(what) }

/// A JSON integer that is not a boolean (JSONSerialization bridges both to NSNumber).
func jsonInt(_ v: Any?) -> Int? {
    guard let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(), let i = v as? Int,
          Double(i) == n.doubleValue else { return nil }
    return i
}

private func isName(_ v: Any?) -> Bool {
    guard let s = v as? String else { return false }
    return (1...64).contains(s.unicodeScalars.count)
}

/// `hka-invite:` QR content (spec 3.2). The app cannot verify the admin signature (the relay
/// does); it uses the fields to know where to enrol and checks the relay's answer against them.
public struct Invite: Equatable, Hashable {
    public let text: String
    public let server: String
    public let userID: String
    public let userName: String
    public let exp: Date

    public static func parse(_ text: String) throws -> Invite {
        guard text.count <= 1024, text.hasPrefix("hka-invite:") else { throw bad("invite") }
        let parts = text.dropFirst("hka-invite:".count).split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 2, let raw = Encoding.b64uDecode(String(parts[0])),
              let sig = Encoding.b64uDecode(String(parts[1])), sig.count == 64,
              let doc = Encoding.parseObject(raw) else { throw bad("invite") }
        guard jsonInt(doc["v"]) == 1, doc["t"] as? String == "invite" else { throw bad("invite type") }
        guard let server = doc["server"] as? String, isOrigin(server) else { throw bad("invite server") }
        guard let user = doc["user_id"] as? String, KeyID.matches("u", user) else { throw bad("invite user_id") }
        guard isName(doc["user_name"]), let name = doc["user_name"] as? String else { throw bad("invite user_name") }
        guard let expText = doc["exp"] as? String, let exp = Encoding.parseInstant(expText) else { throw bad("invite exp") }
        return Invite(text: text, server: server, userID: user, userName: name, exp: exp)
    }

    public static func isOrigin(_ s: String) -> Bool {
        s.range(of: #"^(https://[a-z0-9.-]+(:[0-9]{1,5})?|http://localhost(:[0-9]{1,5})?)$"#, options: .regularExpression) != nil
    }
}

/// A processor as listed in a grant (spec 4.2), parsed from a scanned card (spec 3.1).
public struct Processor: Codable, Equatable, Hashable, Identifiable {
    public let id: String
    public let name: String
    public let ageRecipient: String
    public let signPub: String

    public var fingerprint: String {
        Fingerprint.processor(signPub: Encoding.b64uDecode(signPub)!, ageRecipient: ageRecipient)
    }

    public init(id: String, name: String, ageRecipient: String, signPub: String) {
        self.id = id; self.name = name; self.ageRecipient = ageRecipient; self.signPub = signPub
    }

    public static func parseCard(_ text: String) throws -> Processor {
        guard text.count <= 1024, text.hasPrefix("hka-card:"),
              let raw = Encoding.b64uDecode(String(text.dropFirst("hka-card:".count))),
              let doc = Encoding.parseObject(raw) else { throw bad("card") }
        guard jsonInt(doc["v"]) == 1, doc["t"] as? String == "card" else { throw bad("card type") }
        guard isName(doc["name"]), let name = doc["name"] as? String else { throw bad("card name") }
        guard let recipient = doc["age_recipient"] as? String, (try? AgeWriter.recipientKey(recipient)) != nil else {
            throw bad("card age_recipient")
        }
        guard let pubText = doc["sign_pub"] as? String, let pub = Encoding.b64uDecode(pubText), pub.count == 65,
              pub.first == 0x04, (try? P256.Signing.PublicKey(x963Representation: pub)) != nil else {
            throw bad("card sign_pub")
        }
        return Processor(id: KeyID.make("p", pub), name: name, ageRecipient: recipient, signPub: pubText)
    }

    var grantEntry: [String: Any] {
        ["id": id, "name": name, "age_recipient": ageRecipient, "sign_pub": signPub]
    }
}

/// A signed document: exact bytes plus signature.
public struct Signed: Codable, Equatable {
    public let doc: Data
    public let sig: String
    public var sha256: String { Encoding.sha256Hex(doc) }
}

public enum Documents {
    public static func enrollBody(invite: Invite, signer: DeviceSigner, deviceName: String, now: Date) throws -> Signed {
        guard isName(deviceName) else { throw bad("device_name") }
        let body = try Encoding.json([
            "invite": invite.text, "device_pub": signer.publicKeyB64u, "device_name": deviceName,
            "ts": Int(now.timeIntervalSince1970),
        ] as [String: Any])
        return Signed(doc: body, sig: try signer.sign("enroll", body))
    }

    public static func grant(userID: String, signer: DeviceSigner, grantSeq: Int, processors: [Processor],
                             now: Date) throws -> Signed {
        guard (1...8).contains(processors.count), Set(processors.map(\.id)).count == processors.count else {
            throw bad("a grant lists 1 to 8 distinct processors")
        }
        let doc = try Encoding.json([
            "v": 1, "t": "grant", "user_id": userID, "device_id": signer.deviceID, "grant_seq": grantSeq,
            "processors": processors.map(\.grantEntry), "created_at": Encoding.instant(now),
        ] as [String: Any])
        return Signed(doc: doc, sig: try signer.sign("grant", doc))
    }

    public static func manifest(userID: String, signer: DeviceSigner, seq: Int, prev: String?, ciphertext: Data,
                                grantSeq: Int, grant: Signed, processors: [Processor], now: Date) throws -> Signed {
        let doc = try Encoding.json([
            "v": 1, "t": "manifest", "user_id": userID, "device_id": signer.deviceID, "seq": seq,
            "prev": prev ?? NSNull(), "ciphertext_sha256": Encoding.sha256Hex(ciphertext), "size": ciphertext.count,
            "recipients": processors.map(\.id), "grant_seq": grantSeq, "grant_sha256": grant.sha256,
            "created_at": Encoding.instant(now),
        ] as [String: Any])
        return Signed(doc: doc, sig: try signer.sign("manifest", doc))
    }

    /// Request signature headers (spec 5).
    public static func requestHeaders(signer: DeviceSigner, origin: String, method: String, target: String,
                                      body: Data = Data(), now: Date) throws -> [String: String] {
        let ts = String(Int(now.timeIntervalSince1970))
        let payload = Data("\(method)\n\(origin)\n\(target)\n\(ts)\n\(Encoding.sha256Hex(body))".utf8)
        return ["HKA-Key": signer.deviceID, "HKA-Timestamp": ts, "HKA-Signature": try signer.sign("request", payload)]
    }
}

/// A processor's statement that it holds this device's batches 1 to `seq` (spec 4.5).
public struct Receipt: Codable, Equatable {
    public let processorID: String
    public let seq: Int
    /// The processor's clock when it signed.
    public let createdAt: Date

    /// Verify a receipt the relay passed on. The key comes from the device's own grants, never from the
    /// relay; the receipt must name this device and claim no more than it sealed. nil if any check fails.
    public static func verified(doc: Data, sig: String, deviceID: String, processors: [String: Processor],
                                lastSealedSeq: Int) -> Receipt? {
        guard doc.count <= 1024, let o = Encoding.parseObject(doc), jsonInt(o["v"]) == 1, o["t"] as? String == "receipt",
              o["device_id"] as? String == deviceID,
              let pid = o["processor_id"] as? String, let processor = processors[pid],
              let seq = jsonInt(o["seq"]), seq >= 1, seq <= lastSealedSeq,
              let created = (o["created_at"] as? String).flatMap(Encoding.parseInstant),
              let pub = Encoding.b64uDecode(processor.signPub),
              let key = try? P256.Signing.PublicKey(x963Representation: pub),
              let raw = Encoding.b64uDecode(sig), raw.count == 64,
              let signature = try? P256.Signing.ECDSASignature(rawRepresentation: raw),
              key.isValidSignature(signature, for: Data("hka-v1/receipt\n".utf8) + doc) else { return nil }
        return Receipt(processorID: pid, seq: seq, createdAt: created)
    }
}
