// ui-probe phone: record the screen of an iPhone connected to this Mac by cable, the way a Mac's
// movie recording of a phone does. It reads only the picture the phone offers a trusted Mac: it
// installs nothing on the phone, sends it no input, launches nothing, and never attaches the
// phone's sound. It is compiled from this file by `ui-probe phone` on first use and run by it alone.
//
// Usage (by ui-probe, which chooses the device and writes film.json):
//   phone-screen list                       the capture devices the Mac offers, as JSON
//   phone-screen record <seconds> <out.mov>  the same devices as one line {"offered": [...]}, then one
//                                           line on stdin: the id of the device to record (at most
//                                           300 s), or anything else, or the end of input, to stop
//                                           without recording (exit 11, nothing written); while it
//                                           records, the end of its stdin stops the recording (exit 12)
//
// One process holds the phone from the listing to the end of the recording: once a process that saw
// a phone exits, the phone is not offered to the next one for about 11 to 32 s (measured on one iPhone
// and one Mac), so ui-probe never lists in one process and records in another.
//
// macOS asks once for camera access for the program that runs it: a cabled iPhone's screen is
// offered to the Mac as a capture device.

import AVFoundation
import CoreMediaIO
import Foundation

let capSeconds = 300.0          // the hard cap of one recording; nothing raises it

func fail(_ code: Int32, _ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

func emit(_ object: [String: Any]) {
    let data = try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!)
    fflush(stdout)                  // a pipe is block-buffered: ui-probe reads each line as it is written
}

func say(_ message: String) {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
}

// The words ui-probe's phone module tells a phone by, in a device's model or name.
func isPhone(_ device: AVCaptureDevice) -> Bool {
    let text = "\(device.modelID) \(device.localizedName)".lowercased()
    return text.contains("iphone") || text.contains("ipad")
}

// One line from stdin, read a byte at a time and bounded by the clock: nil at the end of input, on an
// error, after `limit` seconds without a whole line, or for a line longer than 4096 bytes.
func readChoice(within limit: Double) -> String? {
    let deadline = Date().addingTimeInterval(limit)
    var line = Data()
    var fds = pollfd(fd: 0, events: Int16(POLLIN), revents: 0)
    while line.count <= 4096 {
        let left = deadline.timeIntervalSinceNow
        if left <= 0 { return nil }
        let ready = poll(&fds, 1, Int32(min(left, 1.0) * 1000) + 1)
        if ready < 0 && errno != EINTR { return nil }
        if ready <= 0 { continue }
        var byte: UInt8 = 0
        if read(0, &byte, 1) != 1 { return nil }
        if byte == 10 { return String(data: line, encoding: .utf8) }
        line.append(byte)
    }
    return nil
}

func allowScreenCaptureDevices() {
    var address = CMIOObjectPropertyAddress(
        mSelector: CMIOObjectPropertySelector(kCMIOHardwarePropertyAllowScreenCaptureDevices),
        mScope: CMIOObjectPropertyScope(kCMIOObjectPropertyScopeGlobal),
        mElement: CMIOObjectPropertyElement(kCMIOObjectPropertyElementMain))
    var allow: UInt32 = 1
    let status = CMIOObjectSetPropertyData(
        CMIOObjectID(kCMIOObjectSystemObject), &address, 0, nil,
        UInt32(MemoryLayout<UInt32>.size), &allow)
    if status != 0 { FileHandle.standardError.write("could not allow screen capture devices: \(status)\n".data(using: .utf8)!) }
}

func screenDevices() -> [AVCaptureDevice] {
    let session = AVCaptureDevice.DiscoverySession(
        deviceTypes: [.external], mediaType: .muxed, position: .unspecified)
    return session.devices
}

func spin(_ seconds: Double) {
    RunLoop.current.run(until: Date().addingTimeInterval(seconds))
}

func utc(_ date: Date) -> String {
    let format = ISO8601DateFormatter()
    format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return format.string(from: date)
}

// sound-check: begin (this block, and spin above, is all a check of the track reading compiles)
final class SoundCheck: @unchecked Sendable {
    var tracks: [String]? = nil     // every track's media type, as its four-character code ("vide")
    var sound = -1                  // the tracks that hold or can hold sound; -1: the film could not be checked
}

// Every track of a finalised movie file, and how many of them hold sound: a track of type audio, a
// muxed track (the combined stream, which can carry sound) and any track with the audible
// characteristic. Other tracks (a timecode track) are listed and hold none. The wait is bounded by the
// clock, not by turns: each turn waits on the semaphore for 50 ms (so time passes even when the run loop
// has no source and returns at once), then turns the run loop; unanswered by the deadline, the film
// could not be checked.
func soundCheck(_ url: URL, limit: Double = 20) -> SoundCheck {
    let result = SoundCheck()
    let done = DispatchSemaphore(value: 0)
    Task {
        defer { done.signal() }
        do {
            var kinds: [String] = []
            var sound = 0
            for track in try await AVURLAsset(url: url).load(.tracks) {
                let characteristics = try await track.load(.mediaCharacteristics)
                kinds.append(track.mediaType.rawValue)
                if track.mediaType == .audio || track.mediaType == .muxed || characteristics.contains(.audible) {
                    sound += 1
                }
            }
            result.tracks = kinds
            result.sound = sound
        } catch {}
    }
    let deadline = Date().addingTimeInterval(limit)
    while done.wait(timeout: .now() + 0.05) == .timedOut {
        if Date() >= deadline { return SoundCheck() }
        spin(0.05)
    }
    return result
}
// sound-check: end

final class Recorder: NSObject, AVCaptureFileOutputRecordingDelegate {
    var finished = false
    var failure: String?
    func fileOutput(_ output: AVCaptureFileOutput, didFinishRecordingTo outputFileURL: URL,
                    from connections: [AVCaptureConnection], error: Error?) {
        if let error = error as NSError? {
            // A recording stopped on purpose still reports "finished successfully" in its info.
            let ok = (error.userInfo[AVErrorRecordingSuccessfullyFinishedKey] as? Bool) ?? false
            if !ok { failure = error.localizedDescription }
        }
        finished = true
    }
}

// The recording's wait: `limit` seconds by the clock, turning the run loop, while watching stdin. ui-probe
// holds stdin open until the recorder has ended, so the end of input means the program that started the
// recording ended (killed, say): true then, at once; false when the time ran out. Bytes are ignored.
func recordFor(_ limit: Double) -> Bool {
    let deadline = Date().addingTimeInterval(limit)
    var fds = pollfd(fd: 0, events: Int16(POLLIN), revents: 0)
    var bytes = [UInt8](repeating: 0, count: 256)
    while Date() < deadline {
        fds.revents = 0
        let ready = poll(&fds, 1, 50)
        if ready < 0 && errno != EINTR { return true }
        if ready > 0 {
            let n = read(0, &bytes, bytes.count)
            if n == 0 || (n < 0 && errno != EINTR && errno != EAGAIN) { return true }
        }
        spin(0.05)
    }
    return false
}

let arguments = CommandLine.arguments
guard arguments.count >= 2, arguments[1] == "list" || arguments[1] == "record" else {
    fail(2, "usage: phone-screen list | record <seconds> <out.mov>")
}
var seconds = 0.0
var url = URL(fileURLWithPath: "/")
if arguments[1] == "record" {
    guard arguments.count == 4, let asked = Double(arguments[2]) else {
        fail(2, "usage: phone-screen record <seconds> <out.mov>")
    }
    guard asked > 0, asked <= capSeconds else { fail(2, "a recording lasts more than 0 and at most \(Int(capSeconds)) s") }
    seconds = asked
    url = URL(fileURLWithPath: arguments[3])
    // An existing output is refused, never replaced.
    guard !FileManager.default.fileExists(atPath: url.path) else { fail(7, "\(url.path) exists; it is not replaced") }
}

allowScreenCaptureDevices()
if AVCaptureDevice.authorizationStatus(for: .video) == .notDetermined {
    var answered = false
    AVCaptureDevice.requestAccess(for: .video) { _ in answered = true }
    let asking = Date().addingTimeInterval(60)
    while !answered && Date() < asking { spin(0.5) }
}
guard AVCaptureDevice.authorizationStatus(for: .video) == .authorized else {
    fail(3, "no camera access: macOS has not allowed this program to read capture devices")
}

// A cabled iPhone appears a moment after screen capture devices are allowed, and only after up to about
// half a minute when another process looked at it a moment ago: `record` waits for a phone up to 60 s,
// by the clock, `list` up to 8 s.
let lookFor = arguments[1] == "record" ? 60.0 : 8.0
let looking = Date()
let againNote = "no phone is offered yet: a phone that another program looked at a moment ago (ui-probe phone " +
    "list, say) is offered again only after up to about half a minute (11 to 32 s, measured on one iPhone and one Mac)"
var devices = screenDevices()
var noted = false
while !devices.contains(where: isPhone) && Date() < looking.addingTimeInterval(lookFor) {
    if !noted && Date() >= looking.addingTimeInterval(3) {
        say(againNote + (arguments[1] == "record" ? "; waiting up to \(Int(lookFor)) s" : ""))
        noted = true
    }
    spin(0.5)
    devices = screenDevices()
}
let found = devices.map { ["id": $0.uniqueID, "name": $0.localizedName, "model": $0.modelID] }

if arguments[1] == "list" {
    emit(["devices": found])
    exit(0)
}

// The same process keeps the devices while ui-probe chooses, writes film.json and posts the notice.
emit(["offered": found])
let choice = readChoice(within: 60)

// Exactly the device ui-probe chose, by its whole id: no fallback to another one. Anything else stops.
let matching = devices.filter { $0.uniqueID == choice }
guard matching.count == 1, let phone = matching.first else {
    fail(11, "no device chosen: nothing was recorded")
}

let session = AVCaptureSession()
var videoPorts: [AVCaptureInput.Port] = []
var offered: [String] = []
var connected = ""
do {
    let input = try AVCaptureDeviceInput(device: phone)
    guard session.canAddInput(input) else { fail(5, "cannot add the phone as an input") }
    // Picture only. The input is added with no connections, and only its video ports are connected, or
    // its muxed ports when it offers no video port (as the recorder that first filmed a phone did); an
    // audio port never: connecting the phone's sound makes the phone treat the Mac as an audio accessory.
    session.addInputWithNoConnections(input)
    let videoOffered = input.ports.filter { $0.mediaType == .video }
    let muxedOffered = input.ports.filter { $0.mediaType == .muxed }
    videoPorts = videoOffered.isEmpty ? muxedOffered : videoOffered
    connected = videoOffered.isEmpty ? "muxed (no video port offered)" : "video"
    offered = input.ports.map { $0.mediaType.rawValue }   // what the phone offers, for film.json
} catch {
    fail(5, "cannot open the phone's screen: \(error.localizedDescription)")
}
let output = AVCaptureMovieFileOutput()
guard session.canAddOutput(output) else { fail(5, "cannot add a movie output") }
session.addOutputWithNoConnections(output)
guard !videoPorts.isEmpty else { fail(5, "the phone offers no picture port (it offers: \(offered.joined(separator: ", ")))") }
let pictureOnly = AVCaptureConnection(inputPorts: videoPorts, output: output)
guard session.canAddConnection(pictureOnly) else { fail(5, "cannot connect the picture alone") }
session.addConnection(pictureOnly)
session.startRunning()
// A phone starts sending its picture a moment after the session starts. Recording before the
// connection is live fails, so wait for the connection made above (a muxed one included).
var ready = 0.0
while ready < 15 {
    spin(0.5); ready += 0.5
    if session.isRunning, pictureOnly.isActive, pictureOnly.isEnabled { break }
}
spin(1.0)

let recorder = Recorder()
let started = Date()
output.startRecording(to: url, recordingDelegate: recorder)
FileHandle.standardError.write("recording \(phone.localizedName) for \(seconds) s\n".data(using: .utf8)!)
let starterEnded = recordFor(seconds)
let recorded = output.recordedDuration.seconds
output.stopRecording()
var settle = 0.0
while !recorder.finished && settle < 10 { spin(0.25); settle += 0.25 }
let stopped = Date()
session.stopRunning()
var summary: [String: Any] = [
    "device": phone.localizedName, "model": phone.modelID, "start_utc": utc(started), "stop_utc": utc(stopped),
    "seconds_recorded": recorded.isFinite ? recorded : -1, "failure": recorder.failure ?? NSNull(), "ports": offered,
    "connected": connected, "tracks": NSNull(), "audio_tracks": NSNull(), "sound_checked": false]
// A film whose end the recorder has not reported may still be being written: it is never checked, and
// the run is never done.
if !recorder.finished {
    summary["failure"] = "the film was not finalised"
    emit(summary)
    exit(10)
}
// After the film is finalised and before anything is reported as done: its tracks, and those that hold sound.
let check = soundCheck(url)
let picture = check.tracks?.contains(AVMediaType.video.rawValue) ?? false
summary["tracks"] = check.tracks ?? NSNull()
summary["audio_tracks"] = check.sound >= 0 ? check.sound as Any : NSNull() as Any
summary["sound_checked"] = check.sound >= 0
if check.sound == 0, !picture, recorder.failure == nil { summary["failure"] = "the film holds no picture track" }
if starterEnded { summary["failure"] = "the program that started the recording ended: the recording was stopped" }
emit(summary)
if check.sound > 0 { exit(8) }              // the film holds sound
if recorder.failure != nil { exit(6) }
if check.sound != 0 || !picture { exit(9) } // the film could not be checked, or holds no picture
if starterEnded { exit(12) }                // stopped early: the program that started it ended
exit(0)
