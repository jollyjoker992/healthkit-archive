#if DEBUG
import CoreLocation
import Foundation
import HealthKit
import HKACore

/// Development and end-to-end testing only (compiled out of release builds): writes a known day of Health
/// data into the Simulator so the real HealthKit read path runs end to end.
///
///   SIMCTL_CHILD_HKA_DEBUG_SEED=1             remove earlier seed data, then write the seed day
///   SIMCTL_CHILD_HKA_DEBUG_SEED=delete-weight delete the seeded weigh-in (deletion propagation)
///
/// The expected values are asserted by cli/tests/test_e2e_simulator.py; keep the two in step.
enum DebugSeed {
    /// The seeded local day. Fixed and in the past, so the test never races midnight.
    static let day = DateComponents(year: 2026, month: 11, day: 10)
    static let marker = "HKA_SEED"  // metadata key on every seeded sample

    static let store = HKHealthStore()

    static func q(_ id: HKQuantityTypeIdentifier) -> HKQuantityType { HKQuantityType(id) }

    static var shareTypes: Set<HKSampleType> {
        let quantities: [HKQuantityTypeIdentifier] = [
            .stepCount, .distanceWalkingRunning, .activeEnergyBurned, .heartRate, .restingHeartRate,
            .heartRateVariabilitySDNN, .oxygenSaturation, .respiratoryRate, .bodyMass, .height,
            .bloodPressureSystolic, .bloodPressureDiastolic, .bloodGlucose, .vo2Max, .dietaryWater,
        ]
        var set = Set<HKSampleType>(quantities.map { q($0) })
        set.formUnion([HKCategoryType(.sleepAnalysis), HKCategoryType(.mindfulSession), HKObjectType.workoutType(),
                       HKSeriesType.workoutRoute()])
        return set
    }

    static func run(_ mode: String) async throws {
        guard HKHealthStore.isHealthDataAvailable() else { throw SeedError("Health data is not available") }
        try await store.requestAuthorization(toShare: shareTypes, read: HealthCatalog.readTypes)
        switch mode {
        case "delete-weight":
            let ours = HKQuery.predicateForObjects(withMetadataKey: marker, allowedValues: ["weight"])
            _ = try await store.deleteObjects(of: q(.bodyMass), predicate: ours)
        default:
            try await removeEarlierSeed()
            try await writeDay()
        }
    }

    /// Seeds from an earlier run would add to the totals; this app is their only source.
    static func removeEarlierSeed() async throws {
        let mine = HKQuery.predicateForObjects(from: HKSource.default())
        for type in shareTypes {
            _ = try? await store.deleteObjects(of: type, predicate: mine)
        }
    }

    static func at(_ hour: Int, _ minute: Int = 0, dayOffset: Int = 0) -> Date {
        var c = day
        c.hour = hour
        c.minute = minute
        let date = Calendar.current.date(from: c)!
        return Calendar.current.date(byAdding: .day, value: dayOffset, to: date)!
    }

    static func meta(_ name: String) -> [String: Any] { [marker: name] }

    static func quantity(_ id: HKQuantityTypeIdentifier, _ value: Double, _ unit: HKUnit, _ start: Date,
                         _ end: Date? = nil, _ name: String = "sample") -> HKQuantitySample {
        HKQuantitySample(type: q(id), quantity: HKQuantity(unit: unit, doubleValue: value), start: start,
                         end: end ?? start, metadata: meta(name))
    }

    static func writeDay() async throws {
        let bpm = HKUnit.count().unitDivided(by: .minute())
        var samples: [HKObject] = [
            quantity(.stepCount, 6000, .count(), at(9), at(10)),
            quantity(.stepCount, 2345, .count(), at(17), at(18)),
            quantity(.distanceWalkingRunning, 5200, .meter(), at(9), at(10)),
            quantity(.activeEnergyBurned, 450, .kilocalorie(), at(9), at(18)),
            quantity(.restingHeartRate, 55, bpm, at(8)),
            quantity(.heartRateVariabilitySDNN, 42, .secondUnit(with: .milli), at(5)),
            quantity(.oxygenSaturation, 0.97, .percent(), at(4)),
            quantity(.respiratoryRate, 14, bpm, at(4)),
            quantity(.bodyMass, 68.5, .gramUnit(with: .kilo), at(7), nil, "weight"),
            quantity(.height, 172, .meterUnit(with: .centi), at(7)),
            quantity(.bloodGlucose, 95, HKUnit(from: "mg/dL"), at(7, 30)),
            quantity(.vo2Max, 42, HKUnit(from: "ml/kg*min"), at(19)),
            quantity(.dietaryWater, 500, .literUnit(with: .milli), at(12)),
        ]
        // Heart rate: one sample a minute 12:00-12:29, 60...89 bpm.
        samples += (0..<30).map { quantity(.heartRate, Double(60 + $0), bpm, at(12, $0)) }
        // Blood pressure 118/76 as a correlation.
        let systolic = quantity(.bloodPressureSystolic, 118, .millimeterOfMercury(), at(7, 15))
        let diastolic = quantity(.bloodPressureDiastolic, 76, .millimeterOfMercury(), at(7, 15))
        samples.append(HKCorrelation(type: HKCorrelationType(.bloodPressure), start: at(7, 15), end: at(7, 15),
                                     objects: [systolic, diastolic], metadata: meta("bp")))
        // The night ending on the seeded day: in bed 22:00-06:30; stages add up to 7 h 45 min asleep.
        let sleep = HKCategoryType(.sleepAnalysis)
        let night: [(HKCategoryValueSleepAnalysis, Date, Date)] = [
            (.inBed, at(22, dayOffset: -1), at(6, 30)),
            (.asleepCore, at(22, 15, dayOffset: -1), at(0)),
            (.asleepDeep, at(0), at(1, 30)),
            (.asleepREM, at(1, 30), at(3)),
            (.asleepCore, at(3), at(6)),
            (.awake, at(6), at(6, 15)),
        ]
        samples += night.map { HKCategorySample(type: sleep, value: $0.0.rawValue, start: $0.1, end: $0.2, metadata: meta("sleep")) }
        samples.append(HKCategorySample(type: HKCategoryType(.mindfulSession), value: HKCategoryValue.notApplicable.rawValue,
                                        start: at(21), end: at(21, 10), metadata: meta("mindful")))
        try await store.save(samples)
        try await workout(.running, from: at(6, 30), to: at(7, 10), route: true, meters: 6000, kcal: 380)
        try await workout(.pickleball, from: at(16), to: at(17, 30), route: false, meters: 3100, kcal: 520)
    }

    /// A workout through HKWorkoutBuilder (statistics computed by HealthKit), with a GPS route for running.
    static func workout(_ activity: HKWorkoutActivityType, from start: Date, to end: Date, route: Bool,
                        meters: Double, kcal: Double) async throws {
        let config = HKWorkoutConfiguration()
        config.activityType = activity
        config.locationType = route ? .outdoor : .indoor
        let builder = HKWorkoutBuilder(healthStore: store, configuration: config, device: .local())
        try await builder.beginCollection(at: start)
        let bpm = HKUnit.count().unitDivided(by: .minute())
        let minutes = Int(end.timeIntervalSince(start) / 60)
        var data: [HKSample] = (0..<minutes).map { i in
            let t = start.addingTimeInterval(Double(i) * 60)
            return quantity(.heartRate, Double(120 + i % 40), bpm, t, nil, "workout-hr")
        }
        data.append(quantity(.distanceWalkingRunning, meters, .meter(), start, end, "workout-distance"))
        data.append(quantity(.activeEnergyBurned, kcal, .kilocalorie(), start, end, "workout-energy"))
        try await builder.addSamples(data)
        try await builder.addMetadata(meta("workout-\(activity.rawValue)"))
        try await builder.endCollection(at: end)
        guard let workout = try await builder.finishWorkout() else { throw SeedError("workout not saved") }
        guard route else { return }
        let routeBuilder = HKWorkoutRouteBuilder(healthStore: store, device: .local())
        // A straight line north from central Ho Chi Minh City, one point every 30 s.
        let points = (0..<Int(end.timeIntervalSince(start) / 30)).map { i in
            CLLocation(coordinate: CLLocationCoordinate2D(latitude: 10.7700 + Double(i) * 0.0006, longitude: 106.7000),
                       altitude: 5, horizontalAccuracy: 5, verticalAccuracy: 3, course: 0, speed: 2.5,
                       timestamp: start.addingTimeInterval(Double(i) * 30))
        }
        try await routeBuilder.insertRouteData(points)
        _ = try await routeBuilder.finishRoute(with: workout, metadata: meta("route"))
    }

    struct SeedError: LocalizedError {
        let message: String
        init(_ message: String) { self.message = message }
        var errorDescription: String? { message }
    }
}
#endif
