export function encodePcmWav(samples: Float32Array): ArrayBuffer {
  if (samples.length < 3200 || samples.length > 240000)
    throw new Error("请录制 0.2～15 秒普通话语音");
  const output = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(output);
  const word = (offset: number, value: string) => {
    for (let i = 0; i < value.length; i++)
      view.setUint8(offset + i, value.charCodeAt(i));
  };
  word(0, "RIFF");
  view.setUint32(4, output.byteLength - 8, true);
  word(8, "WAVE");
  word(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true);
  view.setUint32(28, 32000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  word(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    if (!Number.isFinite(samples[i])) throw new Error("录音包含无效样本");
    const value = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(
      44 + i * 2,
      Math.round(value * (value < 0 ? 32768 : 32767)),
      true,
    );
  }
  return output;
}

export interface PcmRecorder {
  finished: Promise<ArrayBuffer>;
  stop(): void;
  cancel(): void;
}

export async function recordPcm(signal: AbortSignal): Promise<PcmRecorder> {
  const pending = navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    video: false,
  });
  const stream = await pending;
  if (signal.aborted) {
    stream.getTracks().forEach((track) => track.stop());
    throw new DOMException("录音已取消", "AbortError");
  }
  let context: AudioContext | undefined;
  try {
    context = new AudioContext({ sampleRate: 16000 });
    if (context.sampleRate !== 16000)
      throw new Error("当前音频设备不能创建 16kHz 录音");
    await context.audioWorklet.addModule(
      new URL("./capture-worklet.js", import.meta.url).href,
    );
    if (signal.aborted) throw new DOMException("录音已取消", "AbortError");
    const source = context.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(context, "lf2s-pcm-capture", {
      channelCount: 1,
      channelCountMode: "explicit",
    });
    const chunks: Float32Array[] = [];
    let count = 0;
    let completed = false;
    let timer: ReturnType<typeof setTimeout>;
    let finishTimer: ReturnType<typeof setTimeout> | undefined;
    let resolve!: (value: ArrayBuffer) => void;
    let reject!: (reason: Error) => void;
    const finished = new Promise<ArrayBuffer>((yes, no) => {
      resolve = yes;
      reject = no;
    });
    const cleanup = () => {
      clearTimeout(timer);
      clearTimeout(finishTimer);
      signal.removeEventListener("abort", cancel);
      stream.getTracks().forEach((track) => track.stop());
      source.disconnect();
      node.disconnect();
      node.port.close();
      void context?.close().catch(() => {});
    };
    const fail = (error: Error) => {
      if (completed) return;
      completed = true;
      cleanup();
      reject(error);
    };
    const cancel = () => fail(new DOMException("录音已取消", "AbortError"));
    const stop = () => {
      if (completed || finishTimer) return;
      node.port.postMessage("stop");
      finishTimer = setTimeout(
        () => fail(new Error("录音结束超时，请重新录制")),
        1500,
      );
    };
    signal.addEventListener("abort", cancel, { once: true });
    node.onprocessorerror = () => fail(new Error("音频采集失败，请重新录制"));
    node.port.onmessage = (
      event: MessageEvent<{ samples: Float32Array; done: boolean }>,
    ) => {
      if (completed) return;
      const data = event.data;
      if (
        !(data.samples instanceof Float32Array) ||
        count + data.samples.length > 240000
      )
        return fail(new Error("录音超过大小上限"));
      chunks.push(data.samples);
      count += data.samples.length;
      if (data.done) {
        completed = true;
        cleanup();
        try {
          const samples = new Float32Array(count);
          let offset = 0;
          for (const chunk of chunks) {
            samples.set(chunk, offset);
            offset += chunk.length;
          }
          resolve(encodePcmWav(samples));
        } catch (error) {
          reject(error as Error);
        }
      }
    };
    source.connect(node);
    node.connect(context.destination);
    await context.resume();
    timer = setTimeout(stop, 15000);
    return { finished, stop, cancel };
  } catch (error) {
    stream.getTracks().forEach((track) => track.stop());
    await context?.close().catch(() => {});
    throw error;
  }
}
