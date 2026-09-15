//
//  JARVISApp.swift
//  The app, and the one piece of state everything else reads.
//
//  Two states, and the whole app is which one it is in: paired, or not. There
//  is no third — an app that is "sort of paired" is an app that shows a broken
//  screen and asks the person to work out why.
//
//  NEVER BUILT — see apps/ios/README.md.
//

import SwiftUI

@main
struct JARVISApp: App {
    @StateObject private var session = Session()

    var body: some Scene {
        WindowGroup {
            Group {
                if session.backend == nil {
                    PairingView(session: session)
                } else {
                    HomeView(session: session)
                }
            }
            .task { session.restore() }
        }
    }
}

/// What the app knows about itself.
@MainActor
final class Session: ObservableObject {
    @Published private(set) var backend: Backend?
    @Published private(set) var serverURL: URL?
    @Published var problem: String?

    private let store = IdentityStore()
    private let serverKey = "jarvis.server.url"

    /// Come back to a phone that was already paired. Nothing here reaches the
    /// network: a launch on a train should draw the screen it can and fail at
    /// the first request rather than at startup.
    func restore() {
        guard
            let saved = UserDefaults.standard.string(forKey: serverKey),
            let url = URL(string: saved)
        else { return }

        do {
            guard let identity = try store.load() else { return }
            serverURL = url
            backend = Backend(baseURL: url, identity: identity)
        } catch {
            // A keychain item that cannot be read is worse than none: every
            // request would fail in a way that looks like the server's fault.
            problem = "The stored identity could not be read. Pair this phone again."
            store.clear()
        }
    }

    func pair(serverURL url: URL, code: String) async {
        problem = nil
        do {
            let identity = try await Pairing.pair(baseURL: url, code: code)
            try store.save(identity)
            UserDefaults.standard.set(url.absoluteString, forKey: serverKey)
            serverURL = url
            backend = Backend(baseURL: url, identity: identity)
        } catch {
            problem = error.localizedDescription
        }
    }

    /// Forget this phone's key.
    ///
    /// The backend keeps its own record until the device is revoked there, and
    /// that asymmetry is deliberate: a phone should not be able to erase the
    /// trail of what it did by deleting something on itself.
    func unpair() {
        store.clear()
        UserDefaults.standard.removeObject(forKey: serverKey)
        backend = nil
        serverURL = nil
    }
}
