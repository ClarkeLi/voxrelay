// 把麦克风输入切成固定长度的帧发给主线程。放在 AudioWorklet 里，页面在后台时也照常运行。
class PcmFrames extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.size = options.processorOptions.size;
    this.buf = new Float32Array(this.size);
    this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.buf[this.n++] = ch[i];
      if (this.n === this.size) {
        this.port.postMessage(this.buf.slice());
        this.n = 0;
      }
    }
    return true;
  }
}
registerProcessor("pcm-frames", PcmFrames);

// 把要送去另一台机器的声音切成 20 ms 的 16 位帧。没有声音时也持续发静音，让对方的缓冲保持稳定。
class PcmTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Int16Array(Math.round(sampleRate / 50));
    this.n = 0;
  }
  process(inputs, outputs) {
    const ch = inputs[0] && inputs[0][0];
    const len = ch ? ch.length : outputs[0][0].length;
    for (let i = 0; i < len; i++) {
      this.buf[this.n++] = ch ? Math.max(-32768, Math.min(32767, Math.round(ch[i] * 32767))) : 0;
      if (this.n === this.buf.length) {
        this.port.postMessage(this.buf.slice().buffer);
        this.n = 0;
      }
    }
    return true;
  }
}
registerProcessor("pcm-tap", PcmTap);
