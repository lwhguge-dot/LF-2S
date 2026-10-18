export interface Sensor {
  valid: boolean;
  value: number | number[] | null;
  age_ms: number | null;
  reason: string | null;
  source: "synthetic" | "unavailable" | "uart1";
  unit: string;
}
export interface WireMessage {
  name: string;
  message_id: number;
  fields: Record<string, unknown>;
}
export interface EventItem {
  id: number;
  time: string;
  kind: string;
  text: string;
  operation_id?: string;
  request_id?: number;
  wire?: WireMessage;
  raw_hex?: string;
}
export interface Operation {
  operation_id: string;
  name: string;
  status: string;
  reason: string | null;
  request_id: number | null;
  protocol_phase?: string;
}
export interface State {
  controller?: ControllerStatus;
  simulation: boolean;
  mode?: "simulation" | "esp32_bench" | "hardware_bench" | "uart_readonly" | "replay";
  gateway?: {active: boolean; host: string | null; local_port: number; remote_port: number};
  bench?: Bench;
  usb_fc?: FcUsb;
  text_tasks?: TextTasks;
  voice?: Voice;
  read_only: boolean;
  hardware_control_allowed?: false;
  task_fixture?: boolean;
  task?: {
    running: boolean;
    unknown_request_id: number | null;
    current: TaskRun | null;
    target_height_mm: number;
  };
  replay?: ReplayFrame | null;
  recording?: Recording;
  vision?: Vision;
  service_ready: boolean;
  service_error?: string;
  service_pid?: number;
  scenario?: string;
  scenarios?: Record<string, string>;
  link?: {
    phase: string;
    reason?: string | null;
    state_age_ms: number | null;
    transport: string;
    ports: number[];
  };
  identity?: {
    boot_id: string | null;
    session_id: string | null;
    client_nonce: string | null;
    expected_dialect_hash: string;
    observed_dialect_hash: string | null;
    protocol_version: string;
    sample_id: number | null;
  };
  sensors?: Record<string, Sensor>;
  qualification?: {
    configuration: string;
    configuration_reason: string;
    control_allowed: false;
    control_reason: string;
    hardware_freshness_qualified: false;
    manual_latched: boolean | null;
    lease_id: number | null;
    task_id: number | null;
  };
  profile?: WireMessage | null;
  events?: EventItem[];
  operations?: Operation[];
  counters?: {
    sent: number;
    received: number;
    rejected: number;
    log_dropped: number;
  };
}
export interface ControllerStatus {
  available: boolean;
  source: "mavlink_control_state" | "unavailable";
  state_age_ms: number | null;
  reason: string | null;
  mode_code: number | null;
  mode_name: string | null;
  failsafe_reason_code: number | null;
  failsafe_reason_name: string | null;
  owner_system: number | null;
  owner_component: number | null;
  route: number | null;
  preflight: boolean | null;
  manual_latched: boolean | null;
  lease_id: number | null;
  active_task: number | null;
}
export interface Capture {
  bytes: Uint8Array;
  frame_id: number;
  stream_id: string;
  synthetic: true;
  hardware_freshness_qualified: false;
  received_at: number;
  service_received_ms: number;
}
export interface TaskRun {
  task_id: number;
  status: string;
  stage: string;
  reason: string | null;
  session_id: string;
  request_id?: number;
  steps: string[];
  queried_result?: { request_id: number; phase: string; reason: string };
}
export interface Bench {
  active: boolean;
  phase: "disconnected" | "connecting" | "online" | "recovering" | "error";
  reason: string | null;
  host: string;
  port: number;
  epoch: number;
  health_age_ms: number | null;
  transport_failures: number;
  recoveries: number;
  camera_busy_responses: number;
  last_error: {
    code: string;
    endpoint: string;
    elapsed_ms: number;
    os_error: number | null;
  } | null;
  health: {
    firmware: string;
    protocol: string;
    dialect_hash: string;
    camera_stream_id: string;
    preview_width: number;
    preview_height: number;
    jpeg_quality: number;
    autofocus_initialized: boolean;
    autofocus_focused: boolean;
    autofocus_busy: boolean;
    autofocus_status: number;
    autofocus_error: number;
    uptime_ms: number;
    psram_bytes: number;
    heap_free_bytes: number;
    camera_failures: number;
    udp_received: number;
    udp_validated: number;
    udp_rejected: number;
    wifi_mode: "ap" | "sta" | null;
    wifi_ipv4: string | null;
    wifi_peer: boolean | null;
    network_epoch: number | null;
    wifi_rssi_dbm: number | null;
    wifi_channel: number | null;
    preview_colour?: number | null;
    test_pattern?: boolean | null;
    sensor_bench_frames?: number | null;
    sensor_bench_us?: number | null;
    capture_wait_us?: number | null;
    send_us?: number | null;
    sensor_diagnostics?: {
      valid: boolean;
      exposure_lines: number;
      gain_q4: number;
      gain_ceiling_q4: number;
      wb_r_q10: number;
      wb_g_q10: number;
      wb_b_q10: number;
      manual_control: number;
      isp_control: number;
      sde_control: number;
      sde_u: number;
      sde_v: number;
      luminance: number;
    } | null;
  } | null;
}
export interface RealCapture {
  bytes: Uint8Array;
  frame_id: number;
  stream_id: string;
  capture_us: string;
  epoch: number;
  width: number;
  height: number;
  synthetic: false;
  display_only: true;
  hardware_freshness_qualified: false;
  received_at: number;
  service_received_ms: number;
  roundtrip_ms: number;
  decode_ms: number;
  camera_wait_ms: number | null;
}
export type VisionCapture =
  | {
      bytes: Uint8Array;
      frame_id: number;
      stream_id: string;
      synthetic: true;
      service_received_ms: number;
    }
  | {
      bytes: Uint8Array;
      frame_id: number;
      stream_id: string;
      synthetic: false;
      service_received_ms: number;
      epoch: number;
      capture_us: string;
    };
export interface UsbMeasurement {
  available: boolean;
  received_age_ms: number | null;
  sample_age_ms: null;
  measurement_qualified: false;
  value: Record<string, number | number[]> | null;
}
export interface FcUsb {
  active: boolean;
  phase: "disconnected" | "connecting" | "online" | "stale" | "error";
  reason: string | null;
  epoch: number;
  source: "vendor_usb_hid";
  display_only: true;
  hardware_control_allowed: false;
  hardware_freshness_qualified: false;
  mavlink_session_established: false;
  receive_age_ms: number | null;
  device: {
    product: string;
    manufacturer: string | null;
    vid: number;
    pid: number;
    device_revision: number;
  } | null;
  attitude: UsbMeasurement;
  imu_raw: UsbMeasurement;
  rc_raw: UsbMeasurement;
  status_raw: UsbMeasurement;
  receiver_link_valid: null;
  battery_v: null;
  height_m: null;
  position_m: null;
  firmware_version: null;
  mcu_model: null;
  boot_id: null;
  session_id: null;
  history: {
    elapsed_s: number;
    roll_deg: number;
    pitch_deg: number;
    yaw_deg: number;
  }[];
  counters: Record<string, number>;
  frames_by_function: Record<string, number>;
  raw_log: {
    enabled: boolean;
    dropped: number;
    error: string | null;
    max_file_bytes: number;
    backup_files: number;
  };
}
export interface Recording {
  id: string;
  started?: string;
  frames?: number;
  bytes: number;
  dropped?: number;
  truncated?: boolean;
  error?: string | null;
  complete: boolean;
}
export interface Voice {
  configured: boolean;
  configuration_error: string | null;
  language: "zh";
  spoken_language: string;
  running: boolean;
  timeout_ms: number;
  max_seconds: number;
  transcript_approved: false;
  permission_granted: false;
  hardware_control_allowed: false;
  current: {
    id: string;
    audio_sha256: string;
    duration_ms: number;
    phase: "pending" | "review" | "empty" | "error";
    transcript: string | null;
    processing_ms: number | null;
    elapsed_ms: number | null;
    error: string | null;
  } | null;
}
export interface TextTasks {
  configured: boolean;
  configuration_error: string | null;
  model: string | null;
  thinking_enabled: false;
  running: boolean;
  executable: false;
  permission_granted: false;
  hardware_control_allowed: false;
  timeout_ms: number;
  current: {
    id: string;
    epoch: number;
    source_text: string;
    source_sha256: string;
    phase: "pending" | "review" | "clarify" | "rejected" | "error";
    provider: "company_model" | "local_validation";
    elapsed_ms: number | null;
    result: {
      status: string;
      proposal: {
        intent: string;
        height_m: number | null;
        hold_s: number | null;
        target: string | null;
        clarification: string | null;
        requires_confirmation: boolean;
      } | null;
      errors: string[];
      questions: string[];
      source_evidence: {
        field: string;
        quote: string;
        start: number;
        end: number;
        value: number | string;
      }[];
      executable: false;
      permission_granted: false;
    } | null;
  } | null;
}
export interface VisionSignal {
  box: [number, number, number, number];
  light: "red" | "yellow" | "green" | "off" | "ambiguous" | "unknown";
}
export interface VisionObservation {
  phase: string;
  reason: string | null;
  frame: {
    source: "synthetic" | "esp32_preview";
    frame_id: string;
    stream_id: string;
    session_id: string;
    reset_seq: number | null;
    source_capture_us: string | null;
    received_ms: number;
    image_sha256: string;
  } | null;
  model_elapsed_ms?: number;
  provider_error?: string;
  current_light: VisionSignal["light"] | null;
  signals: VisionSignal[];
  temporal: {
    evidence: string;
    samples: number;
    span_ms: number;
    green_off_transitions: number;
  } | null;
  zebra_visible: boolean | null;
  checkout_visible: boolean | null;
  currency: {
    scope: string;
    provisional: true;
    reason: string;
    visible_count: number | null;
    recognized_count: number | null;
    recognized_subtotal_fen: number | null;
    visible_total_fen: number | null;
    denomination_breakdown: { denomination_fen: number; count: number }[];
    scene_total_fen: null;
    physical_accuracy_verified: false;
  } | null;
  reported_currency?: {
    visible_notes_complete: boolean | null;
    notes: { box: number[]; denomination_fen: number | null }[];
  };
  crossing_allowed: false;
  hardware_control_allowed: false;
  physical_accuracy_verified: false;
}
export interface Vision {
  configured: boolean;
  configuration_error: string | null;
  model: "qwen38" | null;
  thinking_enabled: false;
  running: boolean;
  timeout_ms: number;
  hardware_control_allowed: false;
  crossing_allowed: false;
  physical_accuracy_verified: false;
  current: {
    request_id: string;
    phase: "pending" | "ready" | "error";
    frame: VisionObservation["frame"];
    observation: VisionObservation;
    error: string | null;
  } | null;
}
export interface TaskPresentation {
  current: TextTasks["current"];
  status: string;
  source: string;
  error: string;
}
export interface ReplayFrame {
  recording_id: string;
  index: number;
  frames: number;
  historical: true;
  read_only: true;
  state: State & { service_time_ms: number };
  events: EventItem[];
  image_policy: string;
}
export interface DesktopBridge {
  state(): Promise<State>;
  connect(): Promise<Operation>;
  disconnect(): Promise<unknown>;
  profile(): Promise<Operation>;
  taskStart(confirmed: boolean): Promise<unknown>;
  taskCancel(): Promise<unknown>;
  taskQuery(): Promise<unknown>;
  recordings(): Promise<{ recordings: Recording[] }>;
  replayOpen(id: string, index?: number): Promise<ReplayFrame>;
  replaySeek(id: string, index: number): Promise<ReplayFrame>;
  replayClose(): Promise<unknown>;
  scenario(name: string): Promise<unknown>;
  image(): Promise<Capture>;
  visionAnalyze(capture: VisionCapture): Promise<Vision>;
  visionCancel(requestId: string): Promise<Vision>;
  benchConnect(host: string, port: number): Promise<Bench>;
  benchDisconnect(): Promise<Bench>;
  gatewayConnect(): Promise<Operation>;
  gatewayDisconnect(): Promise<unknown>;
  gatewayProfile(): Promise<Operation>;
  fcUsbConnect(): Promise<FcUsb>;
  fcUsbDisconnect(): Promise<FcUsb>;
  textParse(text: string): Promise<TextTasks>;
  textClear(): Promise<TextTasks>;
  microphone(enabled: boolean): Promise<void>;
  voiceTranscribe(audio: ArrayBuffer): Promise<Voice>;
  voiceClear(): Promise<Voice>;
  benchFocus(): Promise<{ accepted: boolean; focused: false }>;
  benchPreview(): Promise<RealCapture>;
  benchSettings(
    size: "qvga" | "vga" | "svga",
    colour: "driver" | "office",
  ): Promise<Bench>;
  restart(): Promise<unknown>;
  openLogs(): Promise<string>;
  onState(callback: (state: State) => void): () => void;
}
declare global {
  interface Window {
    lf2s: DesktopBridge;
  }
}
// Future WebBridge must implement this interface with its own authentication and deployment.
export const bridge: DesktopBridge = window.lf2s;
