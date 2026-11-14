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

    func testCatalogTypesAllExist() {
        XCTAssertGreaterThan(HealthCatalog.all.count, 150)
        XCTAssertTrue(HealthCatalog.all.contains("HKWorkoutTypeIdentifier"))
        XCTAssertEqual(HealthKitSource.activityName(.pickleball), "pickleball")
    }
}
#endif
