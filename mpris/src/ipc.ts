// Quaver MPRIS daemon — stdio NDJSON IPC 通道
// 与 Electron 主进程约定：stdin 收 state 行，stdout 发 hello/cmd 行。
// stdout 只写协议 JSON——日志一律 stderr。
import type { ControlMsg, HelloMsg, StateMsg } from "./types";

export interface IpcLog {
  (...parts: string[]): void;
}

export interface IpcHandlers {
  onState(msg: StateMsg): void;
  log: IpcLog;
}

let pending = "";

/** 挂接 stdin 行解析；返回后由调用方在合适时机 sendHello。 */
export function attachStdioIpc(handlers: IpcHandlers): void {
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk: string) => {
    pending += chunk;
    let i: number;
    while ((i = pending.indexOf("\n")) >= 0) {
      const line = pending.slice(0, i);
      pending = pending.slice(i + 1);
      if (!line.trim()) continue;
      let msg: unknown;
      try {
        msg = JSON.parse(line);
      } catch (e) {
        handlers.log("bad ipc line:", String(e));
        continue;
      }
      const m = msg as StateMsg;
      if (m && m.t === "state") {
        try {
          handlers.onState(m);
        } catch (e) {
          handlers.log("state apply failed:", String(e));
        }
      } else {
        handlers.log("unknown ipc msg:", (m && (m as { t?: string }).t) || "?");
      }
    }
  });
  // stdin 关闭（Electron 主进程退出）→ 由 main 负责结束自身
}

function writeLine(obj: unknown): void {
  try {
    process.stdout.write(JSON.stringify(obj) + "\n");
  } catch {
    /* stdout 已断开：父进程走了，等 stdin 'end' 收尾 */
  }
}

export const sendCommand = (msg: ControlMsg) => writeLine(msg);
export const sendHello = (msg: HelloMsg) => writeLine(msg);
