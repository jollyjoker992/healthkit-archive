import Foundation
import HKACore
import SwiftUI
import UIKit

/// UI state. Every operation goes through `DeviceHost`, which serializes access to the device.
@MainActor
final class AppModel: ObservableObject {
    @Published var state: DeviceState?
    @Published var outboxCount = 0
    @Published var busy = false
    /// The last failure, shown as an alert. Success needs no message: the status header changes.
    /// The title is localized; the detail is technical (relay codes stay English, system errors are
    /// already in the phone's language).
    @Published var failure: Failure?
    @Published var started = false

    func load() async {
        do {
            try await DeviceHost.shared.start(protectedDataAvailable: UIApplication.shared.isProtectedDataAvailable)
        } catch {
            failure = Failure(title: String(localized: "Couldn’t open this iPhone’s key"), detail: "\(error)")
        }
        await refresh()
        #if DEBUG
        await debugAutoEnroll()
        #endif
        // An update can add types: ask for them before they are read, so their history is not skipped.
        if state != nil {
            let source = await DeviceHost.shared.source(readable: { true })
            if await source.needsAuthorization() { try? await source.requestAuthorization() }
        }
        started = true
    }

    #if DEBUG
    /// Development only: `simctl launch` with SIMCTL_CHILD_HKA_DEBUG_INVITE and SIMCTL_CHILD_HKA_DEBUG_CARD
    /// enrols, adds the processor and syncs without typing. Compiled out of release builds.
    func debugAutoEnroll() async {
        let env = ProcessInfo.processInfo.environment
        if let seed = env["HKA_DEBUG_SEED"] {
            do {
                try await DebugSeed.run(seed)
            } catch {
                failure = Failure(title: "Debug seed failed", detail: "\(error)")
                return
            }
        }
        guard state == nil, let inviteText = env["HKA_DEBUG_INVITE"] else { return }
        do {
            try await DeviceHost.shared.enroll(invite: try Invite.parse(inviteText), deviceName: "Simulator")
            let cards = (env["HKA_DEBUG_CARD"] ?? "").split(whereSeparator: \.isWhitespace).map(String.init)
            if !cards.isEmpty {
                try await DeviceHost.shared.setProcessors(try cards.map { try Processor.parseCard($0) })
                try await DeviceHost.shared.sync(readable: true)
            }
        } catch {
            failure = Failure(title: "Debug enrolment failed", detail: "\(error)")
        }
        await refresh()
    }
    /// Development only: SIMCTL_CHILD_HKA_DEBUG_SCREEN opens a tab, sheet or alert at launch so every
    /// screen can be captured with `simctl io screenshot` (see ios/README.md).
    static let debugScreen = ProcessInfo.processInfo.environment["HKA_DEBUG_SCREEN"] ?? ""
    static func debugEnv(_ key: String) -> String? { ProcessInfo.processInfo.environment[key] }
    #endif

    func refresh() async {
        state = await DeviceHost.shared.state()
        outboxCount = await DeviceHost.shared.outboxCount()
    }

    var deviceFingerprint: String? { state?.deviceFingerprint }

    func run(_ title: LocalizedStringResource, _ work: @escaping () async throws -> Void) {
        busy = true
        failure = nil
        Task {
            do {
                try await work()
            } catch let e as RelayError {
                failure = Failure(title: String(localized: title), detail: "\(e.code): \(e.message)")
            } catch {
                failure = Failure(title: String(localized: title), detail: error.localizedDescription)
            }
            await refresh()
            busy = false
        }
    }

    func enroll(inviteText: String, deviceName: String) {
        run("Couldn’t join") {
            let invite = try Invite.parse(inviteText.trimmingCharacters(in: .whitespacesAndNewlines))
            try await DeviceHost.shared.enroll(invite: invite, deviceName: deviceName)
            try await DeviceHost.shared.source(readable: { true }).requestAuthorization()
            (UIApplication.shared.delegate as? AppDelegate)?.startObserving()
        }
    }

    func setProcessors(_ processors: [Processor]) {
        run("Couldn’t update processors") { try await DeviceHost.shared.setProcessors(processors) }
    }

    func syncNow() {
        run("Couldn’t back up") {
            let readable = UIApplication.shared.isProtectedDataAvailable
            try await DeviceHost.shared.sync(readable: readable)
        }
    }

    func requestHealthAccess() {
        run("Couldn’t request Health access") { try await DeviceHost.shared.source(readable: { true }).requestAuthorization() }
    }

    func startOver() {
        run("Couldn’t start over") { try await DeviceHost.shared.startOver() }
    }

    func resendHistory() {
        run("Couldn’t re-send history") {
            try await DeviceHost.shared.resendHistory()
            try await DeviceHost.shared.sync(readable: UIApplication.shared.isProtectedDataAvailable)
        }
    }
}

struct Failure: Equatable {
    let title: String
    let detail: String
}
