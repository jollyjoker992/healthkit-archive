import CryptoKit
import Foundation
import XCTest
@testable import HKACore

let repoRoot = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()

func vectors(_ name: String) throws -> [String: Any] {
    let data = try Data(contentsOf: repoRoot.appendingPathComponent("spec/vectors/\(name)"))
    return try JSONSerialization.jsonObject(with: data) as! [String: Any]
}

/// Run a tool and return stdout, or nil if it is not installed.
func runTool(_ tool: String, _ args: [String], input: Data? = nil) throws -> Data? {
    let candidates = [tool, NSHomeDirectory() + "/go/bin/" + tool, "/usr/bin/" + tool, "/opt/homebrew/bin/" + tool]
    guard let path = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else { return nil }
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    let out = Pipe(), inp = Pipe(), err = Pipe()
    p.standardOutput = out; p.standardInput = inp; p.standardError = err
    try p.run()
    if let input { inp.fileHandleForWriting.write(input) }
    try inp.fileHandleForWriting.close()
    let data = out.fileHandleForReading.readDataToEndOfFile()
    p.waitUntilExit()
    if p.terminationStatus != 0 {
        throw NSError(domain: tool, code: Int(p.terminationStatus),
                      userInfo: [NSLocalizedDescriptionKey: String(data: err.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""])
    }
    return data
}

final class VectorTests: XCTestCase {
    func testEncodings() throws {
        let v = try vectors("encodings.json")
        for c in v["valid"] as! [[String: String]] {
            XCTAssertEqual(Encoding.hex(Encoding.b64uDecode(c["b64u"]!)!), c["hex"])
            XCTAssertEqual(Encoding.b64u(Encoding.b64uDecode(c["b64u"]!)!), c["b64u"])
        }
        for c in v["invalid_b64u"] as! [[String: String]] { XCTAssertNil(Encoding.b64uDecode(c["input"]!), c["note"]!) }
        for c in v["invalid_hex"] as! [[String: String]] { XCTAssertFalse(Encoding.isHex(c["input"]!), c["note"]!) }
    }

    func testKeysIdsFingerprints() throws {
        for k in try vectors("keys.json")["keys"] as! [[String: String]] {
            let scalar = Data(hexString: k["private_scalar_hex"]!)
            let signer = SoftwareSigner(key: try P256.Signing.PrivateKey(rawRepresentation: scalar))
            XCTAssertEqual(signer.publicKeyB64u, k["pub"])
            let prefix = String(k["id"]!.prefix(1))
            XCTAssertEqual(KeyID.make(prefix, signer.publicKey), k["id"])
            let fp = k["age_recipient"].map { Fingerprint.processor(signPub: signer.publicKey, ageRecipient: $0) }
                ?? Fingerprint.of(signer.publicKey)
            XCTAssertEqual(fp, k["fingerprint"])
        }
    }

    func testSignaturesVerifyWithTheSpecRules() throws {
        let v = try vectors("signatures.json")
        let pub = try P256.Signing.PublicKey(x963Representation: Encoding.b64uDecode(v["pub"] as! String)!)
        for c in v["cases"] as! [[String: Any]] {
            let input = Data("hka-v1/\(c["purpose"] as! String)\n".utf8) + Encoding.b64uDecode(c["payload"] as! String)!
            let sig = try P256.Signing.ECDSASignature(rawRepresentation: Encoding.b64uDecode(c["sig"] as! String)!)
            // CryptoKit rejects high-S like some verifiers; the relay and processors accept it (spec 2.1).
            if (c["note"] as! String).contains("high-S") { continue }
            XCTAssertEqual(pub.isValidSignature(sig, for: input), c["valid"] as! Bool, c["note"] as! String)
        }
    }

    func testOwnSignaturesAreRawAndVerify() throws {
        let s = SoftwareSigner()
        let sig = Encoding.b64uDecode(try s.sign("grant", Data("x".utf8)))!
        XCTAssertEqual(sig.count, 64)
        XCTAssertTrue(s.key.publicKey.isValidSignature(try .init(rawRepresentation: sig), for: Data("hka-v1/grant\nx".utf8)))
    }

    func testRequestHeadersMatchTheVectors() throws {
        let v = try vectors("requests.json")
        let keys = Dictionary(uniqueKeysWithValues: (try vectors("keys.json")["keys"] as! [[String: String]]).map { ($0["id"]!, $0) })
        for c in v["cases"] as! [[String: Any]] where (c["key_id"] as! String).hasPrefix("d_") {
            let k = keys[c["key_id"] as! String]!
            let signer = SoftwareSigner(key: try P256.Signing.PrivateKey(rawRepresentation: Data(hexString: k["private_scalar_hex"]!)))
            let headers = try Documents.requestHeaders(signer: signer, origin: c["origin"] as! String, method: c["method"] as! String,
                                                       target: c["target"] as! String,
                                                       now: Date(timeIntervalSince1970: TimeInterval(c["timestamp"] as! Int)))
            XCTAssertEqual(headers["HKA-Key"], c["key_id"] as? String)
            XCTAssertEqual(headers["HKA-Timestamp"], String(c["timestamp"] as! Int))
            // ECDSA signatures are randomized in CryptoKit: verify instead of comparing bytes.
            let payload = Data((c["payload"] as! String).utf8)
            let sig = try P256.Signing.ECDSASignature(rawRepresentation: Encoding.b64uDecode(headers["HKA-Signature"]!)!)
            XCTAssertTrue(signer.key.publicKey.isValidSignature(sig, for: Data("hka-v1/request\n".utf8) + payload))
        }
    }

    func testCardAndInviteParsing() throws {
        let d = try vectors("documents.json")
        let card = d["card"] as! [String: String]
        let p = try Processor.parseCard(card["text"]!)
        XCTAssertEqual(p.id, card["processor_id"])
        XCTAssertEqual(p.fingerprint, card["fingerprint"])
        let invite = try Invite.parse((d["invite"] as! [String: String])["text"]!)
        XCTAssertEqual(invite.server, d["server"] as? String)
        XCTAssertEqual(invite.userName, "Mẹ")
        XCTAssertThrowsError(try Processor.parseCard("hka-card:" + Encoding.b64u(Data(#"{"v":true,"t":"card"}"#.utf8))))
    }

    func testInstants() {
        XCTAssertEqual(Encoding.instant(Date(timeIntervalSince1970: 1794556800.1239)), "2026-11-13T08:00:00.123Z")
        XCTAssertEqual(Encoding.instant(Date(timeIntervalSince1970: -0.0005)), "1969-12-31T23:59:59.999Z")
        XCTAssertNotNil(Encoding.parseInstant("2026-11-13T08:00:00.123Z"))
        XCTAssertNil(Encoding.parseInstant("2026-11-13T08:00:00Z"))
    }

    func testBech32MatchesTheVectors() throws {
        for a in try vectors("keys.json")["age"] as! [[String: String]] {
            XCTAssertEqual(Bech32.decode(a["recipient"]!, hrp: "age")?.count, 32)
        }
        XCTAssertNil(Bech32.decode("age1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq", hrp: "age"))
    }
}

final class AgeTests: XCTestCase {
    /// The writer's output must decrypt with the reference tool and pyrage (spec section 8 of the design).
    func testDecryptsWithReferenceTools() throws {
        let ids = try vectors("keys.json")["age"] as! [[String: String]]
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmp) }
        let idFile = tmp.appendingPathComponent("id.txt")
        try (ids[1]["identity"]! + "\n").write(to: idFile, atomically: true, encoding: .utf8)
        var checked = 0
        for size in [0, 1, 65535, 65536, 65537, 131072, 200_001] {
            var plain = Data(count: size)
            plain.withUnsafeMutableBytes { _ = SecRandomCopyBytes(kSecRandomDefault, size, $0.baseAddress!) }
            let ct = try AgeWriter.encrypt(plain, to: ids.map { $0["recipient"]! })
            if let out = try runTool("age", ["--decrypt", "-i", idFile.path], input: ct) {
                XCTAssertEqual(out, plain, "age, size \(size)")
                checked += 1
            }
            let python = repoRoot.appendingPathComponent(".venv/bin/python").path
            if FileManager.default.isExecutableFile(atPath: python) {
                let script = "import sys,pyrage;i=pyrage.x25519.Identity.from_str(sys.argv[1]);"
                    + "sys.stdout.buffer.write(pyrage.decrypt(sys.stdin.buffer.read(),[i]))"
                let out = try runTool(python, ["-I", "-c", script, ids[0]["identity"]!], input: ct)
                XCTAssertEqual(out, plain, "pyrage, size \(size)")
                checked += 1
            }
        }
        if checked == 0 { throw XCTSkip("neither the age CLI nor the repo's .venv with pyrage is installed") }
    }

    func testRejectsBadRecipients() {
        XCTAssertThrowsError(try AgeWriter.encrypt(Data(), to: ["age1notarecipient"]))
    }

    func testGzipRoundTripsWithSystemGunzip() throws {
        let text = Data(String(repeating: "{\"k\":\"sample\",\"value\":72}\n", count: 5000).utf8)
        let gz = try Gzip.compress(text)
        XCTAssertEqual(try runTool("gunzip", ["-c"], input: gz), text)
        XCTAssertEqual(Gzip.crc32(Data("123456789".utf8)), 0xCBF43926)
    }
}

extension Data {
    init(hexString: String) {
        self.init()
        var i = hexString.startIndex
        while i < hexString.endIndex {
            let j = hexString.index(i, offsetBy: 2)
            append(UInt8(hexString[i..<j], radix: 16)!)
            i = j
        }
    }
}
