import HKACore
import SwiftUI
import VisionKit

// Localization: every user-facing string is one literal (never concatenated) so it is a key in
// Localizable.xcstrings. Values that come from data (names, fingerprints, relay codes) are shown verbatim.

@main
struct HKAApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView().environmentObject(model).task { await model.load() }
        }
    }
}

/// One screen per state: loading, not enrolled (welcome), enrolled (home). Failures are alerts;
/// success is shown by the status header, not by a transient message.
struct RootView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.scenePhase) var scenePhase

    var body: some View {
        Group {
            if !model.started {
                ProgressView()
            } else if model.state == nil {
                WelcomeView()
            } else {
                HomeView()
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await model.refresh() } }
        }
        .alert(model.failure?.title ?? "", isPresented: Binding(get: { model.failure != nil }, set: { if !$0 { model.failure = nil } })) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(verbatim: model.failure?.detail ?? "")
        }
    }
}

// MARK: welcome and enrolment

struct WelcomeView: View {
    @EnvironmentObject var model: AppModel
    @State var scanning = false
    @State var invite: Invite?

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                ScrollView {
                    VStack(spacing: 32) {
                        VStack(spacing: 12) {
                            Image("Logo").resizable().frame(width: 88, height: 88)
                                .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
                                .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(.quaternary))
                                .accessibilityHidden(true)
                            Text("Health Backup").font(.largeTitle.bold()).multilineTextAlignment(.center)
                            Text("A private backup of your Health data for your family.")
                                .font(.body).foregroundStyle(.secondary).multilineTextAlignment(.center)
                        }
                        .padding(.top, 48)
                        VStack(alignment: .leading, spacing: 24) {
                            Feature(symbol: "lock.fill", title: "Encrypted on this iPhone",
                                    text: "Data is locked before it leaves the phone. The server stores it but cannot read it.")
                            Feature(symbol: "person.badge.key.fill", title: "You choose who can read it",
                                    text: "Only the processors you add can unlock your backup. You can remove them later.")
                            Feature(symbol: "arrow.triangle.2.circlepath", title: "Runs in the background",
                                    text: "New Health data is backed up while the phone is unlocked.")
                        }
                    }
                    .padding(.horizontal, 28)
                }
                VStack(spacing: 8) {
                    Button { scanning = true } label: { Text("Scan Invite").frame(maxWidth: .infinity) }
                        .buttonStyle(.borderedProminent).controlSize(.large).buttonBorderShape(.capsule)
                    Text("Ask your family’s admin for an invite code.").font(.footnote).foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .padding(.horizontal, 28).padding(.vertical, 16)
            }
            .sheet(isPresented: $scanning) {
                CodeScanner(title: "Scan Invite", prefix: "hka-invite:",
                            hint: "Point the camera at the invite QR code from your admin.") { text in
                    guard let parsed = try? Invite.parse(text) else { return false }
                    scanning = false
                    invite = parsed
                    return true
                }
            }
            .navigationDestination(item: $invite) { JoinView(invite: $0) }
            #if DEBUG
            .onAppear {
                if let text = AppModel.debugEnv("HKA_DEBUG_PREFILL") { invite = try? Invite.parse(text) }
                if AppModel.debugScreen == "scan-invite" { scanning = true }
            }
            #endif
        }
    }
}

struct Feature: View {
    let symbol: String
    let title: LocalizedStringKey
    let text: LocalizedStringKey

    var body: some View {
        HStack(alignment: .top, spacing: 16) {
            Image(systemName: symbol).font(.title2).foregroundStyle(.tint).frame(width: 36).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                Text(text).font(.subheadline).foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .combine)
    }
}

/// Shows what the invite says before anything is created, and names the Health prompt that follows.
struct JoinView: View {
    @EnvironmentObject var model: AppModel
    let invite: Invite
    @State var deviceName = UIDevice.current.name

    var body: some View {
        Form {
            Section {
                LabeledContent("Backing up for", value: invite.userName)
                LabeledContent("Server", value: URL(string: invite.server)?.host() ?? invite.server)
            } footer: {
                Text("Check these with your admin. The invite can be used once.")
            }
            Section {
                TextField("Name", text: $deviceName)
            } header: {
                Text("Name of this iPhone")
            } footer: {
                Text("Next, iOS asks which Health data to share. Choose Turn On All to back up everything. The app only reads Health data.")
            }
        }
        .navigationTitle("Join").navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) {
            Button {
                model.enroll(inviteText: invite.text, deviceName: deviceName)
            } label: {
                Group {
                    if model.busy { ProgressView() } else { Text("Continue") }
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent).controlSize(.large).buttonBorderShape(.capsule)
            .disabled(model.busy || deviceName.trimmingCharacters(in: .whitespaces).isEmpty)
            .padding(.horizontal, 28).padding(.vertical, 16)
        }
    }
}

// MARK: home

/// What the header says. One answer to "is my backup working?", worst problem first.
struct BackupStatus {
    enum Level { case ok, working, attention, stopped }
    let level: Level
    let title: String
    let detail: String
    /// The device's technical reason for a stop. The core writes it in English; it is shown as a detail.
    var reason: String?

    /// The processor side alarms after three silent days (design 5.3); warn the owner at the same point.
    static let staleAfter: TimeInterval = 3 * 86400

    init(state s: DeviceState, waiting: Int, busy: Bool, now: Date = Date()) {
        func ago(_ d: Date) -> String { d.formatted(.relative(presentation: .named)) }
        switch s.stop {
        case .halted(let reason, _):
            level = .stopped
            title = String(localized: "Backup stopped")
            detail = String(localized: "The server refuses uploads from this iPhone. Ask your admin to retire it, then start over.")
            self.reason = reason
            return
        case .paused(let reason, _):
            level = .attention
            title = String(localized: "Backup paused")
            detail = String(localized: "The server has no room left for your backup. Ask your admin to raise the limit.")
            self.reason = reason
            return
        case nil: break
        }
        if s.currentGrant == nil {
            (level, title, detail) = (.attention, String(localized: "Finish setting up"),
                                      String(localized: "Add a processor. Nothing is uploaded until someone can read it."))
        } else if busy {
            (level, title, detail) = (.working, String(localized: "Backing up…"),
                                      String(localized: "Keep the app open until this finishes."))
        } else if waiting > 0 {
            (level, title, detail) = (.working, String(localized: "Waiting to upload"),
                                      String(localized: "\(waiting) encrypted files are queued. iOS uploads them in the background."))
        } else if let last = s.lastUploadAt {
            if now.timeIntervalSince(last) > Self.staleAfter {
                (level, title, detail) = (.attention, String(localized: "Backup is out of date"),
                                          String(localized: "Last backup \(ago(last)). Unlock the phone and tap Back Up Now."))
            } else {
                (level, title, detail) = (.ok, String(localized: "Backed up"), String(localized: "Last backup \(ago(last))."))
            }
        } else {
            (level, title, detail) = (.attention, String(localized: "Not backed up yet"), String(localized: "Tap Back Up Now to start."))
        }
    }

    var symbol: String {
        switch level {
        case .ok: "checkmark.circle.fill"
        case .working: "arrow.triangle.2.circlepath.circle.fill"
        case .attention: "exclamationmark.triangle.fill"
        case .stopped: "xmark.octagon.fill"
        }
    }

    var color: Color {
        switch level {
        case .ok: .green
        case .working: .blue
        case .attention: .orange
        case .stopped: .red
        }
    }
}

struct HomeView: View {
    @EnvironmentObject var model: AppModel
    @State var scanning = false
    @State var candidate: Processor?
    @State var selected: Processor?
    @State var confirmStartOver = false
    @State var confirmResend = false

    var processors: [Processor] { model.state?.currentGrant?.processors ?? [] }

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                List {
                    if let s = model.state {
                        statusSection(s)
                        processorSection
                        activitySection(s)
                        phoneSection(s)
                        advancedSection.id("advanced")
                    }
                }
                #if DEBUG
                .task {
                    if ["bottom", "resend"].contains(AppModel.debugScreen) {
                        try? await Task.sleep(nanoseconds: 500_000_000)
                        proxy.scrollTo("advanced", anchor: .bottom)
                        if AppModel.debugScreen == "resend" { confirmResend = true }
                    }
                }
                #endif
            }
            .navigationTitle("Health Backup")
            .refreshable { await model.refresh() }
            .navigationDestination(item: $selected) { ProcessorDetail(processor: $0, isLast: processors.count <= 1) }
            .sheet(isPresented: $scanning) {
                CodeScanner(title: "Scan Processor Card", prefix: "hka-card:",
                            hint: "Point the camera at the QR code shown by “hka card”.") { text in
                    guard let parsed = try? Processor.parseCard(text) else { return false }
                    scanning = false
                    candidate = parsed
                    return true
                }
            }
            .sheet(item: $candidate) { AddProcessorSheet(processor: $0, alreadyAdded: processors.contains($0)) }
            .alert("Start over as a new device?", isPresented: $confirmStartOver) {
                Button("Start Over", role: .destructive) { model.startOver() }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("This iPhone forgets its key and needs a new invite. Ask your admin to retire the old device first. What is already backed up stays readable.")
            }
            #if DEBUG
            .onAppear {
                switch AppModel.debugScreen {
                case "scan-card": scanning = true
                case "add-card": candidate = AppModel.debugEnv("HKA_DEBUG_CARD2").flatMap { try? Processor.parseCard($0) }
                case "processor": selected = processors.last
                case "start-over": confirmStartOver = true
                case "error": model.failure = Failure(title: String(localized: "Couldn’t back up"), detail: "network: the relay could not be reached")
                default: break
                }
            }
            #endif
        }
    }

    func statusSection(_ s: DeviceState) -> some View {
        let status = BackupStatus(state: s, waiting: model.outboxCount, busy: model.busy)
        return Section {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: status.symbol).font(.system(size: 34)).foregroundStyle(status.color)
                    .symbolRenderingMode(.hierarchical).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text(verbatim: status.title).font(.title3.weight(.semibold))
                    Text(verbatim: status.detail).font(.subheadline).foregroundStyle(.secondary)
                    if let reason = status.reason {
                        Text(verbatim: reason).font(.caption).foregroundStyle(.secondary).padding(.top, 4)
                    }
                }
            }
            .padding(.vertical, 6)
            .accessibilityElement(children: .combine)
            if case .halted = s.stop {
                Button("Start Over as a New Device", role: .destructive) { confirmStartOver = true }
            } else if s.currentGrant == nil {
                Button("Scan Processor Card") { scanning = true }.disabled(model.busy)
            } else {
                Button {
                    model.syncNow()
                } label: {
                    HStack {
                        Text("Back Up Now")
                        if model.busy { Spacer(); ProgressView() }
                    }
                }
                .disabled(model.busy)
            }
        } footer: {
            if s.currentGrant != nil { Text("Health data can only be read while the iPhone is unlocked.") }
        }
    }

    var processorSection: some View {
        Section {
            ForEach(processors, id: \.id) { p in
                Button { selected = p } label: {
                    HStack {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(verbatim: p.name).foregroundStyle(Color(.label))
                            Text(verbatim: p.fingerprint).font(.caption.monospaced()).foregroundStyle(Color(.secondaryLabel))
                        }
                        Spacer()
                        Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Color(.tertiaryLabel))
                    }
                }
            }
            if processors.isEmpty {
                Text("No one yet").foregroundStyle(.secondary)
            } else {
                Button("Add Processor") { scanning = true }.disabled(processors.count >= 8 || model.busy)
            }
        } header: {
            Text("Who can read this backup")
        } footer: {
            if processors.isEmpty {
                Text("A processor is a computer that holds a key to your backup, usually your family’s.")
            } else if processors.count == 1 {
                Text("If this processor loses its key, the backup cannot be read. Add a recovery card as a second one.")
            }
        }
    }

    func activitySection(_ s: DeviceState) -> some View {
        Section("Activity") {
            LabeledContent("Last read from Health", value: s.lastSyncAt.map(Self.format) ?? String(localized: "Never"))
            LabeledContent("Last upload", value: s.lastUploadAt.map(Self.format) ?? String(localized: "Never"))
            if model.outboxCount > 0 { LabeledContent("Waiting to upload", value: model.outboxCount.formatted()) }
            if let errors = s.typeErrors, !errors.isEmpty {
                LabeledContent("Data types not shared", value: errors.count.formatted())
            }
            if let e = s.lastError {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Last problem")
                    Text(verbatim: e).font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
    }

    func phoneSection(_ s: DeviceState) -> some View {
        Section {
            LabeledContent("Belongs to", value: s.userName)
            LabeledContent("Name", value: s.deviceName)
            VStack(alignment: .leading, spacing: 4) {
                Text("Fingerprint")
                Text(verbatim: model.deviceFingerprint ?? "—").font(.callout.monospaced()).foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        } header: {
            Text("This iPhone")
        } footer: {
            Text("A processor checks this fingerprint before it accepts data from this iPhone.")
        }
    }

    var advancedSection: some View {
        Section {
            Button("Review Health Access") { model.requestHealthAccess() }.disabled(model.busy)
            Button("Re-send All History…") { confirmResend = true }.disabled(model.busy || processors.isEmpty)
                // Attached to the button so the dialog appears where the tap was.
                .confirmationDialog("Re-send all history?", isPresented: $confirmResend, titleVisibility: .visible) {
                    Button("Re-send All History") { model.resendHistory() }
                    Button("Cancel", role: .cancel) {}
                } message: {
                    Text("Uploads everything in Health again so a newly added processor can read older data. This can take a while and uses data.")
                }
        } footer: {
            Text("To change what is shared later, open the Health app, tap your picture, then Apps.")
        }
    }

    static func format(_ d: Date) -> String { d.formatted(date: .abbreviated, time: .shortened) }
}

// MARK: processors

struct FingerprintText: View {
    let value: String
    var body: some View {
        Text(verbatim: value).font(.title3.monospaced().weight(.medium)).lineLimit(1).minimumScaleFactor(0.6)
            .textSelection(.enabled)
            .accessibilityLabel(Text("Fingerprint \(value.map(String.init).joined(separator: " "))"))
    }
}

struct ProcessorDetail: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) var dismiss
    let processor: Processor
    let isLast: Bool
    @State var confirmRemove = false

    var body: some View {
        List {
            Section {
                FingerprintText(value: processor.fingerprint)
            } header: {
                Text("Fingerprint")
            } footer: {
                Text("It matches the fingerprint this processor shows with “hka card”.")
            }
            Section {
                Button("Remove Processor", role: .destructive) { confirmRemove = true }.disabled(isLast || model.busy)
            } footer: {
                if isLast {
                    Text("This is the only processor. Add another before removing it.")
                } else {
                    Text("It gets nothing new. What it already downloaded stays readable to it.")
                }
            }
        }
        .navigationTitle(processor.name).navigationBarTitleDisplayMode(.inline)
        .confirmationDialog("Remove \(processor.name)?", isPresented: $confirmRemove, titleVisibility: .visible) {
            Button("Remove Processor", role: .destructive) {
                let rest = (model.state?.currentGrant?.processors ?? []).filter { $0.id != processor.id }
                model.setProcessors(rest)
                dismiss()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("New backups will no longer be readable by it. Files already waiting to upload still are.")
        }
        #if DEBUG
        .onAppear { if AppModel.debugEnv("HKA_DEBUG_REMOVE") != nil { confirmRemove = true } }
        #endif
    }
}

/// Adding a processor hands over the ability to decrypt, so the fingerprint gets a full sheet
/// (readable, selectable) and the button says what it does.
struct AddProcessorSheet: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) var dismiss
    let processor: Processor
    let alreadyAdded: Bool

    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                Image(systemName: "person.badge.key.fill").font(.system(size: 44)).foregroundStyle(.tint)
                    .accessibilityHidden(true)
                Text(verbatim: processor.name).font(.title2.bold()).multilineTextAlignment(.center)
                VStack(spacing: 6) {
                    Text("Fingerprint").font(.footnote).foregroundStyle(.secondary)
                    FingerprintText(value: processor.fingerprint)
                }
                .frame(maxWidth: .infinity).padding()
                .background(Color(.secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 16))
                Group {
                    if alreadyAdded {
                        Text("This processor is already on your list.")
                    } else {
                        Text("Compare this with the fingerprint on the processor’s screen. If they match, it will be able to read everything backed up from now on.")
                    }
                }
                .font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 0)
                Button {
                    let current = model.state?.currentGrant?.processors ?? []
                    model.setProcessors(current + [processor])
                    dismiss()
                } label: {
                    Text("Fingerprints Match, Add").frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent).controlSize(.large).buttonBorderShape(.capsule)
                .disabled(alreadyAdded || model.busy)
            }
            .padding(.horizontal, 28).padding(.vertical, 16)
            .background(Color(.systemGroupedBackground))
            .navigationTitle("Add Processor").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
        .presentationDetents([.fraction(0.7), .large])
    }
}

// MARK: QR scanning

/// Camera scanner with a paste fallback. `accept` returns false for a code that does not parse,
/// which is shown inline instead of closing the sheet.
struct CodeScanner: View {
    let title: LocalizedStringKey
    let prefix: String
    let hint: LocalizedStringKey
    let accept: (String) -> Bool
    @State var rejected = false
    @Environment(\.dismiss) var dismiss
    @Environment(\.openURL) var openURL

    var body: some View {
        NavigationStack {
            VStack(spacing: 16) {
                if !DataScannerViewController.isSupported {
                    ContentUnavailableView("Camera Not Available", systemImage: "camera",
                                           description: Text("Copy the code on another device and paste it here."))
                } else if !DataScannerViewController.isAvailable {
                    ContentUnavailableView {
                        Label("Camera Access Is Off", systemImage: "camera")
                    } description: {
                        Text("Allow the camera in Settings, or paste the code instead.")
                    } actions: {
                        Button("Open Settings") {
                            if let url = URL(string: UIApplication.openSettingsURLString) { openURL(url) }
                        }
                    }
                } else {
                    DataScanner(prefix: prefix) { submit($0) }
                        .clipShape(RoundedRectangle(cornerRadius: 20)).padding(.horizontal)
                    Text(hint).font(.subheadline).foregroundStyle(.secondary).multilineTextAlignment(.center)
                        .padding(.horizontal, 28)
                }
                if rejected {
                    Label("That is not a valid code. It starts with “\(prefix)”.", systemImage: "exclamationmark.circle")
                        .font(.footnote).foregroundStyle(.red).padding(.horizontal, 28)
                }
                PasteButton(payloadType: String.self) { strings in
                    if let text = strings.first { submit(text) }
                }
                .buttonBorderShape(.capsule).labelStyle(.titleAndIcon)
                .padding(.bottom, 16)
            }
            .navigationTitle(title).navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
            #if DEBUG
            .onAppear { if AppModel.debugEnv("HKA_DEBUG_REJECT") != nil { rejected = true } }
            #endif
        }
    }

    func submit(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        rejected = !(trimmed.hasPrefix(prefix) && accept(trimmed))
    }
}

struct DataScanner: UIViewControllerRepresentable {
    let prefix: String
    let found: (String) -> Void

    func makeUIViewController(context: Context) -> DataScannerViewController {
        let vc = DataScannerViewController(recognizedDataTypes: [.barcode(symbologies: [.qr])], isHighlightingEnabled: true)
        vc.delegate = context.coordinator
        try? vc.startScanning()
        return vc
    }

    func updateUIViewController(_ vc: DataScannerViewController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(prefix: prefix, found: found) }

    final class Coordinator: NSObject, DataScannerViewControllerDelegate {
        let prefix: String
        let found: (String) -> Void
        var last: String?  // a code that was rejected is not reported again while it stays in view
        init(prefix: String, found: @escaping (String) -> Void) { self.prefix = prefix; self.found = found }

        func dataScanner(_ scanner: DataScannerViewController, didAdd items: [RecognizedItem], allItems: [RecognizedItem]) {
            for case .barcode(let code) in items {
                if let text = code.payloadStringValue, text.hasPrefix(prefix), text != last {
                    last = text
                    found(text)
                }
            }
        }
    }
}
