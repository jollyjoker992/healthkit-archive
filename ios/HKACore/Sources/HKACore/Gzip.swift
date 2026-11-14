import Foundation

/// RFC 1952 gzip around Apple's raw DEFLATE (NSData `.zlib` produces RFC 1951 data).
public enum Gzip {
    public static func compress(_ data: Data) throws -> Data {
        let deflated = try (data as NSData).compressed(using: .zlib) as Data
        var out = Data([0x1F, 0x8B, 0x08, 0x00, 0, 0, 0, 0, 0x00, 0xFF])  // no flags, mtime 0, OS unknown
        out += deflated
        appendLE(&out, crc32(data))
        appendLE(&out, UInt32(truncatingIfNeeded: data.count))
        return out
    }

    static func appendLE(_ out: inout Data, _ v: UInt32) {
        withUnsafeBytes(of: v.littleEndian) { out.append(contentsOf: $0) }
    }

    static let table: [UInt32] = (0..<256).map { n -> UInt32 in
        var c = UInt32(n)
        for _ in 0..<8 { c = (c & 1) != 0 ? 0xEDB88320 ^ (c >> 1) : c >> 1 }
        return c
    }

    public static func crc32(_ data: Data) -> UInt32 {
        var c: UInt32 = 0xFFFFFFFF
        data.withUnsafeBytes { buf in
            for b in buf { c = table[Int((c ^ UInt32(b)) & 0xFF)] ^ (c >> 8) }
        }
        return c ^ 0xFFFFFFFF
    }
}
