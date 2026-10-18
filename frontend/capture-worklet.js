// The graph resamples microphone input to 16 kHz. Outputs remain silent.
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = new Float32Array(4096);
    this.used = 0;
    this.frames = 0;
    this.done = false;
    this.port.onmessage = () => this.flush(true);
  }
  flush(done) {
    if (this.done) return;
    const samples = this.chunk.slice(0, this.used);
    this.port.postMessage({ samples, done }, [samples.buffer]);
    this.used = 0;
    this.done = done;
  }
  process(inputs) {
    if (this.done) return false;
    const channels = inputs[0];
    if (!channels?.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let sample = 0;
      for (const channel of channels) sample += channel[i] / channels.length;
      this.chunk[this.used++] = sample;
      this.frames++;
      if (this.frames >= 240000) {
        this.flush(true);
        return false;
      }
      if (this.used === this.chunk.length) this.flush(false);
    }
    return true;
  }
}
registerProcessor("lf2s-pcm-capture", PcmCapture);
