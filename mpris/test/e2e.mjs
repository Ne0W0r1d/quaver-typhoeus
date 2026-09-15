// MPRIS daemon 端到端自测：spawn dist → hello → 喂 state → playerctl/gdbus 总线侧验证 → 触发 Next 观察 cmd 回推。
// 用法：node test/e2e.mjs（需要活动 D-Bus 会话总线）
import { spawn, execFile } from "node:child_process";
import { setTimeout as sleep } from "node:timers/promises";

const daemon = spawn(process.execPath, ["dist/mpris-daemon.cjs"], { stdio: ["pipe", "pipe", "pipe"] });
let out = "";
let cmds = [];
let sawHello = false;
daemon.stdout.setEncoding("utf8");
daemon.stdout.on("data", (c) => {
  out += c;
  let i;
  while ((i = out.indexOf("\n")) >= 0) {
    const line = out.slice(0, i).trim();
    out = out.slice(i + 1);
    if (!line) continue;
    const m = JSON.parse(line);
    if (m.t === "cmd") cmds.push(m.cmd);
    if (m.t === "hello") sawHello = true;
    console.log("daemon→ipc:", line.slice(0, 100));
  }
});
daemon.stderr.setEncoding("utf8");
daemon.stderr.on("data", (c) => process.stderr.write(c));

function sh(...args) {
  return new Promise((res) => execFile(args[0], args.slice(1), (e, so, se) => res({ code: e ? 1 : 0, out: so + se })));
}

const state = {
  v: 1, t: "state", status: "Playing", posUs: 30_000_000, volume: 0.65, loop: "Playlist", shuffle: false,
  track: { mid: "TEST001", key: "TEST001", name: "测试曲目 Title", artists: ["歌手A", "歌手B"], album: "某专辑",
           artUrl: "https://y.gtimg.cn/music/photo_new/T002R300x300M000000.jpg", durationSec: 180 },
  queue: [
    { mid: "TEST001", key: "TEST001", name: "测试曲目 Title", artists: ["歌手A"], album: "某专辑", artUrl: "", durationSec: 180 },
    { mid: "TEST002", key: "TEST002", name: "第二首", artists: ["歌手C"], album: "某专辑", artUrl: "", durationSec: 200 },
  ],
  can: { next: true, prev: true, play: true, pause: true, seek: true, control: true },
};
daemon.stdin.write(JSON.stringify(state) + "\n");

let pass = 0, fail = 0;
function check(name, ok, detail = "") {
  console.log((ok ? "PASS" : "FAIL") + "  " + name + (detail ? "  | " + detail.trim().slice(0, 160) : ""));
  ok ? pass++ : fail++;
}

await sleep(1200); // hello + 状态落总线 + 外推几秒

const hello = sawHello;
check("hello 握手", hello);

let r = await sh("playerctl", "-p", "quaver", "status");
check("playerctl status=Playing", r.out.trim() === "Playing", r.out);

r = await sh("playerctl", "-p", "quaver", "metadata", "xesam:title");
check("metadata title", r.out.trim() === "测试曲目 Title", r.out);

r = await sh("playerctl", "-p", "quaver", "metadata", "xesam:artist");
check("metadata artist", r.out.trim().includes("歌手A"), r.out);

r = await sh("playerctl", "-p", "quaver", "position");
// playerctl 打印的是十进制秒（非 µs）
const posSec = parseFloat(r.out.trim());
check("position 外推≈31-35s", posSec > 30 && posSec < 36, r.out);

// gdbus 全属性：LoopStatus/Volume/HasTrackList/TrackList 元数据
r = await sh("gdbus", "call", "--session", "--dest", "org.mpris.MediaPlayer2.quaver", "--object-path", "/org/mpris/MediaPlayer2",
  "--method", "org.freedesktop.DBus.Properties.GetAll", "org.mpris.MediaPlayer2.Player");
check("GetAll 含 LoopStatus=Playlist", r.out.includes("'Playlist'"), r.out);
check("GetAll 含 Volume=0.65", r.out.includes("0.65") || r.out.includes("0.650"), r.out);
check("GetAll 含 trackid 路径", r.out.includes("/org/quaver/track/TEST001"), r.out);

r = await sh("gdbus", "call", "--session", "--dest", "org.mpris.MediaPlayer2.quaver", "--object-path", "/org/mpris/MediaPlayer2",
  "--method", "org.mpris.MediaPlayer2.Get");
// Raise 走 root 方法调用（不关心返回，只要不报错且 daemon 回推 raise cmd）
cmds = [];
r = await sh("gdbus", "call", "--session", "--dest", "org.mpris.MediaPlayer2.quaver", "--object-path", "/org/mpris/MediaPlayer2",
  "--method", "org.mpris.MediaPlayer2.Player.Next");
check("Next() 成功返回", r.code === 0, r.out);
await sleep(300);
check("daemon 回推 cmd:next", cmds.includes("next"), JSON.stringify(cmds));

// SetPosition → seekTo 命令
cmds = [];
r = await sh("gdbus", "call", "--session", "--dest", "org.mpris.MediaPlayer2.quaver", "--object-path", "/org/mpris/MediaPlayer2",
  "--method", "org.mpris.MediaPlayer2.Player.SetPosition", "/org/quaver/track/TEST001", Int64ish(60_000_000));
await sleep(300);
check("SetPosition 回推 seekTo", cmds.some((c) => c === "seekTo"), JSON.stringify(cmds) + r.out);

// 暂停：喂第二帧 status=Paused，playerctl 应立刻反映
daemon.stdin.write(JSON.stringify({ ...state, status: "Paused" }) + "\n");
await sleep(400);
r = await sh("playerctl", "-p", "quaver", "status");
check("状态更新 Paused", r.out.trim() === "Paused", r.out);

// TrackList 接口存在性
const r1 = await sh("gdbus", "call", "--session", "--dest", "org.mpris.MediaPlayer2.quaver", "--object-path", "/org/mpris/MediaPlayer2",
  "--method", "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2", "HasTrackList");
check("HasTrackList=true", r1.out.includes("true"), r1.out);

r = await sh("gdbus", "call", "--session", "--dest", "org.mpris.MediaPlayer2.quaver", "--object-path", "/org/mpris/MediaPlayer2",
  "--method", "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.TrackList", "Tracks");
check("TrackList.Tracks 两条", r.out.includes("TEST001") && r.out.includes("TEST002"), r.out);

daemon.kill();
console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);

function Int64ish(n) {
  // gdbus CLI 的 int64 直接十进制字面量
  return String(n);
}
