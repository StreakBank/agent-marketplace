// ui-probe's input agent for the iOS Simulator.
//
// One UI test that serves commands on a loopback socket for the host that runs a scenario. It names
// no app: the app under test, the port, a token and an idle limit come from the environment. The
// host sets them as TEST_RUNNER_UI_PROBE_AGENT_PORT, _TOKEN, _BUNDLE and _IDLE_S; xcodebuild passes
// them on to the test without the TEST_RUNNER_ prefix.
//
// Input goes through the system app: taps and drags at fractions of the display. An accessibility
// read of an app can change how it behaves at rest (measured: after one label lookup an app kept
// using CPU at rest for the rest of its life), and so can a text field that has had focus. So the
// agent has two modes, chosen by the host when it starts the agent and fixed for its life:
//   - for a measured run (UI_PROBE_AGENT_LOOKUPS unset): the app under test is never queried for
//     anything but its state, and "find", "type" and "labels" are refused;
//   - for a rehearsal (UI_PROBE_AGENT_LOOKUPS=1): the host drives an app process it will throw away,
//     and may look elements up by their label and type into the field that has keyboard focus.
//     The host launches that process before it starts the agent. XCTest ties its reads to the
//     process that is running when the agent is first asked about the app ("ping" reads its
//     state); a first read of the tree after a relaunch then waits a minute on the old process
//     (measured: 63 s, and a new XCUIApplication for the read changes nothing).
//
// One JSON object per connection, one JSON object back, each on one line. Every command carries the
// token the host passed and a fresh nonce; every reply carries "proof", the HMAC-SHA256 of the nonce
// keyed with the token, so the host can tell a reply of this agent from anything else on the port:
//   {"token": T, "cmd": "ping"}       -> {"ok": true, "app_state": N, "screen": [width, height], "lookups": B}
//   {"token": T, "cmd": "tap", "x": 0.5, "y": 0.5}
//   {"token": T, "cmd": "swipe", "from": [0.5, 0.8], "to": [0.5, 0.3], "ms": 300}
//   {"token": T, "cmd": "quit"}
// and in a rehearsal only:
//   {"token": T, "cmd": "find", "text": S}  or  {"token": T, "cmd": "find", "desc": S}
//       -> {"ok": true, "found": N, "screen": [width, height], "matches": [...]}: the first 50 matches in
//          the order of the tree, each {"frame": [x, y, w, h] (points), "center": [fx, fy] (where to tap,
//          in fractions of the display), "on_screen": B (part of it is on the display), "hittable": B
//          (XCTest's hit test says a tap would reach it; asked for the first ten on the display),
//          "type": N, "label": S} and, as the case may be, "visible" (the part left by the elements it
//          is inside, when the tap had to be moved into it), "under" (the label of an element later in
//          the tree that lies over the tap's point: a hint for a message), "moved", "not_asked"
//   {"token": T, "cmd": "type", "text": S}  -> {"ok": true, "typed": N}
//   {"token": T, "cmd": "labels"}           -> {"ok": true, "truncated": B, "elements": [{"label", "value",
//          "type", "frame", "visible", "depth"}]}; with "all": true every element, labelled or not
// A command with a wrong or missing token, or one that cannot be parsed or performed, is answered
// with {"ok": false, "error": ...} and changes nothing; it never ends the test. An issue XCTest records
// while a command is handled (a gesture it could not perform) makes that command's reply
// {"ok": false, "error": <the issue>}, and the test run still ends failed. For "type" the reply says
// only how many issues there were: what XCTest says of a failed keystroke can name the character. A request must arrive in
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
        lookups = env["UI_PROBE_AGENT_LOOKUPS"] == "1"      // fixed for the life of the agent
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

    // Whether this agent may read the app's accessibility tree and type: a rehearsal only.
    private var lookups = false

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
                    if (obj["cmd"] as? String) == "type" {
                        // What XCTest says of a failed keystroke can name the character: only a count goes on.
                        reply = ["ok": false, "typed": reply["typed"] ?? 0,
                                 "error": "XCTest recorded \(issues.count) issue\(issues.count == 1 ? "" : "s") while typing; "
                                          + "their text is not passed on, since it can name a typed character"]
                    } else {
                        reply = ["ok": false, "error": "XCTest recorded: " + issues.joined(separator: "; ")]
                    }
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
            return ["ok": true, "app_state": Int(app.state.rawValue), "screen": [Double(screen.width), Double(screen.height)],
                    "lookups": lookups]
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
        case "find", "type", "labels":
            guard lookups else {
                throw AgentError(description: "'\(cmd)' is refused: this agent was started for a measured run, in which "
                                 + "the app is never read or typed into; nothing was done")
            }
            switch cmd {
            case "find": return try find(obj, app: app, system: system)
            case "type": return try type(obj, app: app)
            default: return try labels(obj, app: app, system: system)
            }
        default:
            throw AgentError(description: "unknown command '\(cmd)'; known: ping, tap, swipe, quit, and in a rehearsal "
                             + "find, type, labels")
        }
    }

    // ---- a rehearsal only: reads of the app under test ----

    private func rect(_ frame: CGRect) -> [Double] {
        return [Double(frame.minX), Double(frame.minY), Double(frame.width), Double(frame.height)].map {
            $0.isFinite ? ($0 * 10).rounded() / 10 : 0
        }
    }

    /// One element of the app's tree with the part of it that can be seen and the index one past its
    /// own subtree. An element's frame is its whole extent, also where it is scrolled out of the
    /// element that holds it (measured: a row scrolled under a bar keeps its frame, outside the frame
    /// of the scrolling element it is in). So the visible part is the frame cut by the frame of every
    /// element it is inside, and by the display. An enclosing element with an empty frame cuts nothing.
    private struct Seen {
        let node: XCUIElementSnapshot
        let visible: CGRect
        let depth: Int
        var end: Int
    }

    private func flatten(_ root: XCUIElementSnapshot, screen: CGRect) -> [Seen] {
        var nodes: [Seen] = []
        func walk(_ node: XCUIElementSnapshot, _ clip: CGRect, _ depth: Int) {
            let cut = node.frame.intersection(clip)
            let visible = cut.isNull ? CGRect.zero : cut
            let index = nodes.count
            nodes.append(Seen(node: node, visible: visible, depth: depth, end: 0))
            let inner = node.frame.isEmpty ? clip : visible       // what its children are cut by
            for child in node.children {
                walk(child, inner, depth + 1)
            }
            nodes[index].end = nodes.count
        }
        walk(root, screen, 0)
        return nodes
    }

    /// The elements whose label is the needle, in the order of the tree: every one of them, on the
    /// screen or not, for the host to choose from. With `text`, an element whose value is the needle
    /// matches too (a field shows its text as its value). A match's own descendants are not
    /// searched: a control and the text inside it are one target.
    ///
    /// Whether a tap would reach a match is asked of XCTest's own hit test ("hittable"), for the
    /// first matches that are on the display: it knows what is drawn over what (measured on bars at
    /// the top and at the bottom of an app that draws itself: 19 of 20 answers right). Its one wrong
    /// answer was an element cut by the edge of the area it scrolls in, with nothing drawn over the
    /// part that is cut off: the hit test tries the middle of the whole frame. So the point given for
    /// the tap ("center") is moved into the visible part when the middle of the frame is cut off.
    private func find(_ obj: [String: Any], app: XCUIApplication, system: XCUIApplication) throws -> [String: Any] {
        let text = obj["text"] as? String
        let desc = obj["desc"] as? String
        guard (text == nil) != (desc == nil), let needle = text ?? desc, !needle.isEmpty, needle.count <= 500 else {
            throw AgentError(description: "find needs exactly one of 'text' and 'desc', a string of 1 to 500 characters")
        }
        let screen = system.frame
        let nodes = flatten(try app.snapshot(), screen: screen)
        // The matches kept: the outermost of nested ones.
        var kept: [Int] = []
        var index = 0
        while index < nodes.count {
            let node = nodes[index].node
            if node.label == needle || (text != nil && (node.value as? String) == needle) {
                kept.append(index)
                index = nodes[index].end
            } else {
                index += 1
            }
        }
        // The same elements as XCTest hands them out, by their frame: a query lists them in an order
        // of its own (measured: by position, not by the order of the tree), so the frame is what
        // ties an element of the tree to the element the hit test is asked about.
        let predicate = text != nil ? NSPredicate(format: "label == %@ OR value == %@", needle, needle)
                                    : NSPredicate(format: "label == %@", needle)
        var elements: [String: XCUIElement] = [:]
        var listed = 0
        if kept.contains(where: { !nodes[$0].node.frame.intersection(screen).isEmpty }) {
            for element in app.descendants(matching: .any).matching(predicate).allElementsBoundByIndex.prefix(20) {
                listed += 1
                let key = "\(rect(element.frame))"
                if elements[key] == nil {
                    elements[key] = element
                }
            }
        }
        var found: [[String: Any]] = []
        var asked = 0
        for at in kept.prefix(50) {
            let node = nodes[at].node
            let frame = node.frame
            let onDisplay = frame.intersection(screen)
            let onScreen = !frame.isEmpty && !onDisplay.isNull && !onDisplay.isEmpty
            var point = onScreen ? CGPoint(x: onDisplay.midX, y: onDisplay.midY) : CGPoint(x: frame.midX, y: frame.midY)
            let visible = nodes[at].visible
            let cut = onScreen && !visible.isEmpty && !visible.contains(point)
            if cut {
                point = CGPoint(x: visible.midX, y: visible.midY)
            }
            var match: [String: Any] = [
                "frame": rect(frame),
                "center": [screen.width > 0 ? Double(point.x / screen.width) : 0,
                           screen.height > 0 ? Double(point.y / screen.height) : 0],
                "on_screen": onScreen,
                "hittable": false,
                "type": Int(node.elementType.rawValue),
                "label": String(node.label.prefix(200)),
            ]
            if cut {
                match["visible"] = rect(visible)
            }
            if onScreen && asked < 10 {
                asked += 1
                if let element = elements["\(rect(frame))"] {
                    match["hittable"] = element.isHittable
                } else if listed >= 20 {
                    match["not_asked"] = true   // more than twenty elements carry the label
                } else {
                    match["moved"] = true       // the tree changed between the snapshot and the question
                }
            } else if onScreen {
                match["not_asked"] = true       // more than ten matches on the display
            }
            // A hint for the message of a failed lookup, never the verdict: a labelled element that comes
            // after this one in the tree, is not inside it, and lies over the point of the tap.
            if onScreen {
                var later = nodes[at].end
                while later < nodes.count {
                    let other = nodes[later]
                    if !other.node.label.isEmpty && other.node.frame.contains(point) {
                        match["under"] = String(other.node.label.prefix(200))
                    }
                    later += 1
                }
            }
            found.append(match)
        }
        return ["ok": true, "found": kept.count, "matches": found,
                "screen": [Double(screen.width), Double(screen.height)]]
    }

    /// Types into the element that has keyboard focus, one character at a time, finding that
    /// element again for each character: a field made of several boxes moves the focus on as it
    /// fills, and a whole string sent at once can lose or reorder characters.
    private func type(_ obj: [String: Any], app: XCUIApplication) throws -> [String: Any] {
        guard let text = obj["text"] as? String, !text.isEmpty, text.count <= 200 else {
            throw AgentError(description: "type needs 'text', a string of 1 to 200 characters")
        }
        let focused = NSPredicate(format: "hasKeyboardFocus == true")
        var typed = 0
        for ch in text {
            let field = app.descendants(matching: .any).matching(focused).firstMatch
            guard field.waitForExistence(timeout: 5) else {
                throw AgentError(description: "no element has keyboard focus (typed \(typed) of \(text.count) characters); "
                                 + "tap the field first")
            }
            field.typeText(String(ch))
            typed += 1
            Thread.sleep(forTimeInterval: 0.05)
        }
        return ["ok": true, "typed": typed]
    }

    /// Every element of the app that has a label or a value, with the visible part of its frame: what
    /// a failed lookup is shown beside. With "all": true, every element, labelled or not. Each has its
    /// depth in the tree.
    private func labels(_ obj: [String: Any], app: XCUIApplication, system: XCUIApplication) throws -> [String: Any] {
        let all = (obj["all"] as? Bool) == true
        let limit = all ? 3000 : 500
        var elements: [[String: Any]] = []
        var leftOut = false
        for seen in flatten(try app.snapshot(), screen: system.frame) {
            let value = (seen.node.value as? String) ?? ""
            guard all || !seen.node.label.isEmpty || !value.isEmpty else {
                continue
            }
            if elements.count >= limit {
                leftOut = true
                break
            }
            elements.append(["label": String(seen.node.label.prefix(200)), "value": String(value.prefix(200)),
                             "type": Int(seen.node.elementType.rawValue), "frame": rect(seen.node.frame),
                             "visible": rect(seen.visible), "depth": seen.depth])
        }
        return ["ok": true, "elements": elements, "truncated": leftOut]
    }
}
