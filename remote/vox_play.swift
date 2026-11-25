// vox_play: 从标准输入读 16 位单声道 PCM，写到指定的 macOS 输出设备（一般是 BlackHole 之类的虚拟声卡）。
//   vox_play --list                              列出声卡（JSON）
//   vox_play --device "BlackHole 2ch" --rate 48000
// 找不到指定设备就报错退出，绝不改用默认扬声器。输入断开或 10 秒没有数据就退出，不会残留进程。
import AVFoundation
import CoreAudio
import Foundation

func say(_ s: String) { print(s); fflush(stdout) }
func die(_ s: String) -> Never { say("ERR " + s); exit(1) }

// ---------- 设备 ----------
func address(_ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: 0)
}
func text(_ id: AudioDeviceID, _ sel: AudioObjectPropertySelector) -> String {
    var a = address(sel)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &size, &value) == noErr, let v = value else { return "" }
    return v.takeRetainedValue() as String
}
func number(_ id: AudioDeviceID, _ sel: AudioObjectPropertySelector) -> UInt32 {
    var a = address(sel)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    return AudioObjectGetPropertyData(id, &a, 0, nil, &size, &value) == noErr ? value : 0
}
func channels(_ id: AudioDeviceID, _ scope: AudioObjectPropertyScope) -> Int {
    var a = address(kAudioDevicePropertyStreamConfiguration, scope)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &a, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(id, &a, 0, nil, &size, raw) == noErr else { return 0 }
    return UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self)).reduce(0) { $0 + Int($1.mNumberChannels) }
}
struct Device { let id: AudioDeviceID; let name: String; let uid: String; let outputs: Int; let inputs: Int; let virtual: Bool }
func devices() -> [Device] {
    var a = address(kAudioHardwarePropertyDevices)
    var size: UInt32 = 0
    let system = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyDataSize(system, &a, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
    guard AudioObjectGetPropertyData(system, &a, 0, nil, &size, &ids) == noErr else { return [] }
    return ids.map {
        Device(id: $0, name: text($0, kAudioObjectPropertyName), uid: text($0, kAudioDevicePropertyDeviceUID),
               outputs: channels($0, kAudioObjectPropertyScopeOutput), inputs: channels($0, kAudioObjectPropertyScopeInput),
               virtual: number($0, kAudioDevicePropertyTransportType) == kAudioDeviceTransportTypeVirtual)
    }
}

// ---------- 抖动缓冲 ----------
final class Ring {
    private var data: [Float]
    private var head = 0, count = 0, priming = true
    private let lock = NSLock()
    private let target: Int, limit: Int
    var underruns = 0, dropped = 0, rendered = 0
    init(rate: Double, targetMs: Double, limitMs: Double) {
        data = [Float](repeating: 0, count: Int(rate * 2))
        target = Int(rate * targetMs / 1000)
        limit = Int(rate * limitMs / 1000)
    }
    var buffered: Int { lock.lock(); defer { lock.unlock() }; return count }
    func push(_ samples: [Float]) {
        lock.lock(); defer { lock.unlock() }
        // 两台机器时钟略有快慢：积压偏多时优先丢静音段，听不出来
        if count > target * 5 / 2 && !samples.contains(where: { abs($0) > 0.002 }) { dropped += samples.count; return }
        for s in samples {
            if count == data.count { head = (head + 1) % data.count; count -= 1; dropped += 1 }
            data[(head + count) % data.count] = s
            count += 1
        }
        if count > limit { // 网络卡顿后一次涌进来很多：扔掉旧的，保住即时性
            let cut = count - target
            head = (head + cut) % data.count; count -= cut; dropped += cut
        }
    }
    func pull(_ out: UnsafeMutablePointer<Float>, _ n: Int) {
        lock.lock(); defer { lock.unlock() }
        rendered += n
        if priming && count < target { for i in 0..<n { out[i] = 0 }; return }
        priming = false
        let m = min(n, count)
        for i in 0..<m { out[i] = data[(head + i) % data.count] }
        head = (head + m) % data.count; count -= m
        if m < n {
            for i in m..<n { out[i] = 0 }
            underruns += 1; priming = true
        }
    }
}

// ---------- 入口 ----------
let args = Array(CommandLine.arguments.dropFirst())
func option(_ name: String) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

if args.contains("--list") {
    let list: [[String: Any]] = devices().map { ["name": $0.name, "uid": $0.uid, "outputs": $0.outputs, "inputs": $0.inputs, "virtual": $0.virtual] }
    let json = try! JSONSerialization.data(withJSONObject: list)
    say(String(data: json, encoding: .utf8)!)
    exit(0)
}

guard let wanted = option("--device"), !wanted.isEmpty else { die("缺少 --device") }
guard let rate = Double(option("--rate") ?? ""), rate >= 8000, rate <= 192000 else { die("缺少或不支持的 --rate") }
let outputs = devices().filter { $0.outputs > 0 }
guard let device = outputs.first(where: { $0.uid == wanted || $0.name == wanted })
        ?? outputs.first(where: { $0.name.localizedCaseInsensitiveContains(wanted) }) else {
    die("找不到输出设备“\(wanted)”。现有：" + (outputs.isEmpty ? "（无）" : outputs.map { $0.name }.joined(separator: "、")))
}

let ring = Ring(rate: rate, targetMs: 60, limitMs: 300)
let engine = AVAudioEngine()
var deviceID = device.id
guard let unit = engine.outputNode.audioUnit,
      AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0, &deviceID, UInt32(MemoryLayout<AudioDeviceID>.size)) == noErr
else { die("无法切换到设备“\(device.name)”") }
let format = AVAudioFormat(standardFormatWithSampleRate: rate, channels: 1)!
let source = AVAudioSourceNode(format: format) { _, _, frames, list -> OSStatus in
    let buffers = UnsafeMutableAudioBufferListPointer(list)
    if let out = buffers.first?.mData?.assumingMemoryBound(to: Float.self) { ring.pull(out, Int(frames)) }
    return noErr
}
engine.attach(source)
engine.connect(source, to: engine.mainMixerNode, format: format)
do { try engine.start() } catch { die("无法启动音频输出：\(error.localizedDescription)") }
NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { _ in die("对方的声卡配置发生变化，请重新连接") }
say("READY " + device.name)

var carry: UInt8? = nil
var chunk = [UInt8](repeating: 0, count: 8192)
var lastStat = Date()
while true {
    var fd = pollfd(fd: 0, events: Int16(POLLIN), revents: 0)
    let ready = poll(&fd, 1, 10_000)
    if ready == 0 { die("10 秒没有收到声音数据，已断开") }
    if ready < 0 { if errno == EINTR { continue }; break }
    let n = read(0, &chunk, chunk.count)
    if n <= 0 { break }
    var bytes = Array(chunk[0..<n])
    if let c = carry { bytes.insert(c, at: 0); carry = nil }
    if bytes.count % 2 == 1 { carry = bytes.removeLast() }
    var samples = [Float](repeating: 0, count: bytes.count / 2)
    for i in 0..<samples.count { samples[i] = Float(Int16(bitPattern: UInt16(bytes[2 * i]) | UInt16(bytes[2 * i + 1]) << 8)) / 32768 }
    ring.push(samples)
    if Date().timeIntervalSince(lastStat) >= 2 {
        lastStat = Date()
        say(String(format: "STAT buffer_ms=%.0f played_s=%.1f underruns=%d dropped_ms=%.0f",
                   Double(ring.buffered) / rate * 1000, Double(ring.rendered) / rate, ring.underruns, Double(ring.dropped) / rate * 1000))
    }
}
engine.stop()
