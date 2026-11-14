#if canImport(HealthKit)
import CryptoKit
import Foundation
import HealthKit

/// The catalog of types the app asks to read. Unknown identifiers on older systems are skipped.
public enum HealthCatalog {
    public static let quantityIDs: [String] = [
        // activity
        "StepCount", "DistanceWalkingRunning", "DistanceCycling", "DistanceSwimming", "DistanceWheelchair",
        "DistanceDownhillSnowSports", "DistanceCrossCountrySkiing", "DistancePaddleSports", "DistanceRowing",
        "DistanceSkatingSports", "PushCount", "SwimmingStrokeCount", "FlightsClimbed", "NikeFuel",
        "ActiveEnergyBurned", "BasalEnergyBurned", "AppleExerciseTime", "AppleMoveTime", "AppleStandTime",
        "PhysicalEffort", "CyclingCadence", "CyclingPower", "CyclingSpeed", "CyclingFunctionalThresholdPower",
        "RunningSpeed", "RunningPower", "RunningStrideLength", "RunningVerticalOscillation", "RunningGroundContactTime",
        "CrossCountrySkiingSpeed", "PaddleSportsSpeed", "RowingSpeed", "WorkoutEffortScore", "EstimatedWorkoutEffortScore",
        // body
        "Height", "BodyMass", "BodyMassIndex", "LeanBodyMass", "BodyFatPercentage", "WaistCircumference",
        "AppleSleepingWristTemperature",
        // vitals
        "HeartRate", "RestingHeartRate", "WalkingHeartRateAverage", "HeartRateVariabilitySDNN",
        "HeartRateRecoveryOneMinute", "AtrialFibrillationBurden", "OxygenSaturation", "BodyTemperature",
        "BasalBodyTemperature", "BloodPressureSystolic", "BloodPressureDiastolic", "RespiratoryRate", "VO2Max",
        "PeripheralPerfusionIndex", "BloodGlucose", "ElectrodermalActivity", "BloodAlcoholContent",
        "ForcedVitalCapacity", "ForcedExpiratoryVolume1", "PeakExpiratoryFlowRate", "InhalerUsage",
        "InsulinDelivery", "NumberOfTimesFallen", "NumberOfAlcoholicBeverages", "AppleSleepingBreathingDisturbances",
        // mobility
        "WalkingSpeed", "WalkingStepLength", "WalkingAsymmetryPercentage", "WalkingDoubleSupportPercentage",
        "StairAscentSpeed", "StairDescentSpeed", "SixMinuteWalkTestDistance", "AppleWalkingSteadiness",
        // hearing, environment
        "EnvironmentalAudioExposure", "HeadphoneAudioExposure", "EnvironmentalSoundReduction", "UVExposure",
        "TimeInDaylight", "UnderwaterDepth", "WaterTemperature",
        // nutrition
        "DietaryEnergyConsumed", "DietaryCarbohydrates", "DietaryProtein", "DietaryFatTotal", "DietaryFatSaturated",
        "DietaryFatMonounsaturated", "DietaryFatPolyunsaturated", "DietaryCholesterol", "DietarySodium",
        "DietaryFiber", "DietarySugar", "DietaryWater", "DietaryCaffeine", "DietaryCalcium", "DietaryIron",
        "DietaryPotassium", "DietaryVitaminA", "DietaryVitaminB6", "DietaryVitaminB12", "DietaryVitaminC",
        "DietaryVitaminD", "DietaryVitaminE", "DietaryVitaminK", "DietaryMagnesium", "DietaryZinc",
        "DietaryFolate", "DietaryNiacin", "DietaryRiboflavin", "DietaryThiamin", "DietaryPhosphorus",
        "DietarySelenium", "DietaryCopper", "DietaryManganese", "DietaryChromium", "DietaryMolybdenum",
        "DietaryChloride", "DietaryIodine", "DietaryBiotin", "DietaryPantothenicAcid",
    ].map { "HKQuantityTypeIdentifier" + $0 }

    public static let categoryIDs: [String] = [
        "SleepAnalysis", "MindfulSession", "AppleStandHour", "HighHeartRateEvent", "LowHeartRateEvent",
        "IrregularHeartRhythmEvent", "LowCardioFitnessEvent", "AppleWalkingSteadinessEvent", "HeadphoneAudioExposureEvent",
        "EnvironmentalAudioExposureEvent", "HandwashingEvent", "ToothbrushingEvent", "MenstrualFlow",
        "IntermenstrualBleeding", "OvulationTestResult", "CervicalMucusQuality", "SexualActivity", "PregnancyTestResult",
        "ProgesteroneTestResult", "Contraceptive", "Lactation", "Pregnancy", "InfrequentMenstrualCycles",
        "IrregularMenstrualCycles", "PersistentIntermenstrualBleeding", "ProlongedMenstrualPeriods",
        "AbdominalCramps", "Acne", "AppetiteChanges", "BladderIncontinence", "Bloating", "BreastPain",
        "ChestTightnessOrPain", "Chills", "Constipation", "Coughing", "Diarrhea", "Dizziness", "DrySkin",
        "Fainting", "Fatigue", "Fever", "GeneralizedBodyAche", "HairLoss", "Headache", "Heartburn", "HotFlashes",
        "LossOfSmell", "LossOfTaste", "LowerBackPain", "MemoryLapse", "MoodChanges", "Nausea", "NightSweats",
        "PelvicPain", "RapidPoundingOrFlutteringHeartbeat", "RunnyNose", "ShortnessOfBreath", "SinusCongestion",
        "SkippedHeartbeat", "SleepChanges", "SoreThroat", "VaginalDryness", "Vomiting", "Wheezing",
        "SleepApneaEvent",
    ].map { "HKCategoryTypeIdentifier" + $0 }

    public static let correlationIDs = ["HKCorrelationTypeIdentifierBloodPressure", "HKCorrelationTypeIdentifierFood"]
    public static let workout = "HKWorkoutTypeIdentifier"
    public static let ecg = "HKDataTypeIdentifierElectrocardiogram"
    public static let heartbeats = "HKDataTypeIdentifierHeartbeatSeries"
    public static let audiogram = "HKDataTypeIdentifierAudiogram"

    public static func sampleType(_ id: String) -> HKSampleType? {
        if id == workout { return HKObjectType.workoutType() }
        if id == ecg { return HKObjectType.electrocardiogramType() }
        if id == heartbeats { return HKSeriesType.heartbeat() }
        if id == audiogram { return HKObjectType.audiogramSampleType() }
        if id.hasPrefix("HKQuantityTypeIdentifier") { return HKQuantityType.quantityType(forIdentifier: .init(rawValue: id)) }
        if id.hasPrefix("HKCategoryTypeIdentifier") { return HKCategoryType.categoryType(forIdentifier: .init(rawValue: id)) }
        if id.hasPrefix("HKCorrelationTypeIdentifier") { return HKCorrelationType.correlationType(forIdentifier: .init(rawValue: id)) }
        return nil
    }

    /// Every type, in sync order, that exists on this system.
    public static var all: [String] {
        (quantityIDs + categoryIDs + correlationIDs + [workout, ecg, heartbeats, audiogram]).filter { sampleType($0) != nil }
    }

    public static var readTypes: Set<HKObjectType> {
        var set = Set<HKObjectType>(all.compactMap { sampleType($0) })
        set.insert(HKSeriesType.workoutRoute())
        for c in [HKCharacteristicTypeIdentifier.dateOfBirth, .biologicalSex, .bloodType, .fitzpatrickSkinType,
                  .wheelchairUse, .activityMoveMode] {
            if let t = HKObjectType.characteristicType(forIdentifier: c) { set.insert(t) }
        }
        return set
    }
}

/// Reads HealthKit and produces records as defined in spec/records.md.
public final class HealthKitSource: HealthSource {
    let store: HKHealthStore
    let types: [String]
    let readable: () -> Bool
    let timeZone: () -> TimeZone
    var units: [HKQuantityType: HKUnit] = [:]

    public init(store: HKHealthStore = HKHealthStore(), types: [String] = HealthCatalog.all,
                readable: @escaping () -> Bool, timeZone: @escaping () -> TimeZone = { .current }) {
        self.store = store; self.types = types; self.readable = readable; self.timeZone = timeZone
    }

    public var isReadable: Bool { readable() }
    public func syncTypes() -> [String] { types }

    public func requestAuthorization() async throws {
        try await store.requestAuthorization(toShare: [], read: HealthCatalog.readTypes)
    }

    /// Preferred units, asked per type so one type that is not authorized does not lose the others.
    func loadUnits() async {
        for t in types.compactMap({ HealthCatalog.sampleType($0) as? HKQuantityType }) where units[t] == nil {
            if let u = try? await store.preferredUnits(for: [t])[t] { units[t] = u }
        }
    }

    func unit(for type: HKQuantityType) -> HKUnit {
        if let u = units[type] { return u }
        for candidate in Self.fallbackUnits where type.is(compatibleWith: candidate) { return candidate }
        return HKUnit.count()
    }

    // MARK: HealthSource

    public func changes(type: String, anchor: Data?, limit: Int) async throws -> HealthPage {
        guard let sampleType = HealthCatalog.sampleType(type) else { return HealthPage(records: [], anchor: anchor, more: false) }
        await loadUnits()
        let previous = anchor.flatMap { try? NSKeyedUnarchiver.unarchivedObject(ofClass: HKQueryAnchor.self, from: $0) }
        let (samples, deleted, newAnchor) = try await anchored(sampleType, anchor: previous, limit: limit)
        var records: [[String: Any]] = []
        for s in samples { records += try await serialize(s) }
        let now = Encoding.instant(Date())
        for d in deleted { records.append(["k": "deleted", "uuid": d.uuid.uuidString, "type": type, "observed_at": now]) }
        let archived = try newAnchor.map { try NSKeyedArchiver.archivedData(withRootObject: $0, requiringSecureCoding: true) }
        return HealthPage(records: records, anchor: archived ?? anchor, more: samples.count + deleted.count >= limit)
    }

    public func recent(type: String, since: Date) async throws -> [[String: Any]] {
        guard let sampleType = HealthCatalog.sampleType(type) else { return [] }
        await loadUnits()
        let predicate = HKQuery.predicateForSamples(withStart: since, end: nil, options: .strictStartDate)
        var records: [[String: Any]] = []
        for s in try await samples(sampleType, predicate: predicate) { records += try await serialize(s) }
        return records
    }

    public func census(days: Int, now: Date) async throws -> [[String: Any]] {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timeZone()
        let today = cal.startOfDay(for: now)
        let start = cal.date(byAdding: .day, value: -days, to: today)!
        let predicate = HKQuery.predicateForSamples(withStart: start, end: today, options: .strictStartDate)
        let df = DateFormatter()
        df.calendar = cal; df.timeZone = cal.timeZone; df.locale = Locale(identifier: "en_US_POSIX"); df.dateFormat = "yyyy-MM-dd"
        var out: [[String: Any]] = []
        for type in types where type != HealthCatalog.audiogram {  // `object` records are not counted
            guard let sampleType = HealthCatalog.sampleType(type) else { continue }
            var byDay: [String: [String]] = [:]
            guard let found = try? await samples(sampleType, predicate: predicate) else { continue }  // not readable
            for s in found {
                byDay[df.string(from: s.startDate), default: []].append(s.uuid.uuidString)
            }
            for (day, uuids) in byDay.sorted(by: { $0.key < $1.key }) {
                let digest = Encoding.sha256Hex(Data(uuids.sorted().joined(separator: "\n").utf8))
                out.append(["k": "census", "type": type, "day": day, "tz": cal.timeZone.identifier,
                            "count": uuids.count, "uuid_sha256": digest])
            }
        }
        return out
    }

    public func profile(now: Date) -> [String: Any]? {
        var p: [String: Any] = ["k": "profile", "observed_at": Encoding.instant(now)]
        if let dob = try? store.dateOfBirthComponents(), let y = dob.year, let m = dob.month, let d = dob.day {
            p["date_of_birth"] = String(format: "%04d-%02d-%02d", y, m, d)
        }
        if let s = try? store.biologicalSex().biologicalSex {
            p["biological_sex"] = ["notSet", "female", "male", "other"][safe: s.rawValue] ?? "value_\(s.rawValue)"
        }
        if let b = try? store.bloodType().bloodType {
            p["blood_type"] = ["notSet", "aPositive", "aNegative", "bPositive", "bNegative", "abPositive", "abNegative",
                               "oPositive", "oNegative"][safe: b.rawValue] ?? "value_\(b.rawValue)"
        }
        if let f = try? store.fitzpatrickSkinType().skinType {
            p["fitzpatrick_skin_type"] = ["notSet", "I", "II", "III", "IV", "V", "VI"][safe: f.rawValue] ?? "value_\(f.rawValue)"
        }
        if let w = try? store.wheelchairUse().wheelchairUse {
            p["wheelchair_use"] = ["notSet", "no", "yes"][safe: w.rawValue] ?? "value_\(w.rawValue)"
        }
        if let a = try? store.activityMoveMode().activityMoveMode {
            p["activity_move_mode"] = a == .activeEnergy ? "activeEnergy" : a == .appleMoveTime ? "appleMoveTime" : "value_\(a.rawValue)"
        }
        return p.count > 2 ? p : nil
    }

    // MARK: queries

    func anchored(_ type: HKSampleType, anchor: HKQueryAnchor?, limit: Int) async throws
        -> ([HKSample], [HKDeletedObject], HKQueryAnchor?) {
        try await withCheckedThrowingContinuation { cont in
            let q = HKAnchoredObjectQuery(type: type, predicate: nil, anchor: anchor, limit: limit) { _, s, d, a, error in
                if let error { cont.resume(throwing: error) } else { cont.resume(returning: (s ?? [], d ?? [], a)) }
            }
            store.execute(q)
        }
    }

    func samples(_ type: HKSampleType, predicate: NSPredicate) async throws -> [HKSample] {
        try await withCheckedThrowingContinuation { cont in
            let sort = NSSortDescriptor(key: HKSampleSortIdentifierStartDate, ascending: true)
            let q = HKSampleQuery(sampleType: type, predicate: predicate, limit: HKObjectQueryNoLimit, sortDescriptors: [sort]) { _, s, error in
                if let error { cont.resume(throwing: error) } else { cont.resume(returning: s ?? []) }
            }
            store.execute(q)
        }
    }

    // MARK: serialization (spec/records.md)

    func common(_ s: HKSample) -> [String: Any] {
        let rev = s.sourceRevision
        var source: [String: Any] = ["bundle_id": rev.source.bundleIdentifier, "name": rev.source.name]
        if let v = rev.version { source["version"] = v }
        if let p = rev.productType { source["product_type"] = p }
        let os = rev.operatingSystemVersion
        source["os_version"] = "\(os.majorVersion).\(os.minorVersion).\(os.patchVersion)"
        var out: [String: Any] = ["uuid": s.uuid.uuidString, "start": Encoding.instant(s.startDate),
                                  "end": Encoding.instant(s.endDate), "source": source,
                                  "metadata": Self.metadata(s.metadata), "type": s.sampleType.identifier]
        if let d = s.device {
            var dev: [String: Any] = [:]
            dev["name"] = d.name; dev["manufacturer"] = d.manufacturer; dev["model"] = d.model
            dev["hardware_version"] = d.hardwareVersion; dev["firmware_version"] = d.firmwareVersion
            dev["software_version"] = d.softwareVersion; dev["local_identifier"] = d.localIdentifier
            dev["udi"] = d.udiDeviceIdentifier
            out["device"] = dev
        } else {
            out["device"] = NSNull()
        }
        return out
    }

    static func metadata(_ m: [String: Any]?) -> [String: Any] {
        var out: [String: Any] = [:]
        for (k, v) in m ?? [:] {
            switch v {
            case let s as String: out[k] = s
            case let d as Date: out[k] = ["date": Encoding.instant(d)]
            case let q as HKQuantity:
                for unit in [HKUnit.count(), .meter(), .second(), .kilocalorie(), .degreeCelsius(), .percent(), .perMinute,
                             .gramUnit(with: .kilo), .decibelAWeightedSoundPressureLevel()] where q.is(compatibleWith: unit) {
                    out[k] = ["quantity": q.doubleValue(for: unit), "unit": unit.unitString]
                    break
                }
                if out[k] == nil { out[k] = ["unsupported": "HKQuantity"] }
            case let n as NSNumber:
                if CFGetTypeID(n) == CFBooleanGetTypeID() { out[k] = n.boolValue }
                else if n.doubleValue.isFinite { out[k] = n }
            default: out[k] = ["unsupported": String(describing: type(of: v))]
            }
        }
        return out
    }

    static let fallbackUnits: [HKUnit] = {
        var units = baseUnits
        if #available(iOS 18.0, macOS 15.0, *) { units.append(.appleEffortScore()) }
        return units
    }()

    static let baseUnits: [HKUnit] = [
        .count(), .perMinute, .meter(), .kilocalorie(), .second(), .percent(), .gramUnit(with: .kilo), .degreeCelsius(),
        .millimeterOfMercury(), .literUnit(with: .milli), .watt(), .decibelAWeightedSoundPressureLevel(),
        .secondUnit(with: .milli), .meter().unitDivided(by: .second()), .siemen(), .hertz(),
        .gramUnit(with: .milli).unitDivided(by: .literUnit(with: .deci)), .internationalUnit(),
        .literUnit(with: .milli).unitDivided(by: .gramUnit(with: .kilo).unitMultiplied(by: .minute())),
        .liter().unitDivided(by: .minute()), .kilocalorie().unitDivided(by: .gramUnit(with: .kilo).unitMultiplied(by: .hour())),
        .lux(),
    ]

    func quantityRecord(_ q: HKQuantitySample) -> [String: Any] {
        var unit = unit(for: q.quantityType)
        if !q.quantity.is(compatibleWith: unit) {  // doubleValue(for:) raises on an incompatible unit
            unit = Self.fallbackUnits.first { q.quantity.is(compatibleWith: $0) } ?? unit
        }
        var r = common(q)
        r["k"] = "sample"; r["kind"] = "quantity"
        guard q.quantity.is(compatibleWith: unit) else {
            r["value"] = NSNull(); r["unit"] = NSNull(); r["unit_error"] = "no compatible unit"
            return r
        }
        r["unit"] = unit.unitString
        let v = q.quantity.doubleValue(for: unit)
        if v.isFinite { r["value"] = v }
        if q.count > 1 { r["count"] = q.count }
        if let d = q as? HKDiscreteQuantitySample, d.count > 1 {
            var stats: [String: Any] = [:]
            for (name, quantity) in [("min", d.minimumQuantity), ("max", d.maximumQuantity), ("avg", d.averageQuantity),
                                     ("most_recent", d.mostRecentQuantity)] {
                let x = quantity.doubleValue(for: unit)
                if x.isFinite { stats[name] = x }
            }
            stats["most_recent_start"] = Encoding.instant(d.mostRecentQuantityDateInterval.start)
            stats["most_recent_end"] = Encoding.instant(d.mostRecentQuantityDateInterval.end)
            r["stats"] = stats
        }
        return r
    }

    func serialize(_ s: HKSample) async throws -> [[String: Any]] {
        switch s {
        case let q as HKQuantitySample:
            return [quantityRecord(q)]
        case let c as HKCategorySample:
            var r = common(c)
            r["k"] = "sample"; r["kind"] = "category"; r["value"] = c.value
            if c.categoryType.identifier == HKCategoryTypeIdentifier.sleepAnalysis.rawValue {
                r["value_name"] = ["inBed", "asleepUnspecified", "awake", "asleepCore", "asleepDeep", "asleepREM"][safe: c.value]
            }
            return [r]
        case let corr as HKCorrelation:
            var r = common(corr)
            r["k"] = "sample"; r["kind"] = "correlation"
            r["objects"] = corr.objects.compactMap { obj -> [String: Any]? in
                guard let q = obj as? HKQuantitySample else { return nil }
                var m = quantityRecord(q)
                m.removeValue(forKey: "k")
                return m
            }
            return [r]
        // A series that cannot be read must not block the type: keep the record, note the error.
        case let w as HKWorkout:
            return try await workoutRecords(w)
        case let e as HKElectrocardiogram:
            do { return try await ecgRecords(e) } catch {
                var r = common(e); r["k"] = "ecg"; r["type"] = HealthCatalog.ecg; r["part"] = 0; r["parts"] = 1
                r["voltages_uv"] = [Any](); r["series_error"] = error.localizedDescription
                return [r]
            }
        case let h as HKHeartbeatSeriesSample:
            do { return try await heartbeatRecords(h) } catch {
                var r = common(h); r["k"] = "heartbeats"; r["type"] = HealthCatalog.heartbeats; r["part"] = 0; r["parts"] = 1
                r["beats"] = [Any](); r["series_error"] = error.localizedDescription
                return [r]
            }
        default:
            var r = common(s)
            r["k"] = "object"; r["class"] = String(describing: type(of: s)); r["data"] = [String: Any]()
            return [r]
        }
    }

    func statsDict(_ all: [HKQuantityType: HKStatistics]) -> [String: Any] {
        var out: [String: Any] = [:]
        for (t, st) in all {
            let unit = unit(for: t)
            var d: [String: Any] = ["unit": unit.unitString]
            for (name, q) in [("sum", st.sumQuantity()), ("avg", st.averageQuantity()), ("min", st.minimumQuantity()),
                              ("max", st.maximumQuantity())] {
                if let q, q.is(compatibleWith: unit) {
                    let x = q.doubleValue(for: unit)
                    if x.isFinite { d[name] = x }
                }
            }
            out[t.identifier] = d
        }
        return out
    }

    func workoutRecords(_ w: HKWorkout) async throws -> [[String: Any]] {
        var r = common(w)
        r["k"] = "workout"; r["type"] = HealthCatalog.workout
        r["activity_type"] = Int(w.workoutActivityType.rawValue)
        r["activity_name"] = Self.activityName(w.workoutActivityType) ?? NSNull()
        r["duration_s"] = w.duration
        r["stats"] = statsDict(w.allStatistics)
        r["events"] = (w.workoutEvents ?? []).map { e -> [String: Any] in
            ["type": Self.eventName(e.type), "start": Encoding.instant(e.dateInterval.start),
             "end": Encoding.instant(e.dateInterval.end), "metadata": Self.metadata(e.metadata)]
        }
        r["activities"] = w.workoutActivities.map { a -> [String: Any] in
            ["uuid": a.uuid.uuidString, "activity_type": Int(a.workoutConfiguration.activityType.rawValue),
             "start": Encoding.instant(a.startDate), "end": Encoding.instant(a.endDate ?? a.startDate),
             "stats": statsDict(a.allStatistics), "metadata": Self.metadata(a.metadata)]
        }
        var routeList: [(HKWorkoutRoute, [[Any]])] = []
        do {
            for route in try await routes(of: w) { routeList.append((route, try await locations(of: route))) }
        } catch {
            r["series_error"] = "routes: \(error.localizedDescription)"
            routeList = []
        }
        var out = [r]
        for (route, points) in routeList {
            let chunks = stride(from: 0, to: max(points.count, 1), by: 5000).map { Array(points[$0..<min($0 + 5000, points.count)]) }
            for (i, chunk) in chunks.enumerated() {
                out.append(["k": "route", "uuid": route.uuid.uuidString, "workout_uuid": w.uuid.uuidString, "part": i,
                            "parts": chunks.count, "start": Encoding.instant(route.startDate),
                            "end": Encoding.instant(route.endDate), "points": chunk])
            }
        }
        return out
    }

    func routes(of w: HKWorkout) async throws -> [HKWorkoutRoute] {
        try await withCheckedThrowingContinuation { cont in
            let q = HKSampleQuery(sampleType: HKSeriesType.workoutRoute(), predicate: HKQuery.predicateForObjects(from: w),
                                  limit: HKObjectQueryNoLimit, sortDescriptors: nil) { _, s, error in
                if let error { cont.resume(throwing: error) } else { cont.resume(returning: (s as? [HKWorkoutRoute]) ?? []) }
            }
            store.execute(q)
        }
    }

    func locations(of route: HKWorkoutRoute) async throws -> [[Any]] {
        try await withCheckedThrowingContinuation { cont in
            var points: [[Any]] = []
            let q = HKWorkoutRouteQuery(route: route) { _, locs, done, error in
                if let error { cont.resume(throwing: error); return }
                for l in locs ?? [] {
                    func opt(_ v: Double, valid: Bool) -> Any { valid && v.isFinite ? v : NSNull() }
                    points.append([Int64((l.timestamp.timeIntervalSince1970 * 1000).rounded(.down)), l.coordinate.latitude,
                                   l.coordinate.longitude, opt(l.altitude, valid: l.verticalAccuracy >= 0),
                                   opt(l.horizontalAccuracy, valid: l.horizontalAccuracy >= 0),
                                   opt(l.verticalAccuracy, valid: l.verticalAccuracy >= 0), opt(l.speed, valid: l.speed >= 0),
                                   opt(l.course, valid: l.course >= 0), opt(l.speedAccuracy, valid: l.speedAccuracy >= 0),
                                   opt(l.courseAccuracy, valid: l.courseAccuracy >= 0)])
                }
                if done { cont.resume(returning: points) }
            }
            store.execute(q)
        }
    }

    func ecgRecords(_ e: HKElectrocardiogram) async throws -> [[String: Any]] {
        let voltages: [Any] = try await withCheckedThrowingContinuation { cont in
            var values: [Any] = []
            let q = HKElectrocardiogramQuery(e) { _, result in
                switch result {
                case .measurement(let m):
                    // Keep one entry per measurement so timing stays aligned; a missing lead is null.
                    let v = m.quantity(for: .appleWatchSimilarToLeadI)?.doubleValue(for: .voltUnit(with: .micro))
                    values.append(v.flatMap { $0.isFinite ? $0 : nil } ?? NSNull())
                case .done: cont.resume(returning: values)
                case .error(let error): cont.resume(throwing: error)
                @unknown default: cont.resume(returning: values)
                }
            }
            store.execute(q)
        }
        var base = common(e)
        base["k"] = "ecg"; base["type"] = HealthCatalog.ecg
        base["classification"] = Self.ecgClassification(e.classification)
        base["avg_hr"] = e.averageHeartRate?.doubleValue(for: .perMinute) ?? NSNull()
        base["sampling_hz"] = e.samplingFrequency?.doubleValue(for: .hertz()) ?? NSNull()
        base["symptoms"] = ["notSet", "none", "present"][safe: e.symptomsStatus.rawValue] ?? "value_\(e.symptomsStatus.rawValue)"
        let chunks = stride(from: 0, to: max(voltages.count, 1), by: 5000).map { Array(voltages[$0..<min($0 + 5000, voltages.count)]) }
        return chunks.enumerated().map { i, chunk in
            var r = base
            r["part"] = i; r["parts"] = chunks.count; r["voltages_uv"] = chunk
            return r
        }
    }

    func heartbeatRecords(_ h: HKHeartbeatSeriesSample) async throws -> [[String: Any]] {
        let beats: [[Any]] = try await withCheckedThrowingContinuation { cont in
            var values: [[Any]] = []
            let q = HKHeartbeatSeriesQuery(heartbeatSeries: h) { _, t, gap, done, error in
                if let error { cont.resume(throwing: error); return }
                values.append([t, gap])
                if done { cont.resume(returning: values) }
            }
            store.execute(q)
        }
        var base = common(h)
        base["k"] = "heartbeats"; base["type"] = HealthCatalog.heartbeats
        let chunks = stride(from: 0, to: max(beats.count, 1), by: 5000).map { Array(beats[$0..<min($0 + 5000, beats.count)]) }
        return chunks.enumerated().map { i, chunk in
            var r = base
            r["part"] = i; r["parts"] = chunks.count; r["beats"] = chunk
            return r
        }
    }

    static func ecgClassification(_ c: HKElectrocardiogram.Classification) -> String {
        switch c {
        case .notSet: return "notSet"
        case .sinusRhythm: return "sinusRhythm"
        case .atrialFibrillation: return "atrialFibrillation"
        case .inconclusiveLowHeartRate: return "inconclusiveLowHeartRate"
        case .inconclusiveHighHeartRate: return "inconclusiveHighHeartRate"
        case .inconclusivePoorReading: return "inconclusivePoorReading"
        case .inconclusiveOther: return "inconclusiveOther"
        case .unrecognized: return "unrecognized"
        @unknown default: return "value_\(c.rawValue)"
        }
    }

    static func eventName(_ t: HKWorkoutEventType) -> String {
        switch t {
        case .pause: return "pause"
        case .resume: return "resume"
        case .lap: return "lap"
        case .marker: return "marker"
        case .motionPaused: return "motionPaused"
        case .motionResumed: return "motionResumed"
        case .segment: return "segment"
        case .pauseOrResumeRequest: return "pauseOrResumeRequest"
        @unknown default: return "unknown_\(t.rawValue)"
        }
    }

    static func activityName(_ t: HKWorkoutActivityType) -> String? {
        let names: [HKWorkoutActivityType: String] = [
            .running: "running", .walking: "walking", .cycling: "cycling", .swimming: "swimming", .hiking: "hiking",
            .pickleball: "pickleball", .tennis: "tennis", .badminton: "badminton", .tableTennis: "tableTennis",
            .squash: "squash", .racquetball: "racquetball", .soccer: "soccer", .basketball: "basketball",
            .volleyball: "volleyball", .yoga: "yoga", .pilates: "pilates", .traditionalStrengthTraining: "traditionalStrengthTraining",
            .functionalStrengthTraining: "functionalStrengthTraining", .highIntensityIntervalTraining: "highIntensityIntervalTraining",
            .rowing: "rowing", .elliptical: "elliptical", .stairClimbing: "stairClimbing", .dance: "dance",
            .coreTraining: "coreTraining", .crossTraining: "crossTraining", .mixedCardio: "mixedCardio",
            .mindAndBody: "mindAndBody", .cooldown: "cooldown", .golf: "golf", .martialArts: "martialArts",
            .swimBikeRun: "swimBikeRun", .other: "other",
        ]
        return names[t]
    }
}

extension HKUnit {
    static let perMinute = HKUnit.count().unitDivided(by: .minute())
}

extension Array {
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}
#endif
