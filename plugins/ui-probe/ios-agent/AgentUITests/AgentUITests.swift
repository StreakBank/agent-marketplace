// ui-probe's input agent for the iOS Simulator.
//
// One UI test that serves commands on a loopback socket for the host that runs a scenario. It names
// no app: the app under test, the port, a token and an idle limit come from the environment. The
// host sets them as TEST_RUNNER_UI_PROBE_AGENT_PORT, _TOKEN, _BUNDLE and _IDLE_S; xcodebuild passes
// them on to the test without the TEST_RUNNER_ prefix.
//
// Input goes through the system app: taps and drags at fractions of the display. The app under test
// is never queried for anything but its state. An accessibility read of an app can change how it
// behaves at rest (measured: after one label lookup an app kept using CPU at rest for the rest of
// its life), so this agent has no lookup and no typing.
//
// One JSON object per connection, one JSON object back, each on one line. Every command carries the
// token the host passed and a fresh nonce; every reply carries "proof", the HMAC-SHA256 of the nonce
// keyed with the token, so the host can tell a reply of this agent from anything else on the port:
//   {"token": T, "cmd": "ping"}       -> {"ok": true, "app_state": N, "screen": [width, height]} (points)
//   {"token": T, "cmd": "tap", "x": 0.5, "y": 0.5}
//   {"token": T, "cmd": "swipe", "from": [0.5, 0.8], "to": [0.5, 0.3], "ms": 300}
//   {"token": T, "cmd": "quit"}
// A command with a wrong or missing token, or one that cannot be parsed or performed, is answered
// with {"ok": false, "error": ...} and changes nothing; it never ends the test. An issue XCTest records
// while a command is handled (a gesture it could not perform) makes that command's reply
// {"ok": false, "error": <the issue>}, and the test run still ends failed. A request must arrive in
// full within 5 s of its connection. The test ends after "quit", or by itself once no valid command
// has arrived for UI_PROBE_AGENT_IDLE_S seconds, so a host that was killed cannot leave it running.
import CryptoKit
import Darwin
import XCTest

final class AgentUITests: XCTestCase {
    private struct AgentError: Error, CustomStringConvertible {
        let description: String
    }

    // The issues XCTest recorded while the current command was being handled.
    private var issues: [String] = []

    override func record(_ issue: XCTIssue) {
        issues.append(issue.compactDescription)
        super.record(issue)         // the test run itself still ends failed
    }

    func testServe() throws {
        continueAfterFailure = true
        let env = ProcessInfo.processInfo.environment
        guard let port = UInt16(env["UI_PROBE_AGENT_PORT"] ?? ""), port > 0,
              let token = env["UI_PROBE_AGENT_TOKEN"], token.count >= 16,
              let bundle = env["UI_PROBE_AGENT_BUNDLE"], !bundle.isEmpty,
              let idle = Double(env["UI_PROBE_AGENT_IDLE_S"] ?? ""), idle > 0 else {
            XCTFail("UI_PROBE_AGENT_PORT, UI_PROBE_AGENT_TOKEN (16 characters or more), UI_PROBE_AGENT_BUNDLE and "
                    + "UI_PROBE_AGENT_IDLE_S must be set")
            return
        }
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else {
            XCTFail("socket: errno \(errno)")
            return
        }
        defer { close(fd) }
        var yes: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout<Int32>.size))
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr = in_addr(s_addr: inet_addr("127.0.0.1"))      // loopback only
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) }
        }
        guard bound == 0, listen(fd, 4) == 0 else {
            XCTFail("bind or listen on 127.0.0.1:\(port): errno \(errno)")
            return
        }

        let system = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        let app = XCUIApplication(bundleIdentifier: bundle)
        var lastCommand = Date()
        var running = true
        while running && Date().timeIntervalSince(lastCommand) <= idle {   // the host gone quiet: end by ourselves
            var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            if poll(&pfd, 1, 1000) <= 0 {
                continue
            }
            let conn = accept(fd, nil, nil)
            if conn < 0 {
                continue
            }
            serve(conn, token: token, app: app, system: system, lastCommand: &lastCommand, running: &running)
        }
    }

    private func serve(_ conn: Int32, token: String, app: XCUIApplication, system: XCUIApplication,
                       lastCommand: inout Date, running: inout Bool) {
        defer { close(conn) }
        var one: Int32 = 1
        setsockopt(conn, SOL_SOCKET, SO_NOSIGPIPE, &one, socklen_t(MemoryLayout<Int32>.size))
        // 5 s in all for the request to arrive, however slowly its bytes come: a slow client cannot hold
        // the agent open.
        let deadline = Date().addingTimeInterval(5)
        var data = Data()
        var byte: UInt8 = 0
        reading: while data.count < 65536 {
            let left = deadline.timeIntervalSinceNow
            if left <= 0 {
                break
            }
            var pfd = pollfd(fd: conn, events: Int16(POLLIN), revents: 0)
            if poll(&pfd, 1, Int32(max(1, left * 1000))) <= 0 {
                break
            }
            if read(conn, &byte, 1) != 1 || byte == 10 {
                break reading
            }
            data.append(byte)
        }
        let started = Date()
        var reply: [String: Any]
        if let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            if (obj["token"] as? String) != token {
                reply = ["ok": false, "error": "wrong or missing token; nothing was done"]
            } else if let nonce = obj["nonce"] as? String, !nonce.isEmpty, nonce.count <= 128 {
                lastCommand = Date()
                issues = []
                do {
                    reply = try handle(obj, app: app, system: system, running: &running)
                } catch {
                    reply = ["ok": false, "error": "\(error)"]
                }
                if !issues.isEmpty {
                    reply = ["ok": false, "error": "XCTest recorded: " + issues.joined(separator: "; ")]
                }
                let code = HMAC<SHA256>.authenticationCode(for: Data(nonce.utf8), using: SymmetricKey(data: Data(token.utf8)))
                reply["proof"] = code.map { String(format: "%02x", $0) }.joined()
            } else {
                reply = ["ok": false, "error": "missing nonce; nothing was done"]
            }
        } else {
            reply = ["ok": false, "error": "not a JSON object on one line; nothing was done"]
        }
        reply["elapsed_ms"] = Int(Date().timeIntervalSince(started) * 1000)
        var out = (try? JSONSerialization.data(withJSONObject: reply)) ?? Data("{\"ok\":false,\"error\":\"reply\"}".utf8)
        out.append(10)
        out.withUnsafeBytes { buf in
            _ = write(conn, buf.baseAddress, buf.count)
        }
    }

    // A JSON number, never a boolean (JSONSerialization gives both as NSNumber).
    private func number(_ value: Any?) -> Double? {
        guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else {
            return nil
        }
        let d = n.doubleValue
        return d.isFinite ? d : nil
    }

    private func fraction(_ value: Any?) -> Double? {
        guard let d = number(value), d >= 0, d <= 1 else {
            return nil
        }
        return d
    }

    private func point(_ value: Any?) -> CGVector? {
        guard let a = value as? [Any], a.count == 2, let x = fraction(a[0]), let y = fraction(a[1]) else {
            return nil
        }
        return CGVector(dx: x, dy: y)
    }

    private func handle(_ obj: [String: Any], app: XCUIApplication, system: XCUIApplication,
                        running: inout Bool) throws -> [String: Any] {
        let cmd = obj["cmd"] as? String ?? ""
        switch cmd {
        case "ping":
            // The app's state only (4: running in front): no other read of the app under test.
            let screen = system.frame
            return ["ok": true, "app_state": Int(app.state.rawValue), "screen": [Double(screen.width), Double(screen.height)]]
        case "tap":
            guard let x = fraction(obj["x"]), let y = fraction(obj["y"]) else {
                throw AgentError(description: "tap needs 'x' and 'y' between 0 and 1")
            }
            system.coordinate(withNormalizedOffset: CGVector(dx: x, dy: y)).tap()
            return ["ok": true]
        case "swipe":
            guard let from = point(obj["from"]), let to = point(obj["to"]) else {
                throw AgentError(description: "swipe needs 'from' and 'to' as [x, y] between 0 and 1")
            }
            guard let ms = number(obj["ms"]), ms > 0, ms <= 60000 else {
                throw AgentError(description: "swipe needs 'ms' above 0 and at most 60000")
            }
            let screen = system.frame
            let a = system.coordinate(withNormalizedOffset: from)
            let b = system.coordinate(withNormalizedOffset: to)
            let distance = hypot(Double(to.dx - from.dx) * Double(screen.width), Double(to.dy - from.dy) * Double(screen.height))
            let velocity = XCUIGestureVelocity(rawValue: CGFloat(max(50, distance / max(0.05, ms / 1000))))
            a.press(forDuration: 0.05, thenDragTo: b, withVelocity: velocity, thenHoldForDuration: 0.05)
            return ["ok": true]
        case "quit":
            running = false
            return ["ok": true]
        default:
            throw AgentError(description: "unknown command '\(cmd)'; known: ping, tap, swipe, quit")
        }
    }
}
