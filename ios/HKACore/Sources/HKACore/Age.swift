import CryptoKit
import Foundation

/// Bech32 (BIP-173) decoding for `age1…` recipients.
public enum Bech32 {
    static let charset = Array("qpzry9x8gf2tvdw0s3jn54khce6mua7l")

    static func polymod(_ values: [UInt8]) -> UInt32 {
        let gen: [UInt32] = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
        var chk: UInt32 = 1
        for v in values {
            let top = chk >> 25
            chk = (chk & 0x1FFFFFF) << 5 ^ UInt32(v)
            for i in 0..<5 where (top >> i) & 1 == 1 { chk ^= gen[i] }
        }
        return chk
    }

    /// Returns the data part of a lowercase Bech32 string with the expected human-readable part.
    public static func decode(_ text: String, hrp: String) -> Data? {
        guard text == text.lowercased(), text.hasPrefix(hrp + "1"), text.count <= 90 else { return nil }
        let dataPart = Array(text.dropFirst(hrp.count + 1))
        guard dataPart.count >= 6 else { return nil }
        var values: [UInt8] = []
        for c in dataPart {
            guard let i = charset.firstIndex(of: c) else { return nil }
            values.append(UInt8(i))
        }
        let expanded = hrp.utf8.map { $0 >> 5 } + [0] + hrp.utf8.map { $0 & 31 }
        guard polymod(expanded + values) == 1 else { return nil }
        var acc: UInt32 = 0, bits = 0
        var out = Data()
        for v in values.dropLast(6) {
            acc = (acc << 5) | UInt32(v)
            bits += 5
            if bits >= 8 {
                bits -= 8
                out.append(UInt8((acc >> UInt32(bits)) & 0xFF))
            }
        }
        guard bits < 5, (acc & ((1 << UInt32(bits)) - 1)) == 0 else { return nil }
        return out
    }
}

public enum AgeError: Error, Equatable {
    case badRecipient(String)
    case lowOrderPoint
}

/// Encrypt-only writer for the age v1 format with X25519 recipients (https://c2sp.org/age).
/// Output is a binary age file that the reference `age` tool and `pyrage` decrypt.
public enum AgeWriter {
    static let chunkSize = 64 * 1024

    public static func recipientKey(_ recipient: String) throws -> Curve25519.KeyAgreement.PublicKey {
        guard let raw = Bech32.decode(recipient, hrp: "age"), raw.count == 32,
              let key = try? Curve25519.KeyAgreement.PublicKey(rawRepresentation: raw) else {
            throw AgeError.badRecipient(recipient)
        }
        return key
    }

    public static func encrypt(_ plaintext: Data, to recipients: [String]) throws -> Data {
        precondition(!recipients.isEmpty, "age needs at least one recipient")
        let keys = try recipients.map(recipientKey)
        let fileKey = SymmetricKey(size: .bits128).withUnsafeBytes { Data($0) }

        var header = "age-encryption.org/v1\n"
        for key in keys {
            let ephemeral = Curve25519.KeyAgreement.PrivateKey()
            let share = ephemeral.publicKey.rawRepresentation
            let shared = try ephemeral.sharedSecretFromKeyAgreement(with: key)
            let sharedBytes = shared.withUnsafeBytes { Data($0) }
            if sharedBytes.allSatisfy({ $0 == 0 }) { throw AgeError.lowOrderPoint }
            let wrapKey = HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: sharedBytes),
                                                 salt: share + key.rawRepresentation,
                                                 info: Data("age-encryption.org/v1/X25519".utf8),
                                                 outputByteCount: 32)
            let sealed = try ChaChaPoly.seal(fileKey, using: wrapKey, nonce: ChaChaPoly.Nonce(data: Data(count: 12)))
            header += "-> X25519 \(Encoding.b64Std(share))\n" + wrap64(Encoding.b64Std(sealed.ciphertext + sealed.tag))
        }
        header += "---"
        let macKey = HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: fileKey), salt: Data(),
                                            info: Data("header".utf8), outputByteCount: 32)
        let mac = HMAC<SHA256>.authenticationCode(for: Data(header.utf8), using: macKey)
        var out = Data((header + " " + Encoding.b64Std(Data(mac)) + "\n").utf8)

        let nonce = SymmetricKey(size: .bits128).withUnsafeBytes { Data($0) }
        out += nonce
        let payloadKey = HKDF<SHA256>.deriveKey(inputKeyMaterial: SymmetricKey(data: fileKey), salt: nonce,
                                                info: Data("payload".utf8), outputByteCount: 32)
        var counter: UInt64 = 0
        var offset = 0
        repeat {
            let end = min(offset + chunkSize, plaintext.count)
            let last = end == plaintext.count
            var chunkNonce = Data(count: 3)  // 11-byte big-endian counter: 3 zero bytes + 8 bytes
            withUnsafeBytes(of: counter.bigEndian) { chunkNonce.append(contentsOf: $0) }
            chunkNonce.append(last ? 1 : 0)
            let sealed = try ChaChaPoly.seal(plaintext.subdata(in: offset..<end), using: payloadKey,
                                             nonce: ChaChaPoly.Nonce(data: chunkNonce))
            out += sealed.ciphertext + sealed.tag
            counter += 1
            offset = end
            if last { break }
        } while true
        return out
    }

    /// Wrap a stanza body at 64 columns; the final line is always shorter than 64 (possibly empty).
    static func wrap64(_ text: String) -> String {
        var lines: [String] = []
        var rest = Substring(text)
        while rest.count >= 64 {
            lines.append(String(rest.prefix(64)))
            rest = rest.dropFirst(64)
        }
        lines.append(String(rest))
        return lines.joined(separator: "\n") + "\n"
    }
}
