// Quaver MPRIS daemon — IPC 线协议类型（Electron 主进程 ↔ 本守护进程，stdio NDJSON）
//
// 方向：
//   渲染进程 --ipc(quaver:mpris)--> Electron main --stdin-->  本守护进程（state 快照）
//   渲染进程 <--ipc(quaver:mpris-cmd)-- Electron main <--stdout-- 本守护进程（hello / 控制命令）
//
// 每行一个 JSON（NDJSON），v 为协议版本。

export const PROTOCOL_VERSION = 1;

export type WireStatus = "Playing" | "Paused" | "Stopped";
export type WireLoop = "None" | "Track" | "Playlist";

/** 队列/当前曲目条目（渲染进程 Player.Song 的线格式投影） */
export interface WireTrack {
  mid: string;
  /** 去重键：QQ 同一 mid 可能有多版本，取 _key ?? mid；同时用于生成 mpris:trackid 路径 */
  key: string;
  name: string;
  artists: string[];
  album: string;
  /** 封面直链（https，y.gtimg.cn 系）；daemon 拉取失败则省略 mpris:artUrl */
  artUrl: string;
  durationSec: number;
}

/** 渲染进程 → daemon：播放器完整状态快照（幂等，daemon 端 diff 后才上总线） */
export interface StateMsg {
  v: 1;
  t: "state";
  status: WireStatus;
  /** 快照时刻的播放位置（微秒）。daemon 以收到行为基准做单调时钟外推。 */
  posUs: number;
  /** 0..1 生效音量（静音时为 0） */
  volume: number;
  loop: WireLoop;
  shuffle: boolean;
  /** true = 渲染进程刚发生 seek，daemon 应发 Seeked 信号 */
  seeked?: boolean;
  track: WireTrack | null;
  /** TrackList 接口数据源（渲染端截断 ≤200） */
  queue: WireTrack[];
  can: { next: boolean; prev: boolean; play: boolean; pause: boolean; seek: boolean; control: boolean };
}

/** daemon → Electron：启动握手 */
export interface HelloMsg {
  v: 1;
  t: "hello";
  identity: string;
}

/** daemon → Electron：D-Bus 侧收到的控制请求。raise/quit/present 由主进程消费，其余转发渲染层。 */
export type ControlMsg =
  | { v: 1; t: "cmd"; cmd: "play" | "pause" | "playpause" | "stop" | "next" | "prev" | "raise" | "quit" | "present" }
  | { v: 1; t: "cmd"; cmd: "volume"; value: number }
  | { v: 1; t: "cmd"; cmd: "setLoop"; loop: WireLoop }
  | { v: 1; t: "cmd"; cmd: "setShuffle"; on: boolean }
  | { v: 1; t: "cmd"; cmd: "seek"; deltaUs: number }
  | { v: 1; t: "cmd"; cmd: "seekTo"; posSec: number; trackId?: string }
  | { v: 1; t: "cmd"; cmd: "jump"; trackId: string }
  | { v: 1; t: "cmd"; cmd: "openUri"; uri: string };

export type FromDaemonMsg = HelloMsg | ControlMsg;
export type ToDaemonMsg = StateMsg;

/** mpris:trackid 对象路径：/org/quaver/track/<key 安全化>。渲染端 jump 命令反解时须用同一规则。 */
export function trackPath(t: { key?: string; mid?: string }): string {
  const raw = t.key || t.mid || "unknown";
  const safe = raw.replace(/[^A-Za-z0-9_]/g, "_").replace(/^_+|_+$/g, "") || "unknown";
  return `/org/quaver/track/${safe}`;
}
