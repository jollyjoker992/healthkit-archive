import CryptoKit
import Foundation

/// Strict encodings from spec/README.md section 1.
public enum Encoding {
    public static func b64u(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    /// Canonical base64url without padding, or nil.
    public static func b64uDecode(_ text: String) -> Data? {
        guard text.utf8.allSatisfy({ c in
            (c >= 0x41 && c <= 0x5A) || (c >= 0x61 && c <= 0x7A) || (c >= 0x30 && c <= 0x39) || c == 0x2D || c == 0x5F
        }), text.count % 4 != 1 else { return nil }
        var std = text.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        std += String(repeating: "=", count: (4 - std.count % 4) % 4)
        guard let data = Data(base64Encoded: std), b64u(data) == text else { return nil }
        return data
    }

    /// Standard base64 without padding, as the age format uses.
    public static func b64Std(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "=", with: "")
    }

    public static func hex(_ data: Data) -> String {
        data.map { String(format: "%02x", $0) }.joined()
    }

    public static func isHex(_ text: String, bytes: Int? = nil) -> Bool {
        guard text.count % 2 == 0, text.utf8.allSatisfy({ ($0 >= 0x30 && $0 <= 0x39) || ($0 >= 0x61 && $0 <= 0x66) }) else {
            return false
        }
        return bytes == nil || text.count == bytes! * 2
    }

    public static func sha256(_ data: Data) -> Data { Data(SHA256.hash(data: data)) }
    public static func sha256Hex(_ data: Data) -> String { hex(sha256(data)) }

    // MARK: instants

    /// UTC, `Z`, exactly three fractional digits, truncated (not rounded).
    public static func instant(_ date: Date) -> String {
        let ms = Int64((date.timeIntervalSince1970 * 1000).rounded(.down))
        let seconds = ms >= 0 ? ms / 1000 : (ms - 999) / 1000
        let millis = ms - seconds * 1000
        var tm = tm()
        var t = time_t(seconds)
        gmtime_r(&t, &tm)
        return String(format: "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ",
                      Int(tm.tm_year) + 1900, Int(tm.tm_mon) + 1, Int(tm.tm_mday),
                      Int(tm.tm_hour), Int(tm.tm_min), Int(tm.tm_sec), Int(millis))
    }

    public static func parseInstant(_ text: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        guard text.count == 24, text.hasSuffix("Z"), let date = f.date(from: text), instant(date) == text else { return nil }
        return date
    }

    // MARK: JSON

    /// Compact JSON with sorted keys and unescaped slashes. Documents are signed over these exact bytes.
    /// Non-finite numbers would make JSONSerialization raise an Objective-C exception, so they are refused.
    public static func json(_ object: Any) throws -> Data {
        guard isFinite(object), JSONSerialization.isValidJSONObject(object) || object is String || object is NSNumber else {
            throw DocumentError.malformed("value cannot be encoded as JSON")
        }
        return try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes, .fragmentsAllowed])
    }

    static func isFinite(_ v: Any) -> Bool {
        switch v {
        case let d as Double: return d.isFinite
        case let f as Float: return f.isFinite
        case let a as [Any]: return a.allSatisfy(isFinite)
        case let o as [String: Any]: return o.values.allSatisfy(isFinite)
        default: return true
        }
    }

    /// Replace non-finite numbers with null (spec/records.md: such values are omitted or null).
    public static func sanitize(_ record: [String: Any]) -> [String: Any] {
        record.mapValues(sanitizeValue)
    }

    static func sanitizeValue(_ v: Any) -> Any {
        switch v {
        case let n as NSNumber where CFGetTypeID(n) == CFBooleanGetTypeID(): return n
        case let n as NSNumber where !CFNumberIsFloatType(n): return n
        case let d as Double: return d.isFinite ? d : NSNull()
        case let a as [Any]: return a.map(sanitizeValue)
        case let o as [String: Any]: return o.mapValues(sanitizeValue)
        default: return v
        }
    }

    public static func parseObject(_ data: Data) -> [String: Any]? {
        guard String(data: data, encoding: .utf8) != nil else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }
}

/// Fingerprints shown to people (spec 2.5).
public enum Fingerprint {
    public static func of(_ data: Data) -> String {
        let hex = Encoding.hex(Encoding.sha256(data).prefix(10)).uppercased()
        return stride(from: 0, to: 20, by: 4).map { i in
            String(hex[hex.index(hex.startIndex, offsetBy: i)..<hex.index(hex.startIndex, offsetBy: i + 4)])
        }.joined(separator: "-")
    }

    public static func processor(signPub: Data, ageRecipient: String) -> String {
        of(signPub + Data(ageRecipient.utf8))
    }
}

public enum KeyID {
    public static func make(_ prefix: String, _ pub: Data) -> String {
        "\(prefix)_" + Encoding.hex(Encoding.sha256(pub).prefix(16))
    }

    public static func matches(_ prefix: String, _ id: String) -> Bool {
        id.count == 34 && id.hasPrefix("\(prefix)_") && Encoding.isHex(String(id.dropFirst(2)), bytes: 16)
    }
}
