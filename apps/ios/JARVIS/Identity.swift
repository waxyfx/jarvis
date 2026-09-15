//
//  Identity.swift
//  The phone's own key, and the two signatures the backend asks for.
//
//  Ed25519, generated on the device, private half never leaving it. The public
//  half is enrolled once at pairing and the backend recognises the phone by it
//  from then on.
//
//  NOT THE SECURE ENCLAVE, and the reason is worth stating rather than
//  discovering: the Enclave does P-256 only, and this protocol is Ed25519
//  everywhere — the signing domains are hashed into every signature the Windows
//  agent already produces, and changing the curve for one device would mean
//  changing it for all of them. So the key lives in the Keychain with
//  `kSecAttrAccessibleWhenUnlockedThisDeviceOnly`: it does not sync to iCloud,
//  it does not leave in a backup, and it is unreadable while the phone is
//  locked.
//
//  NEVER BUILT. There is no Mac in this project. Every line here is written
//  against the backend's real wire format, and none of it has been through a
//  compiler. See apps/ios/README.md.
//

import CryptoKit
import Foundation

/// Bytes the backend expects to have been signed. Both domains and the
/// separator are copied from `atlas_shared/auth.py`; a mismatch here shows up
/// only as an unexplained signature failure, which is why they are together in
/// one place on each side.
enum SigningInput {
    private static let separator = Data([0x1f])
    private static let pairingDomain = Data("atlas.pair.proof.v1".utf8)
    private static let challengeDomain = Data("atlas.auth.challenge.v1".utf8)

    /// Binds the pairing code to the key being enrolled, so an intercepted code
    /// cannot register a different one.
    static func pairing(code: String, publicKey: Data) -> Data {
        join([pairingDomain, Data(normalise(code).utf8), publicKey])
    }

    static func challenge(deviceID: String, nonce: Data) -> Data {
        join([challengeDomain, Data(deviceID.utf8), nonce])
    }

    /// Accepts what a person typed: separators and case are not part of a code.
    static func normalise(_ code: String) -> String {
        code.uppercased().filter { $0.isLetter || $0.isNumber }
    }

    private static func join(_ parts: [Data]) -> Data {
        var result = Data()
        for (index, part) in parts.enumerated() {
            if index > 0 { result.append(separator) }
            result.append(part)
        }
        return result
    }
}

/// URL-safe base64 without padding, which is what the wire uses throughout.
extension Data {
    var base64URL: String {
        base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    init?(base64URL string: String) {
        var padded = string
            .replacingOccurrences(of: "-", with: "+")
            .replacingOccurrences(of: "_", with: "/")
        while padded.count % 4 != 0 { padded.append("=") }
        guard let data = Data(base64Encoded: padded) else { return nil }
        self = data
    }
}

/// The phone's key pair, kept in the Keychain.
struct DeviceIdentity {
    let deviceID: String
    let privateKey: Curve25519.Signing.PrivateKey
    /// Pinned at pairing. A command signed by any other key is refused, which
    /// is what stops a different server from talking to this phone.
    let serverPublicKey: Data

    var publicKey: Data { privateKey.publicKey.rawRepresentation }

    func sign(_ message: Data) throws -> String {
        try privateKey.signature(for: message).base64URL
    }
}

enum IdentityError: Error, LocalizedError {
    case keychain(OSStatus)
    case malformed

    var errorDescription: String? {
        switch self {
        case .keychain(let status): return "the keychain refused: \(status)"
        case .malformed: return "the stored identity could not be read"
        }
    }
}

/// Where the identity lives between launches.
///
/// One item, one account name. Deleting it is how the phone unpairs — the
/// backend keeps its own record until the device is revoked there, which is
/// deliberate: a phone that has lost its key should not be able to erase the
/// audit trail of what it did.
struct IdentityStore {
    private let service = "com.jarvis.identity"
    private let account = "device"

    func load() throws -> DeviceIdentity? {
        var query = baseQuery()
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data else {
            throw IdentityError.keychain(status)
        }

        guard let stored = try? JSONDecoder().decode(StoredIdentity.self, from: data),
              let secret = Data(base64URL: stored.privateKey),
              let server = Data(base64URL: stored.serverPublicKey),
              let key = try? Curve25519.Signing.PrivateKey(rawRepresentation: secret)
        else { throw IdentityError.malformed }

        return DeviceIdentity(deviceID: stored.deviceID, privateKey: key, serverPublicKey: server)
    }

    func save(_ identity: DeviceIdentity) throws {
        let stored = StoredIdentity(
            deviceID: identity.deviceID,
            privateKey: identity.privateKey.rawRepresentation.base64URL,
            serverPublicKey: identity.serverPublicKey.base64URL
        )
        let data = try JSONEncoder().encode(stored)

        var query = baseQuery()
        SecItemDelete(query as CFDictionary)
        query[kSecValueData as String] = data
        // Not `kSecAttrAccessibleAfterFirstUnlock`: this key authorises actions
        // on the owner's computer, and there is no reason for it to be readable
        // while the phone is sitting locked on a table.
        query[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly

        let status = SecItemAdd(query as CFDictionary, nil)
        guard status == errSecSuccess else { throw IdentityError.keychain(status) }
    }

    func clear() {
        SecItemDelete(baseQuery() as CFDictionary)
    }

    private func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            // Never synchronised. A key on another device is another device.
            kSecAttrSynchronizable as String: false,
        ]
    }

    private struct StoredIdentity: Codable {
        let deviceID: String
        let privateKey: String
        let serverPublicKey: String
    }
}
