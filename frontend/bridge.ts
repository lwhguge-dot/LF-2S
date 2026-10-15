export interface Sensor {
  valid: boolean;
  value: number | number[] | null;
  age_ms: number | null;
  reason: string | null;
  source: "synthetic";
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
  simulation: true;
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
export interface Capture {
  bytes: Uint8Array;
  frame_id: number;
  stream_id: string;
  synthetic: true;
  hardware_freshness_qualified: false;
  received_at: number;
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
