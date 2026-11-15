# Health Backup (iOS)

The device side of [../spec/README.md](../spec/README.md): reads Apple Health, encrypts each batch on the
phone with `age` to the processors the member chose, signs it with a Secure Enclave key, and
uploads it to the relay.

```
HKACore/            Swift package, builds and tests on macOS
  Encoding, Keys, Documents     spec sections 1-5 (b64u, instants, P-256, invite/card/grant/manifest)
  Age, Gzip                     encrypt-only age v1 writer (X25519), gzip
  DeviceStore, SyncEngine       state, outbox, sealing, upload rules (spec 4.2, 4.3 device rules)
  RelayClient                   relay calls, error classification (spec 6.2)
  HealthKitSource               HealthKit -> records (spec/records.md), catalog of read types
  hka-swift-device              command-line stand-in for the app, used in end-to-end tests
App/                SwiftUI app: onboarding, status, processors, QR scanning, background work
AppSupport/         Info.plist additions, entitlements (HealthKit, background delivery)
HealthBackup.xcodeproj
```

## Build and test

```sh
cd ios/HKACore && swift test     # vectors, age output decrypted by the age CLI and pyrage, sync engine, serializer
xcodebuild -project ios/HealthBackup.xcodeproj -scheme HealthBackup -sdk iphonesimulator \
  CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM= build
```

The cross-language end-to-end test (`cli/tests/test_e2e_local.py`) runs `hka-swift-device` against the
real Worker under `wrangler dev` and decrypts its batches with the Python processor.

The Simulator end-to-end test (`cli/tests/test_e2e_simulator.py`, UI tests in `UITests/`) runs the real
app: `App/DebugSeed.swift` writes a known day into the Simulator's Health store (steps, heart rate, sleep
stages, a running workout with a GPS route, a pickleball workout, weight, blood pressure, glucose, VO2 max,
water, a mindful session), the UI test allows Health access in the system sheet, and the app enrols, reads
HealthKit and uploads; the processor and Medplum must then hold exactly the seeded values. A second step
deletes the weigh-in in Health and taps Back Up Now; the deletion must reach Medplum. It needs Xcode's
first-launch components (`sudo xcodebuild -runFirstLaunch`; see [docs/ci.md](../docs/ci.md)).

Debug builds read these launch variables (all compiled out of release builds), so every screen can be
captured with `xcrun simctl io <sim> screenshot` without tapping:

| Variable (prefix `SIMCTL_CHILD_`) | Effect |
|---|---|
| `HKA_DEBUG_INVITE`, `HKA_DEBUG_CARD` | Enrol with the invite and add the cards (space-separated; may be empty) |
| `HKA_DEBUG_PREFILL` | Open the Join screen for this invite without enrolling |
| `HKA_DEBUG_SCREEN` | `scan-invite`, `scan-card`, `add-card` (card from `HKA_DEBUG_CARD2`), `processor`, `details` (Backup Details), `bottom` (its end), `resend`, `start-over`, `error` |
| `HKA_DEBUG_REMOVE`, `HKA_DEBUG_REJECT` | Open the remove dialog on the processor screen; show the invalid-code message in the scanner |
| `HKA_DEBUG_SEED` | `1`: write the test day into HealthKit (removing earlier seed data first); `delete-weight`: delete the seeded weigh-in. `probe:<type>,…,read:<from>-<to>`: only request authorization for those types (HealthKit aborts at once on a type an app may not request; used to find that correlations cannot be requested). Debug builds also carry a Health *write* description for this; release builds never ask to write. |

## Languages

English and Vietnamese. Strings live in `App/Localizable.xcstrings`; the app name and permission prompts
in `App/InfoPlist.xcstrings`. Every user-facing string is a single literal (never concatenated) so the
compiler extracts it. Names, fingerprints and relay error codes are shown as they are; stop reasons
written by `HKACore` stay English and appear only as a detail under a translated explanation.

After changing UI text, build and check that every string has a translation:

```sh
python3 ios/tools/check-strings.py <derived data folder>   # the -derivedDataPath you built with
```

To see the app in Vietnamese in the Simulator: `xcrun simctl launch <sim> app.hka.healthbackup -AppleLanguages "(vi)" -AppleLocale vi_VN`.

## On a phone

1. Open `HealthBackup.xcodeproj`, set your team and a bundle identifier you own (`app.hka.healthbackup` is a
   placeholder), and enable the HealthKit capability with background delivery. Apple lists HealthKit as
   available to free accounts (Personal Team) too
   ([supported capabilities](https://developer.apple.com/help/account/reference/supported-capabilities-ios));
   free signing lasts 7 days. Not yet checked: whether a free profile includes background delivery.
2. Distribute to family phones with ad hoc or TestFlight (see docs/design.md 3.7; TestFlight builds stop
   after 90 days, and the daily heartbeat makes that visible in `hka status`).

### Checks that need a real device

These cannot be verified in the simulator or on a Mac:

- Secure Enclave signing works in the background while the phone is locked (key created with
  `AfterFirstUnlockThisDeviceOnly`).
- Background uploads continue after the app is suspended, and outbox files are readable while locked.
- HealthKit observer wakes and background delivery actually trigger sealing (only while unlocked).
- A full-history backfill on a phone with years of data: duration, battery, batch sizes.
- A sample deleted in Health disappears from its day folder after `hka run`.
- Adding a second processor and "Re-send all history" gives it the older data.
- Reinstalling the app makes it a new device (the old key is deleted on first launch).
