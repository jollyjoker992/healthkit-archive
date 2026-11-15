import SwiftUI

// In-app help and credits. Written for people who are not technical: short sentences, everyday
// words, and the same names the rest of the app uses ("processor", "Back Up Now").

enum AppLinks {
    static let source = URL(string: "https://github.com/jollyjoker992/healthkit-archive")!
    static let license = URL(string: "https://github.com/jollyjoker992/healthkit-archive/blob/main/LICENSE")!
    /// Opens the Health app. Apple offers no link to one app's Health permissions.
    static let health = URL(string: "x-apple-health://")!
}

/// The four kinds of upload, with the symbol the Uploads list shows for each.
enum UploadKind: String, CaseIterable, Identifiable {
    case sync, backfill, resend, census
    var id: String { rawValue }

    var symbol: String {
        switch self {
        case .sync: "heart.text.square"
        case .backfill: "clock.arrow.circlepath"
        case .resend: "checkmark.circle"
        case .census: "chart.bar"
        }
    }

    var title: String {
        switch self {
        case .sync: String(localized: "New Health data")
        case .backfill: String(localized: "Earlier history")
        case .resend: String(localized: "Daily check")
        case .census: String(localized: "Weekly count")
        }
    }

    var explanation: String {
        switch self {
        case .sync: String(localized: "Anything new in Health since the last upload: steps, heart rate, workouts, sleep and more. Most of your backup is this.")
        case .backfill: String(localized: "All your past Health data, sent again. This only happens after you tap Re-send All History, so a newly added processor can read your older data too.")
        case .resend: String(localized: "Once a day, the last two days are sent again to fill any gaps. It also tells your family’s computer that this iPhone is still backing up, so it is sent even when nothing is new.")
        case .census: String(localized: "Once a week, a count of how many records you have each day, not the records themselves. Your family’s computer uses it to check that nothing is missing.")
        }
    }
}

/// What each kind of upload is for, opened from the Uploads screen and from About.
struct UploadKindsView: View {
    var body: some View {
        List {
            Section {
                ForEach(UploadKind.allCases) { kind in
                    HStack(alignment: .top, spacing: 14) {
                        Image(systemName: kind.symbol).font(.title3).foregroundStyle(.tint).frame(width: 28)
                            .accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(verbatim: kind.title).font(.headline)
                            Text(verbatim: kind.explanation).font(.subheadline).foregroundStyle(.secondary)
                        }
                    }
                    .padding(.vertical, 4)
                    .accessibilityElement(children: .combine)
                }
            } footer: {
                Text("Sending something twice is safe. Your family’s computer recognises copies and keeps each record once.")
            }
        }
        .navigationTitle("Types of Upload").navigationBarTitleDisplayMode(.inline)
    }
}

/// One short topic of the help page.
private struct Topic: View {
    let symbol: String
    let title: LocalizedStringKey
    let text: LocalizedStringKey

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            Image(systemName: symbol).font(.title3).foregroundStyle(.tint).frame(width: 28).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                Text(text).font(.subheadline).foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
    }
}

struct HowItWorksView: View {
    var body: some View {
        List {
            Topic(symbol: "heart.fill", title: "What it does",
                  text: "Health Backup keeps a private copy of your Apple Health data, so it is not lost if your iPhone is lost, broken or replaced.")
            Topic(symbol: "lock.fill", title: "Locked before it leaves",
                  text: "Your data is locked (encrypted) on this iPhone before it is sent. The server only stores locked files. It cannot open them, and neither can anyone who breaks into it.")
            Topic(symbol: "person.badge.key.fill", title: "Who can unlock it",
                  text: "Only the processors you add can unlock your data. A processor is a computer that holds a key, usually your family’s computer. Adding a recovery card as a second one means a lost key does not mean lost data.")
            Topic(symbol: "arrow.triangle.2.circlepath", title: "When it backs up",
                  text: "The app backs up by itself in the background. iPhone only lets apps read Health data while the phone is unlocked, so backups happen while you use your phone. You can also tap Back Up Now.")
            Topic(symbol: "checkmark.circle", title: "What you need to do",
                  text: "Usually nothing. If the top of the main screen shows a warning, follow what it says, or ask your family’s admin.")
            Topic(symbol: "slider.horizontal.3", title: "Choosing what is shared",
                  text: "Open the Health app, tap your picture, then Apps, then Health Backup. Turn on everything you want backed up.")
        }
        .navigationTitle("How It Works").navigationBarTitleDisplayMode(.inline)
    }
}

/// Help, credits and the disclaimer, opened from the info button on the main screen.
struct AboutView: View {
    @Environment(\.dismiss) var dismiss
    enum Page: Hashable { case howItWorks, uploadKinds }
    @State var path: [Page] = []

    var version: String {
        let info = Bundle.main.infoDictionary ?? [:]
        return "\(info["CFBundleShortVersionString"] ?? "?") (\(info["CFBundleVersion"] ?? "?"))"
    }

    var body: some View {
        NavigationStack(path: $path) {
            List {
                Section {
                    NavigationLink(value: Page.howItWorks) { Label("How It Works", systemImage: "questionmark.circle") }
                    NavigationLink(value: Page.uploadKinds) { Label("Types of Upload", systemImage: "square.stack.3d.up") }
                }
                Section {
                    LabeledContent("Version", value: version)
                    Link(destination: AppLinks.source) { Label("Source Code on GitHub", systemImage: "chevron.left.forwardslash.chevron.right") }
                    Link(destination: AppLinks.license) { Label("License: Apache 2.0", systemImage: "doc.text") }
                } header: {
                    Text("Open source")
                } footer: {
                    Text("Health Backup is free and open source. Anyone can read the code to check what it does with your data.")
                }
                Section {
                    Text("Health Backup is not a medical device and does not give medical advice. It is provided as is, without any warranty. It is not made by, or connected with, Apple. Apple Health and iPhone are trademarks of Apple Inc.")
                        .font(.footnote).foregroundStyle(.secondary)
                } header: {
                    Text("Disclaimer")
                }
            }
            .navigationTitle("About").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            .navigationDestination(for: Page.self) { page in
                switch page {
                case .howItWorks: HowItWorksView()
                case .uploadKinds: UploadKindsView()
                }
            }
            #if DEBUG
            .onAppear {
                switch AppModel.debugScreen {
                case "how-it-works": path = [.howItWorks]
                case "upload-kinds": path = [.uploadKinds]
                default: break
                }
            }
            #endif
        }
    }
}
