// Quaver MPRIS daemon — 入口：mpris-service 总线导出 + stdio IPC 桥。
//
// 运行形态：Electron 主进程 spawn `node mpris-daemon.cjs`（打包态用 Electron 自身
// ELECTRON_RUN_AS_NODE=1 跑）。本进程不碰 UI，只做两件事：
//   1. 收渲染进程的播放器状态快照 → 差量更新 D-Bus 上的 org.mpris.MediaPlayer2.quaver
//   2. 收 D-Bus 控制请求（playerctl / KDE 多媒体键 / GNOME 扩展等）→ 回推 cmd 给渲染进程执行
//
// 位置模型：快照带 posUs 基准，daemon 以 monotonic 时钟外推（Playing 时 +dt），
// 避免为进度每秒往返 IPC；客户端轮询 Position 属性即可拿到平滑值。
//
// mpris-service@2.1.2 无 .d.ts，这里用最小接口声明 + 运行时 require。
import { attachStdioIpc, sendCommand, sendHello } from "./ipc";
import { PROTOCOL_VERSION, trackPath, type StateMsg, type WireLoop, type WireTrack } from "./types";

// eslint-disable-next-line @typescript-eslint/no-var-requires
const MprisPlayer = require("mpris-service") as MprisPlayerFactory;

interface MprisPlayerInstance {
  on(ev: string, cb: (...args: any[]) => void): void;
  metadata: Record<string, unknown>;
  playbackStatus: string;
  loopStatus: string;
  shuffle: boolean;
  volume: number;
  canGoNext: boolean;
  canGoPrevious: boolean;
  canPlay: boolean;
  canPause: boolean;
  canSeek: boolean;
  canControl: boolean;
  getPosition(): number;
  seeked(posUs: number): void;
  objectPath(sub: string): string;
  /** TrackList 整表替换（setter 触发 TrackListReplaced 信号）；仅 supportedInterfaces 含 trackList 时存在 */
  tracks?: Record<string, unknown>[];
  setProperty?(name: string, value: unknown): void;
}
interface MprisPlayerFactory {
  (opts: Record<string, unknown>): MprisPlayerInstance;
}

const log = (...parts: unknown[]) => process.stderr.write("[mpris] " + parts.join(" ") + "\n");

/** 总线名（org.mpris.MediaPlayer2.<name>）与 Identity 展示名 */
const BUS_NAME = process.env.QUAVER_MPRIS_NAME || "quaver";
const IDENTITY = process.env.QUAVER_MPRIS_IDENTITY || "Quaver";

const player: MprisPlayerInstance = MprisPlayer({
  name: BUS_NAME,
  identity: IDENTITY,
  desktopEntry: "quaver",
  supportedUriSchemes: ["https", "http"],
  supportedMimeTypes: ["audio/mpeg", "audio/flac", "audio/ogg", "audio/x-flac"],
  supportedInterfaces: ["player", "trackList"],
});

player.on("error", (e: unknown) => {
  // D-Bus 断线 / 名字抢占失败：不重试——主进程监视到进程退出会按需重启或忽略
  log("dbus error:", String((e as Error)?.message ?? e));
  process.exitCode = 0; // 会话总线不可用不是致命错误，安静退出让父进程决策
  process.exit(0);
});

player.getPosition = () => positionUs();
player.canControl = true;
// mpris-service 建 trackList 接口时不会同步 root.HasTrackList（库疏漏），手动置真
(player as unknown as { hasTrackList: boolean }).hasTrackList = true;

// —— 状态镜像（diff 后才写总线属性，mpris-service 内部再 diff 后才发 PropertiesChanged）——
let lastStatus = "";
let lastLoop = "";
let lastShuffle: boolean | null = null;
let lastVolume = -1;
let lastCanStr = "";
let lastTrackKey = "";
let lastQueueSig = "";

// —— 位置外推 ——
let posBaseUs = 0; // 快照时刻位置
let posStampMs = 0; // 快照到达的 monotonic 时刻
let playing = false;
let durationUs = 0;

function nowMs(): number {
  return Number(process.hrtime.bigint() / 1000000n);
}

function positionUs(): number {
  let p = posBaseUs;
  if (playing) p += (nowMs() - posStampMs) * 1000;
  if (durationUs > 0) p = Math.min(p, durationUs);
  return Math.max(0, Math.floor(p));
}

/** 渲染进程 state 快照 → 总线。幂等：同一快照重放无副作用。 */
function applyState(s: StateMsg): void {
  if (s.v !== PROTOCOL_VERSION) {
    log("proto mismatch:", s.v);
    return;
  }
  playing = s.status === "Playing";
  posBaseUs = Math.max(0, Math.floor(s.posUs || 0));
  posStampMs = nowMs();

  if (s.status !== lastStatus) {
    lastStatus = s.status;
    player.playbackStatus = s.status;
  }
  if (s.loop !== lastLoop) {
    lastLoop = s.loop;
    player.loopStatus = s.loop;
  }
  if (s.shuffle !== lastShuffle) {
    lastShuffle = s.shuffle;
    player.shuffle = s.shuffle;
  }
  const vol = clamp01(s.volume);
  if (Math.abs(vol - lastVolume) > 0.004) {
    lastVolume = vol;
    player.volume = vol;
  }
  const canStr = `${s.can.next}|${s.can.prev}|${s.can.play}|${s.can.pause}|${s.can.seek}|${s.can.control}`;
  if (canStr !== lastCanStr) {
    lastCanStr = canStr;
    player.canGoNext = s.can.next;
    player.canGoPrevious = s.can.prev;
    player.canPlay = s.can.play;
    player.canPause = s.can.pause;
    player.canSeek = s.can.seek;
    player.canControl = s.can.control;
  }

  durationUs = Math.floor((s.track?.durationSec || 0) * 1e6);

  const trackKey = s.track ? trackPath(s.track) : "";
  if (trackKey !== lastTrackKey) {
    lastTrackKey = trackKey;
    player.metadata = s.track ? metadataOf(s.track) : { "mpris:trackid": player.objectPath("TrackList/NoTrack") };
  }

  // TrackList：队列签名变化才整表替换（信号较重，diff 防刷屏）。
  // 必须整表重赋——mpris-service 只在 tracks setter 触发时才同步 root.HasTrackList（其默认 false），
  // 仅更新 Metadata 会让 Qt 侧播放列表客户端看不到列表。
  const queueSig = s.queue.map((t) => t.mid).join(",") + "#" + trackKey;
  if (queueSig !== lastQueueSig) {
    lastQueueSig = queueSig;
    try {
      player.tracks = s.queue.map(trackMetaOf);
    } catch (e) {
      log("tracklist update failed:", String(e));
    }
  }

  if (s.seeked) {
    try {
      player.seeked(posBaseUs);
    } catch (e) {
      log("seeked signal failed:", String(e));
    }
  }
}

function metadataOf(t: WireTrack): Record<string, unknown> {
  const m: Record<string, unknown> = {
    "mpris:trackid": trackPath(t),
    "xesam:title": t.name || "未知歌曲",
    "xesam:artist": t.artists.length ? t.artists : ["未知歌手"],
  };
  if (durationUs > 0) m["mpris:length"] = durationUs;
  if (t.album) m["xesam:album"] = t.album;
  // 封面：QQ 图床对 D-Bus 客户端（plasma-mpris 等）直连可用；失败最多显示占位图
  if (t.artUrl) m["mpris:artUrl"] = t.artUrl;
  return m;
}

/** TrackList 需要 aa{sv} 全表；mpris-service 的 addTrack 有 bug（this 引用错），
 *  所以只走 `player.tracks = [...]` 整表替换路径。 */
function trackMetaOf(t: WireTrack): Record<string, unknown> {
  const m: Record<string, unknown> = {
    "mpris:trackid": trackPath(t),
    "xesam:title": t.name || "未知歌曲",
    "xesam:artist": t.artists.length ? t.artists : ["未知歌手"],
  };
  const len = Math.floor((t.durationSec || 0) * 1e6);
  if (len > 0) m["mpris:length"] = len;
  if (t.album) m["xesam:album"] = t.album;
  if (t.artUrl) m["mpris:artUrl"] = t.artUrl;
  return m;
}

function clamp01(v: number): number {
  return Math.max(0, Math.min(1, v || 0));
}

// —— D-Bus 控制事件 → IPC cmd（渲染层执行，UI 是唯一事实源）——
const emitCmd = (msg: Parameters<typeof sendCommand>[0]) => sendCommand(msg);

player.on("play", () => emitCmd({ v: 1, t: "cmd", cmd: "play" }));
player.on("pause", () => emitCmd({ v: 1, t: "cmd", cmd: "pause" }));
player.on("playpause", () => emitCmd({ v: 1, t: "cmd", cmd: "playpause" }));
player.on("stop", () => emitCmd({ v: 1, t: "cmd", cmd: "stop" }));
player.on("next", () => emitCmd({ v: 1, t: "cmd", cmd: "next" }));
player.on("previous", () => emitCmd({ v: 1, t: "cmd", cmd: "prev" }));
player.on("raise", () => emitCmd({ v: 1, t: "cmd", cmd: "raise" }));
player.on("quit", () => emitCmd({ v: 1, t: "cmd", cmd: "quit" }));
player.on("volume", (v: number) => emitCmd({ v: 1, t: "cmd", cmd: "volume", value: clamp01(v) }));
player.on("loopStatus", (l: string) => {
  // 总线写回值先归一，非法值回弹到当前 UI 状态（lastLoop）
  const loop = (l === "Track" || l === "Playlist" || l === "None" ? l : lastLoop || "None") as WireLoop;
  emitCmd({ v: 1, t: "cmd", cmd: "setLoop", loop });
});
player.on("shuffle", (on: boolean) => emitCmd({ v: 1, t: "cmd", cmd: "setShuffle", on: !!on }));
player.on("seek", (offsetUs: number) => emitCmd({ v: 1, t: "cmd", cmd: "seek", deltaUs: Math.floor(offsetUs || 0) }));
player.on("position", (e: { trackId: string; position: number }) =>
  emitCmd({ v: 1, t: "cmd", cmd: "seekTo", posSec: Math.floor(e.position || 0) / 1e6, trackId: e.trackId })
);
player.on("open", (e: { uri: string }) => emitCmd({ v: 1, t: "cmd", cmd: "openUri", uri: String(e.uri ?? "") }));
// TrackList.GoTo（Qt 播放列表控件双击跳曲）
(player as unknown as { on(ev: "goTo", cb: (id: string) => void): void }).on("goTo", (id: string) =>
  emitCmd({ v: 1, t: "cmd", cmd: "jump", trackId: String(id ?? "") })
);

// —— IPC 通道 ——
attachStdioIpc({ onState: applyState, log });

process.stdin.on("end", () => {
  // Electron 主进程断开 stdin = 应用退出，daemon 随之收尾
  log("stdin closed, exiting");
  process.exit(0);
});

sendHello({ v: 1, t: "hello", identity: IDENTITY });
