#if canImport(HealthKit)
import HealthKit
import XCTest
@testable import HKACore

final class SerializerTests: XCTestCase {
    let source = HealthKitSource(readable: { true })
    let start = Date(timeIntervalSince1970: 1794556800.1239)

    func roundTrip(_ records: [[String: Any]]) throws -> [[String: Any]] {
        try records.map { try XCTUnwrap(Encoding.parseObject(try Encoding.json($0))) }
    }

    func testQuantitySample() async throws {
        let type = HKQuantityType(.heartRate)
        let sample = HKQuantitySample(type: type, quantity: HKQuantity(unit: .perMinute, doubleValue: 61), start: start, end: start,
                                      metadata: [HKMetadataKeyHeartRateMotionContext: NSNumber(value: 1),
                                                 HKMetadataKeyTimeZone: "Asia/Ho_Chi_Minh",
                                                 "custom_date": start, HKMetadataKeyWasUserEntered: true])
        let r = try roundTrip(try await source.serialize(sample))[0]
        XCTAssertEqual(r["k"] as? String, "sample")
        XCTAssertEqual(r["kind"] as? String, "quantity")
        XCTAssertEqual(r["type"] as? String, "HKQuantityTypeIdentifierHeartRate")
        XCTAssertEqual(r["unit"] as? String, "count/min")
        XCTAssertEqual(r["value"] as? Double, 61)
        XCTAssertEqual(r["start"] as? String, "2026-11-13T08:00:00.123Z")
        XCTAssertEqual(r["uuid"] as? String, sample.uuid.uuidString)
        let meta = r["metadata"] as! [String: Any]
        XCTAssertEqual(meta["HKTimeZone"] as? String, "Asia/Ho_Chi_Minh")
        XCTAssertEqual((meta["custom_date"] as? [String: String])?["date"], "2026-11-13T08:00:00.123Z")
        XCTAssertEqual(meta["HKWasUserEntered"] as? Bool, true)
        XCTAssertNotNil((r["source"] as? [String: Any])?["bundle_id"])
    }

    func testSleepCategoryHasValueName() async throws {
        let sample = HKCategorySample(type: HKCategoryType(.sleepAnalysis), value: HKCategoryValueSleepAnalysis.asleepREM.rawValue,
                                      start: start, end: start.addingTimeInterval(1800))
        let r = try roundTrip(try await source.serialize(sample))[0]
        XCTAssertEqual(r["kind"] as? String, "category")
        XCTAssertEqual(r["value"] as? Int, 5)
        XCTAssertEqual(r["value_name"] as? String, "asleepREM")
    }

    func testBloodPressureCorrelationNestsMembers() async throws {
        let sys = HKQuantitySample(type: HKQuantityType(.bloodPressureSystolic),
                                   quantity: HKQuantity(unit: .millimeterOfMercury(), doubleValue: 120), start: start, end: start)
        let dia = HKQuantitySample(type: HKQuantityType(.bloodPressureDiastolic),
                                   quantity: HKQuantity(unit: .millimeterOfMercury(), doubleValue: 80), start: start, end: start)
        let corr = HKCorrelation(type: HKCorrelationType(.bloodPressure), start: start, end: start, objects: [sys, dia])
        let r = try roundTrip(try await source.serialize(corr))[0]
        XCTAssertEqual(r["kind"] as? String, "correlation")
        let members = r["objects"] as! [[String: Any]]
        XCTAssertEqual(Set(members.compactMap { $0["value"] as? Double }), [120, 80])
        XCTAssertTrue(members.allSatisfy { $0["k"] == nil && $0["unit"] as? String == "mmHg" })
    }

    func testMetadataKeepsEveryQuantity() async throws {
        let sample = HKQuantitySample(type: HKQuantityType(.stepCount), quantity: HKQuantity(unit: .count(), doubleValue: 5),
                                      start: start, end: start,
                                      metadata: ["pressure": HKQuantity(unit: .millimeterOfMercury(), doubleValue: 12),
                                                 "lux": HKQuantity(unit: .lux(), doubleValue: 300)])
        let meta = try roundTrip(try await source.serialize(sample))[0]["metadata"] as! [String: [String: Any]]
        XCTAssertEqual(meta["pressure"]?["unit"] as? String, "mmHg")
        XCTAssertEqual(meta["pressure"]?["quantity"] as? Double, 12)
        XCTAssertEqual(meta["lux"]?["quantity"] as? Double, 300)
    }

    func testAudiogramKeepsThresholds() async throws {
        let dbHL = HKUnit.decibelHearingLevel()
        let point = try HKAudiogramSensitivityPoint(frequency: HKQuantity(unit: .hertz(), doubleValue: 1000),
                                                    leftEarSensitivity: HKQuantity(unit: dbHL, doubleValue: 15),
                                                    rightEarSensitivity: HKQuantity(unit: dbHL, doubleValue: 20))
        let sample = HKAudiogramSample(sensitivityPoints: [point], start: start, end: start, metadata: nil)
        let r = try roundTrip(try await source.serialize(sample))[0]
        XCTAssertEqual(r["k"] as? String, "object")
        XCTAssertEqual(r["type"] as? String, HealthCatalog.audiogram)
        let p = try XCTUnwrap(((r["data"] as? [String: Any])?["sensitivity_points"] as? [[String: Any]])?.first)
        XCTAssertEqual(p["frequency_hz"] as? Double, 1000)
        XCTAssertEqual(p["left_db_hl"] as? Double, 15)
        XCTAssertEqual(p["right_db_hl"] as? Double, 20)
    }

    func testStateOfMindAndAssessments() async throws {
        guard #available(macOS 15.0, *) else { throw XCTSkip("needs macOS 15") }
        let mood = HKStateOfMind(date: start, kind: .dailyMood, valence: 0.4, labels: [.happy, .calm], associations: [.family])
        let m = try roundTrip(try await source.serialize(mood))[0]
        XCTAssertEqual(m["type"] as? String, HealthCatalog.stateOfMind)
        let md = try XCTUnwrap(m["data"] as? [String: Any])
        XCTAssertEqual(md["kind"] as? String, "dailyMood")
        XCTAssertEqual(md["valence"] as? Double, 0.4)
        XCTAssertEqual(md["labels"] as? [Int], [HKStateOfMind.Label.happy.rawValue, HKStateOfMind.Label.calm.rawValue])
        XCTAssertEqual(md["associations"] as? [Int], [HKStateOfMind.Association.family.rawValue])

        let gad = HKGAD7Assessment(date: start, answers: Array(repeating: .severalDays, count: 7))
        let g = try roundTrip(try await source.serialize(gad))[0]
        XCTAssertEqual(g["type"] as? String, HealthCatalog.gad7)
        let gd = try XCTUnwrap(g["data"] as? [String: Any])
        XCTAssertEqual(gd["score"] as? Int, 7)
        XCTAssertEqual((gd["answers"] as? [Int])?.count, 7)
        XCTAssertNotNil(gd["risk"] as? Int)
    }

    func testTypesAreReadOnlyAfterTheMemberWasAsked() {
        let defaults = UserDefaults.standard
        let saved = defaults.object(forKey: HealthCatalog.requestedKey)
        defer { defaults.set(saved, forKey: HealthCatalog.requestedKey) }
        defaults.removeObject(forKey: HealthCatalog.requestedKey)
        XCTAssertEqual(source.syncTypes(), [])
        HealthCatalog.recordRequested(["HKQuantityTypeIdentifierStepCount"])
        XCTAssertEqual(source.syncTypes(), ["HKQuantityTypeIdentifierStepCount"])
        HealthCatalog.recordRequested()
        XCTAssertEqual(source.syncTypes(), HealthCatalog.all)
    }

    func testCatalogTypesAllExist() {
        XCTAssertGreaterThan(HealthCatalog.all.count, 150)
        XCTAssertTrue(HealthCatalog.all.contains("HKWorkoutTypeIdentifier"))
        XCTAssertEqual(HealthKitSource.activityName(.pickleball), "pickleball")
    }
}
#endif
