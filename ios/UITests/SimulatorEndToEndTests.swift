import XCTest

/// Driven by cli/tests/test_e2e_simulator.py, which starts the relay and the processor and passes the invite
/// and card through the test runner's environment (xcodebuild TEST_RUNNER_HKA_E2E_*). Run alone, it skips.
///
/// 1. testSeedEnrolAndBackUp: the app writes a known day into the Simulator's Health store, the test
///    allows Health access in the system sheet, the app enrols, reads HealthKit and uploads.
/// 2. testDeleteWeighInAndBackUp: the seeded weigh-in is deleted in Health and backed up with Back Up Now.
final class SimulatorEndToEndTests: XCTestCase {
    let env = ProcessInfo.processInfo.environment

    override func setUp() {
        continueAfterFailure = false
    }

    func app(_ extra: [String: String]) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments += ["-AppleLanguages", "(en)", "-AppleLocale", "en_US"]
        app.launchEnvironment = extra
        return app
    }

    func testSeedEnrolAndBackUp() throws {
        guard let invite = env["HKA_E2E_INVITE"], let card = env["HKA_E2E_CARD"], let out = env["HKA_E2E_OUT"] else {
            throw XCTSkip("run through cli/tests/test_e2e_simulator.py")
        }
        let app = app(["HKA_DEBUG_SEED": "1", "HKA_DEBUG_INVITE": invite, "HKA_DEBUG_CARD": card])
        app.launch()
        allowHealthAccess(app)
        waitForBackedUp(app)

        app.buttons["backupDetails"].tap()
        let fingerprint = app.staticTexts["deviceFingerprint"]
        for _ in 0..<6 where !fingerprint.isHittable { app.swipeUp() }
        XCTAssertTrue(fingerprint.exists, "fingerprint row not found")
        try fingerprint.label.write(to: URL(fileURLWithPath: out).appendingPathComponent("fingerprint.txt"),
                                    atomically: true, encoding: .utf8)
    }

    func testDeleteWeighInAndBackUp() throws {
        guard env["HKA_E2E_OUT"] != nil else { throw XCTSkip("run through cli/tests/test_e2e_simulator.py") }
        let app = app(["HKA_DEBUG_SEED": "delete-weight"])
        app.launch()
        allowHealthAccess(app, required: false)
        let status = app.descendants(matching: .any)["backupStatus"]
        XCTAssertTrue(status.waitForExistence(timeout: 60))
        app.buttons["Back Up Now"].tap()
        // The header leaves "Backed up" while the pass runs (it can be too quick to see), then returns.
        _ = wait(status, label: { !$0.hasPrefix("Backed up") }, timeout: 5)
        waitForBackedUp(app)
    }

    // MARK: helpers

    /// The HealthKit permission sheet: "Turn On All", then "Allow".
    func allowHealthAccess(_ app: XCUIApplication, required: Bool = true) {
        let turnOnAll = app.descendants(matching: .any)["Turn On All"]
        guard turnOnAll.waitForExistence(timeout: required ? 60 : 8) else {
            if required { XCTFail("Health permission sheet did not appear") }
            return
        }
        turnOnAll.tap()
        let allow = app.buttons["Allow"]
        XCTAssertTrue(allow.waitForExistence(timeout: 10))
        allow.tap()
    }

    func waitForBackedUp(_ app: XCUIApplication) {
        let status = app.descendants(matching: .any)["backupStatus"]
        XCTAssertTrue(status.waitForExistence(timeout: 180), "home screen did not appear")
        XCTAssertTrue(wait(status, label: { $0.hasPrefix("Backed up") }, timeout: 180),
                      "status stayed: \(status.label)")
    }

    func wait(_ element: XCUIElement, label matches: @escaping (String) -> Bool, timeout: TimeInterval) -> Bool {
        let predicate = NSPredicate { object, _ in
            guard let e = object as? XCUIElement, e.exists else { return false }
            return matches(e.label)
        }
        return XCTWaiter().wait(for: [expectation(for: predicate, evaluatedWith: element)], timeout: timeout) == .completed
    }
}
