import BackgroundTasks
import Foundation
import HKACore
import HealthKit
import UIKit

/// Background work: HealthKit observer wakes, a periodic refresh task, and background uploads.
/// HealthKit is unreadable while the phone is locked, so wakes then only re-enqueue uploads
/// (spec 4.3 and the design's iOS rules).
final class AppDelegate: NSObject, UIApplicationDelegate {
    static let refreshTask = "app.hka.refresh"
    @MainActor var observerQueries: [HKObserverQuery] = []
    @MainActor var observing = false

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: Self.refreshTask, using: nil) { task in
            self.handleRefresh(task as! BGAppRefreshTask)
        }
        _ = BackgroundUploader.shared  // reconnect to the background session after a relaunch
        Task { @MainActor in
            await Self.startHost()
            self.startObserving()
            await BackgroundUploader.shared.enqueuePending()
            self.scheduleRefresh()
        }
        return true
    }

    @MainActor
    static func startHost() async {
        try? await DeviceHost.shared.start(protectedDataAvailable: UIApplication.shared.isProtectedDataAvailable)
    }

    func applicationProtectedDataDidBecomeAvailable(_ application: UIApplication) {
        // A launch before the first unlock could not start; start now.
        Task { @MainActor in
            await Self.startHost()
            self.startObserving()
            await Self.wakeSync()
        }
    }

    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        BackgroundUploader.shared.systemCompletion = completionHandler
    }

    // MARK: HealthKit observers

    /// Register observer queries once; also called right after enrolment.
    @MainActor
    func startObserving() {
        guard !observing else { return }
        observing = true  // set before any await so concurrent calls cannot both register
        Task { @MainActor in
            // Not enrolled or not started yet: keep the flag so a concurrent call does not race this one,
            // and register once the host has a device (bounded retry; enrolment calls this again).
            var attempts = 0
            while await DeviceHost.shared.state() == nil {
                attempts += 1
                if attempts > 30 { observing = false; return }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
            let store = DeviceHost.shared.health
            for id in HealthCatalog.all {
                guard let type = HealthCatalog.sampleType(id) else { continue }
                let q = HKObserverQuery(sampleType: type, predicate: nil) { _, completion, error in
                    // Answer HealthKit at once (three missed completions stop deliveries) and do the work
                    // under a background-task assertion; concurrent wakes coalesce inside the Device actor.
                    completion()
                    guard error == nil else { return }
                    Task { @MainActor in
                        var token = UIBackgroundTaskIdentifier.invalid
                        token = UIApplication.shared.beginBackgroundTask(withName: "hka.observer") {
                            // Out of time: end the assertion so iOS does not kill the app; sealed work is safe.
                            UIApplication.shared.endBackgroundTask(token)
                            token = .invalid
                        }
                        await Self.wakeSync()
                        if token != .invalid { UIApplication.shared.endBackgroundTask(token) }
                    }
                }
                store.execute(q)
                observerQueries.append(q)
                // Many types are capped at hourly by the system whatever is asked.
                store.enableBackgroundDelivery(for: type, frequency: .immediate) { _, _ in }
            }
        }
    }

    @MainActor
    static func wakeSync() async {
        let readable = UIApplication.shared.isProtectedDataAvailable
        try? await DeviceHost.shared.sealOnly(readable: readable)
        await BackgroundUploader.shared.enqueuePending()
    }

    // MARK: refresh task

    func scheduleRefresh() {
        let request = BGAppRefreshTaskRequest(identifier: Self.refreshTask)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 4 * 3600)
        try? BGTaskScheduler.shared.submit(request)
    }

    func handleRefresh(_ task: BGAppRefreshTask) {
        scheduleRefresh()
        let done = Completion(task)
        let work = Task { @MainActor in
            await Self.startHost()
            await DeviceHost.shared.checkRelay()
            await Self.wakeSync()
            done.finish(success: true)
        }
        task.expirationHandler = {
            work.cancel()
            done.finish(success: false)  // the system kills apps that never complete an expired task
        }
    }

    /// Calls setTaskCompleted exactly once.
    final class Completion {
        let task: BGTask
        let lock = NSLock()
        var finished = false
        init(_ task: BGTask) { self.task = task }
        func finish(success: Bool) {
            lock.lock(); defer { lock.unlock() }
            guard !finished else { return }
            finished = true
            task.setTaskCompleted(success: success)
        }
    }
}

/// Uploads sealed batches with a background URLSession, so they continue after the app is suspended.
/// Headers are fixed when a task is created; batch uploads need no timestamp (spec 4.3).
final class BackgroundUploader: NSObject, URLSessionDataDelegate {
    static let shared = BackgroundUploader()
    static let identifier = "app.hka.upload"
    var systemCompletion: (() -> Void)?
    private var bodies: [String: Data] = [:]
    private var inFlight: Set<String> = []
    private let lock = NSLock()

    lazy var session: URLSession = {
        let config = URLSessionConfiguration.background(withIdentifier: Self.identifier)
        config.sessionSendsLaunchEvents = true
        config.isDiscretionary = false
        return URLSession(configuration: config, delegate: self, delegateQueue: nil)
    }()

    override init() {
        super.init()
        _ = session
    }

    /// Tasks are tagged "<device_id>:<seq>" so a result can only be applied to the device that sealed it.
    static func tag(_ deviceID: String, _ seq: Int) -> String { "\(deviceID):\(seq)" }
    static func parse(_ tag: String?) -> (deviceID: String, seq: Int)? {
        guard let parts = tag?.split(separator: ":"), parts.count == 2, let seq = Int(parts[1]) else { return nil }
        return (String(parts[0]), seq)
    }

    /// Enqueue every ready outbox entry that is not already uploading. A user force-quit cancels
    /// background tasks, so this runs on every launch.
    func enqueuePending() async {
        guard let deviceID = await DeviceHost.shared.deviceID else { return }
        let ready = await DeviceHost.shared.pendingUploads()
        let running = Set(await session.allTasks.compactMap(\.taskDescription))
        for item in ready {
            let tag = Self.tag(deviceID, item.seq)
            if running.contains(tag) || !claim(tag) { continue }
            let task = session.uploadTask(with: item.request, fromFile: item.file)
            task.taskDescription = tag
            task.resume()
        }
    }

    func cancelAll() async {
        for task in await session.allTasks { task.cancel() }
        clear()
    }

    /// Mark a task as in flight; false if it already is. (Synchronous: locks are not held across awaits.)
    private func claim(_ tag: String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return inFlight.insert(tag).inserted
    }

    private func clear() {
        lock.lock(); defer { lock.unlock() }
        inFlight.removeAll(); bodies.removeAll()
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        guard let tag = dataTask.taskDescription else { return }
        lock.lock(); bodies[tag, default: Data()].append(data); lock.unlock()
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let tag = task.taskDescription, let (deviceID, seq) = Self.parse(tag) else { return }
        lock.lock()
        let body = bodies.removeValue(forKey: tag) ?? Data()
        inFlight.remove(tag)
        lock.unlock()
        let status = (task.response as? HTTPURLResponse)?.statusCode ?? 0
        if (error as? URLError)?.code == .cancelled { return }
        Task { @MainActor in
            // After a background relaunch the host may not be started yet.
            await AppDelegate.startHost()
            if error == nil && status == 200 {
                try? await DeviceHost.shared.acknowledge(seq, deviceID: deviceID)
            } else {
                let e = error.map { RelayError.transport($0) } ?? RelayError.from(status: status, body: body)
                try? await DeviceHost.shared.uploadFailed(seq, deviceID: deviceID, error: e)
            }
        }
    }

    func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        DispatchQueue.main.async {
            self.systemCompletion?()
            self.systemCompletion = nil
        }
    }
}
