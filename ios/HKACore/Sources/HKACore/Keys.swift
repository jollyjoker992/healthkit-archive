import CryptoKit
import Foundation
import Security

/// Signing with the purposes of spec 2.2. Signatures are raw r||s, b64u.
public protocol DeviceSigner {
    /// SEC1 uncompressed public key, 65 bytes.
    var publicKey: Data { get }
    func signRaw(_ data: Data) throws -> Data
}

public extension DeviceSigner {
    var deviceID: String { KeyID.make("d", publicKey) }
    var fingerprint: String { Fingerprint.of(publicKey) }
    var publicKeyB64u: String { Encoding.b64u(publicKey) }

    func sign(_ purpose: String, _ payload: Data) throws -> String {
        let input = Data("hka-v1/\(purpose)\n".utf8) + payload
        return Encoding.b64u(try signRaw(input))
    }
}

/// A software key: tests, the macOS test device, and the simulator.
public struct SoftwareSigner: DeviceSigner {
    public let key: P256.Signing.PrivateKey
    public init(key: P256.Signing.PrivateKey = .init()) { self.key = key }
    public var publicKey: Data { key.publicKey.x963Representation }
    public func signRaw(_ data: Data) throws -> Data { try key.signature(for: data).rawRepresentation }
}

public enum KeychainError: Error {
    case status(OSStatus)
    /// errSecInteractionNotAllowed: the device is locked. Retry later; never regenerate the key.
    case locked
}

/// The device key in the Secure Enclave (spec 4.3 device rules). Its opaque handle is kept in
/// the keychain as AfterFirstUnlockThisDeviceOnly so background uploads can sign while locked.
public struct SecureEnclaveSigner: DeviceSigner {
    static let service = "hka.device-key"
    let key: SecureEnclave.P256.Signing.PrivateKey

    public var publicKey: Data { key.publicKey.x963Representation }
    public func signRaw(_ data: Data) throws -> Data { try key.signature(for: data).rawRepresentation }

    public static var isAvailable: Bool { SecureEnclave.isAvailable }

    public static func create() throws -> SecureEnclaveSigner {
        try delete()
        var error: Unmanaged<CFError>?
        guard let access = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
                                                           .privateKeyUsage, &error) else {
            throw error!.takeRetainedValue() as Error
        }
        let key = try SecureEnclave.P256.Signing.PrivateKey(accessControl: access)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
            kSecValueData as String: key.dataRepresentation,
        ]
        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else { throw KeychainError.status(status) }
        return SecureEnclaveSigner(key: key)
    }

    /// The existing key, nil if there is none. Throws `.locked` while the device is locked.
    public static func load() throws -> SecureEnclaveSigner? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecReturnData as String: true,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        switch status {
        case errSecSuccess:
            guard let data = item as? Data else { return nil }
            return SecureEnclaveSigner(key: try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: data))
        case errSecItemNotFound: return nil
        case errSecInteractionNotAllowed: throw KeychainError.locked
        default: throw KeychainError.status(status)
        }
    }

    public static func delete() throws {
        let status = SecItemDelete([kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: service] as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw KeychainError.status(status) }
    }
}
