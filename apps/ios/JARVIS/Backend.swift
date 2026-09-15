//
//  Backend.swift
//  Everything this app says to JARVIS, and nothing it does not.
//
//  Five calls. Pairing once, a token when the old one expires, one request to
//  draw the screen, one to ask a question, one to answer a confirmation. There
//  is deliberately no general "call any endpoint" method: the surface a phone
//  needs is small and naming it is what keeps it small.
//
//  NEVER BUILT — see apps/ios/README.md.
//

import CryptoKit
import Foundation

struct Machine: Decodable, Identifiable {
    let deviceID: String
    let name: String
    let connected: Bool
    let lastSeenAt: Date?
    let cpuPct: Double?
    let ramUsedPct: Double?
    let uptimeS: Int?
    let telemetryAgeS: Int?
    let telemetryStale: Bool

    var id: String { deviceID }

    enum CodingKeys: String, CodingKey {
        case deviceID = "device_id"
        case name
        case connected
        case lastSeenAt = "last_seen_at"
        case cpuPct = "cpu_pct"
        case ramUsedPct = "ram_used_pct"
        case uptimeS = "uptime_s"
        case telemetryAgeS = "telemetry_age_s"
        case telemetryStale = "telemetry_stale"
    }
}

/// What `/v1/overview` reports as waiting.
struct PendingAction: Decodable, Identifiable {
    let id: String
    let tool: String
    let risk: String
    let requestedAt: Date

    enum CodingKeys: String, CodingKey {
        case id
        case tool
        case risk
        case requestedAt = "requested_at"
    }
}

struct Overview: Decodable {
    let at: Date
    let machines: [Machine]
    let pending: [PendingAction]
    /// `nil` means nothing was recorded, which is not the same as no time
    /// worked. The backend is careful about that distinction and so is the view.
    let activeMinutesToday: Int?

    enum CodingKeys: String, CodingKey {
        case at, machines, pending
        case activeMinutesToday = "active_minutes_today"
    }
}

struct Answer: Decodable {
    let reply: String
    let executed: [ToolCallSummary]
    /// A different shape from `/v1/overview`'s pending list, and deliberately a
    /// different type: this one carries the decision and the result, that one
    /// carries when it was asked for.
    let pendingConfirmation: [ToolCallSummary]
    let stoppedBecause: String

    enum CodingKeys: String, CodingKey {
        case reply, executed
        case pendingConfirmation = "pending_confirmation"
        case stoppedBecause = "stopped_because"
    }
}

/// One tool call, as the assistant endpoint reports it.
struct ToolCallSummary: Decodable, Identifiable {
    let id: String
    let tool: String
    let risk: String
    let decision: String
    let status: String
    let refusal: String?

    enum CodingKeys: String, CodingKey {
        case id, tool, risk, decision, status, refusal
    }
}

enum BackendError: Error, LocalizedError {
    case notPaired
    case http(Int, String)
    case transport(Error)
    case malformed

    var errorDescription: String? {
        switch self {
        case .notPaired:
            return "This phone is not paired yet."
        case .http(401, _):
            return "JARVIS did not accept this phone. It may have been revoked."
        case .http(let code, let body):
            return body.isEmpty ? "JARVIS answered \(code)." : body
        case .transport:
            // The underlying message is usually about sockets, which helps
            // nobody standing on a platform with one bar of signal.
            return "Could not reach JARVIS."
        case .malformed:
            return "JARVIS answered something this app could not read."
        }
    }
}

/// Talks to one backend, as one device.
actor Backend {
    private let baseURL: URL
    private let identity: DeviceIdentity
    private let session: URLSession

    /// Kept in memory only. It is short-lived by design, and a token written to
    /// disk is a token that outlives the reason it was issued.
    private var token: String?
    private var tokenObtained: Date?

    /// Re-authenticate before the backend would refuse. Its tokens last fifteen
    /// minutes; ten keeps a slow request from expiring mid-flight.
    private static let tokenLifetime: TimeInterval = 10 * 60

    init(baseURL: URL, identity: DeviceIdentity, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.identity = identity
        self.session = session
    }

    // MARK: - What the app actually asks for

    func overview() async throws -> Overview {
        try await authorised(path: "/v1/overview", method: "GET")
    }

    func ask(_ text: String, language: String = "ru") async throws -> Answer {
        try await authorised(
            path: "/v1/assistant/message",
            method: "POST",
            body: ["text": text, "language": language]
        )
    }

    /// Say yes to something the Policy Engine held back.
    ///
    /// The phone confirms; it never decides. What may be confirmed at all, and
    /// what happens next, are the backend's business — this only carries the
    /// answer.
    func confirm(callID: String) async throws -> ToolCallSummary {
        try await authorised(path: "/v1/tools/calls/\(callID)/confirm", method: "POST")
    }

    // MARK: - Authentication

    private func accessToken() async throws -> String {
        if let token, let obtained = tokenObtained,
           Date().timeIntervalSince(obtained) < Self.tokenLifetime {
            return token
        }

        struct Challenge: Decodable { let nonce: String }
        struct Token: Decodable {
            let accessToken: String
            enum CodingKeys: String, CodingKey { case accessToken = "access_token" }
        }

        let challenge: Challenge = try await plain(
            path: "/v1/auth/challenge",
            method: "POST",
            body: ["device_id": identity.deviceID]
        )
        guard let nonce = Data(base64URL: challenge.nonce) else { throw BackendError.malformed }

        let signature = try identity.sign(
            SigningInput.challenge(deviceID: identity.deviceID, nonce: nonce)
        )
        let issued: Token = try await plain(
            path: "/v1/auth/token",
            method: "POST",
            body: [
                "device_id": identity.deviceID,
                "nonce": challenge.nonce,
                "signature": signature,
            ]
        )

        token = issued.accessToken
        tokenObtained = Date()
        return issued.accessToken
    }

    // MARK: - Requests

    private func authorised<T: Decodable>(
        path: String, method: String, body: [String: Any]? = nil
    ) async throws -> T {
        let bearer = try await accessToken()
        do {
            return try await plain(path: path, method: method, body: body, bearer: bearer)
        } catch BackendError.http(401, _) {
            // The token was rejected. One retry with a fresh one, because the
            // usual cause is a clock or a restart rather than a revocation —
            // and if it really was revoked, the second 401 is reported.
            token = nil
            let renewed = try await accessToken()
            return try await plain(path: path, method: method, body: body, bearer: renewed)
        }
    }

    private func plain<T: Decodable>(
        path: String, method: String, body: [String: Any]? = nil, bearer: String? = nil
    ) async throws -> T {
        var request = URLRequest(url: baseURL.appendingPathComponent(path))
        request.httpMethod = method
        request.timeoutInterval = 120  // a spoken turn can take a while
        if let bearer { request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization") }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw BackendError.transport(error)
        }

        guard let http = response as? HTTPURLResponse else { throw BackendError.malformed }
        guard (200..<300).contains(http.statusCode) else {
            throw BackendError.http(http.statusCode, Self.detail(from: data))
        }

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601WithFractionalSeconds
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw BackendError.malformed
        }
    }

    /// The backend's error bodies carry a `detail`. Anything else is not worth
    /// showing raw to someone holding a phone.
    private static func detail(from data: Data) -> String {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return "" }
        return object["detail"] as? String ?? ""
    }
}

extension JSONDecoder.DateDecodingStrategy {
    /// The backend sends ISO 8601 with fractional seconds; `.iso8601` alone
    /// rejects those, and the failure looks like a malformed response.
    static var iso8601WithFractionalSeconds: JSONDecoder.DateDecodingStrategy {
        .custom { decoder in
            let container = try decoder.singleValueContainer()
            let text = try container.decode(String.self)
            let formatter = ISO8601DateFormatter()

            // Postgres timestamps come back with fractional seconds and
            // sometimes without, and `.iso8601` alone rejects the first kind —
            // which surfaces as "JARVIS answered something this app could not
            // read", pointing at entirely the wrong thing.
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = formatter.date(from: text) { return date }
            formatter.formatOptions = [.withInternetDateTime]
            if let date = formatter.date(from: text) { return date }

            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "not an ISO 8601 date: \(text)"
            )
        }
    }
}

/// Enrolling this phone. Separate from `Backend` because it is the one thing
/// that happens before there is an identity to talk with.
enum Pairing {
    struct Result: Decodable {
        let deviceID: String
        let serverPublicKey: String

        enum CodingKeys: String, CodingKey {
            case deviceID = "device_id"
            case serverPublicKey = "server_public_key"
        }
    }

    static func pair(
        baseURL: URL, code: String, session: URLSession = .shared
    ) async throws -> DeviceIdentity {
        let key = Curve25519.Signing.PrivateKey()
        let signature = try key.signature(
            for: SigningInput.pairing(code: code, publicKey: key.publicKey.rawRepresentation)
        ).base64URL

        var request = URLRequest(url: baseURL.appendingPathComponent("/v1/pair/complete"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "code": SigningInput.normalise(code),
            "public_key": key.publicKey.rawRepresentation.base64URL,
            "signature": signature,
        ])

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw BackendError.malformed }
        guard (200..<300).contains(http.statusCode) else {
            throw BackendError.http(http.statusCode, "")
        }

        let result = try JSONDecoder().decode(Result.self, from: data)
        guard let serverKey = Data(base64URL: result.serverPublicKey) else {
            throw BackendError.malformed
        }
        return DeviceIdentity(
            deviceID: result.deviceID, privateKey: key, serverPublicKey: serverKey
        )
    }
}
