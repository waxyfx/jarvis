//
//  Views.swift
//  Three screens: pair, home, ask.
//
//  Written for the thing a phone is actually for. The laptop is where JARVIS
//  listens; the phone is where you find out what it did, answer the one
//  question it is holding, and type something when speaking aloud would be
//  rude.
//
//  Dictation is the system keyboard's microphone button rather than the Speech
//  framework. It is free, needs no permission prompt, no entitlement and no
//  audio leaving this app, and it is the button people already know. The Speech
//  framework would buy continuous recognition, which is what the laptop is for.
//
//  NEVER BUILT — see apps/ios/README.md.
//

import SwiftUI

// MARK: - Pairing

struct PairingView: View {
    @ObservedObject var session: Session

    @State private var address = ""
    @State private var code = ""
    @State private var working = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://jarvis.example.com", text: $address)
                        .textContentType(.URL)
                        .keyboardType(.URL)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.never)
                } header: {
                    Text("Where JARVIS is")
                } footer: {
                    Text("The address your backend answers on.")
                }

                Section {
                    TextField("XXXX-XXXX", text: $code)
                        .textInputAutocapitalization(.characters)
                        .autocorrectionDisabled()
                        .font(.system(.body, design: .monospaced))
                } header: {
                    Text("Pairing code")
                } footer: {
                    Text(
                        "Issued on the machine running JARVIS. It is single-use "
                        + "and expires in a few minutes."
                    )
                }

                if let problem = session.problem {
                    Section {
                        Text(problem).foregroundStyle(.red)
                    }
                }

                Section {
                    Button(action: pair) {
                        if working { ProgressView() } else { Text("Pair this phone") }
                    }
                    .disabled(working || !isReady)
                }
            }
            .navigationTitle("JARVIS")
        }
    }

    /// Checked here so the button is honest rather than failing on tap. Eight
    /// characters after the separators come out, which is the code's real shape.
    private var isReady: Bool {
        URL(string: address)?.scheme?.hasPrefix("http") == true
            && SigningInput.normalise(code).count == 8
    }

    private func pair() {
        guard let url = URL(string: address) else { return }
        working = true
        Task {
            await session.pair(serverURL: url, code: code)
            working = false
        }
    }
}

// MARK: - Home

struct HomeView: View {
    @ObservedObject var session: Session

    @State private var overview: Overview?
    @State private var problem: String?
    @State private var loading = false

    var body: some View {
        NavigationStack {
            List {
                if let overview {
                    machineSection(overview)
                    pendingSection(overview)
                    daySection(overview)
                } else if loading {
                    HStack { Spacer(); ProgressView(); Spacer() }
                }

                if let problem {
                    Section { Text(problem).foregroundStyle(.secondary) }
                }

                Section {
                    NavigationLink("Ask JARVIS") { AskView(session: session) }
                }

                Section {
                    Button("Unpair this phone", role: .destructive) { session.unpair() }
                } footer: {
                    Text(
                        "Forgets this phone's key. Revoking the device on the "
                        + "backend is separate, and is what actually stops it."
                    )
                }
            }
            .navigationTitle("JARVIS")
            .refreshable { await load() }
            .task { await load() }
        }
    }

    @ViewBuilder
    private func machineSection(_ overview: Overview) -> some View {
        Section("Computer") {
            if overview.machines.isEmpty {
                Text("No machine is paired.").foregroundStyle(.secondary)
            }
            ForEach(overview.machines) { machine in
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Circle()
                            .fill(machine.connected ? .green : .secondary)
                            .frame(width: 8, height: 8)
                        Text(machine.name)
                        Spacer()
                        Text(machine.connected ? "connected" : "offline")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    if let cpu = machine.cpuPct, let ram = machine.ramUsedPct {
                        // Shown with its age when it is old rather than hidden:
                        // "12% an hour ago" is information, a blank row is not.
                        Text(
                            "CPU \(Int(cpu))%  ·  RAM \(Int(ram))%"
                            + (machine.telemetryStale ? "  ·  \(age(machine))" : "")
                        )
                        .font(.caption)
                        .foregroundStyle(machine.telemetryStale ? .secondary : .primary)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func pendingSection(_ overview: Overview) -> some View {
        if !overview.pending.isEmpty {
            Section {
                ForEach(overview.pending) { action in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(action.tool).font(.body.monospaced())
                            Text(action.risk).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("Confirm") { confirm(action) }
                            .buttonStyle(.borderedProminent)
                    }
                }
            } header: {
                Text("Waiting for you")
            } footer: {
                Text(
                    "JARVIS held these back. Confirming runs them; it does not "
                    + "change what the Policy Engine allows."
                )
            }
        }
    }

    @ViewBuilder
    private func daySection(_ overview: Overview) -> some View {
        Section("Today") {
            if let minutes = overview.activeMinutesToday {
                Text(spoken(minutes: minutes) + " at the computer")
            } else {
                // Not "0 minutes". That would be a claim about the day rather
                // than about the record.
                Text("Nothing recorded yet.").foregroundStyle(.secondary)
            }
        }
    }

    private func age(_ machine: Machine) -> String {
        guard let seconds = machine.telemetryAgeS else { return "" }
        if seconds < 3600 { return "\(seconds / 60) min ago" }
        return "\(seconds / 3600) h ago"
    }

    private func spoken(minutes: Int) -> String {
        let hours = minutes / 60
        let rest = minutes % 60
        if hours > 0 && rest > 0 { return "\(hours) h \(rest) min" }
        if hours > 0 { return "\(hours) h" }
        return "\(rest) min"
    }

    private func load() async {
        guard let backend = session.backend else { return }
        loading = true
        defer { loading = false }
        do {
            overview = try await backend.overview()
            problem = nil
        } catch {
            problem = error.localizedDescription
        }
    }

    private func confirm(_ action: PendingAction) {
        guard let backend = session.backend else { return }
        Task {
            do {
                _ = try await backend.confirm(callID: action.callID)
                await load()
            } catch {
                problem = error.localizedDescription
            }
        }
    }
}

// MARK: - Ask

struct AskView: View {
    @ObservedObject var session: Session

    @State private var text = ""
    @State private var turns: [Turn] = []
    @State private var working = false

    struct Turn: Identifiable {
        let id = UUID()
        let asked: String
        let reply: String
        let tools: [String]
    }

    var body: some View {
        VStack(spacing: 0) {
            List {
                ForEach(turns) { turn in
                    VStack(alignment: .leading, spacing: 8) {
                        Text(turn.asked)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                        Text(turn.reply)
                        if !turn.tools.isEmpty {
                            // What it did, not only what it said. A reply that
                            // sounds like something happened is not evidence
                            // that it did.
                            Text(turn.tools.joined(separator: ", "))
                                .font(.caption.monospaced())
                                .foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 4)
                }
            }

            HStack {
                TextField("Ask, or use the microphone key", text: $text, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .disabled(working)
                Button(action: send) {
                    if working { ProgressView() } else { Image(systemName: "arrow.up.circle.fill") }
                }
                .disabled(working || text.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            .padding()
        }
        .navigationTitle("Ask")
        .navigationBarTitleDisplayMode(.inline)
    }

    private func send() {
        guard let backend = session.backend else { return }
        let asked = text.trimmingCharacters(in: .whitespacesAndNewlines)
        text = ""
        working = true

        Task {
            do {
                let answer = try await backend.ask(asked)
                turns.append(
                    Turn(
                        asked: asked,
                        reply: answer.reply,
                        tools: answer.executed.map { "\($0.tool) → \($0.status)" }
                    )
                )
            } catch {
                turns.append(Turn(asked: asked, reply: error.localizedDescription, tools: []))
            }
            working = false
        }
    }
}
