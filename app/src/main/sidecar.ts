/**
 * 主进程 sidecar 管理器（02-技术路径与架构.md §13）：
 * - `start(cmd, args)` spawn 子进程；
 * - `send(request)` 序列化 JSONL 写 stdin（写之前自检请求形状）；
 * - stdout 按行解析为 `EngineEvent`（解析失败跳过该行，stderr 转发日志回调）；
 * - 崩溃（非零退出 / spawn 失败）时发 `error{fatal}` 事件并自动重启一次；
 * - `stop()`：关 stdin → 引擎 EOF 收尾；`EXIT_GRACE_MS`（5s）后 kill。
 */
import { spawn, type ChildProcess } from 'node:child_process';
import { createInterface } from 'node:readline';
import { isEngineEvent, isRequest, type EngineEvent, type Request } from '../shared/protocol';

/** 引擎收到 stdin EOF / cancel 后的收尾宽限（协议 §9.1：5s 内收尾退出，超时 kill）。 */
export const EXIT_GRACE_MS = 5_000;

/** 崩溃自动重启上限（重启过一次仍崩 → 只发 error{fatal} 不再重启）。 */
export const MAX_RESTARTS = 1;

export type SidecarState =
  | 'stopped'
  | 'starting'
  | 'running'
  | 'restarting'
  | 'stopping'
  | 'exited';

export interface SidecarEvents {
  /** 每个解析成功的引擎事件（含假 error 事件）。 */
  onEvent: (event: EngineEvent) => void;
  /** sidecar 自身 stderr 日志（tracing，人读）。 */
  onLog: (line: string) => void;
  /** 状态机变化。 */
  onStateChange: (state: SidecarState) => void;
}

export interface SidecarStartOptions {
  cmd: string;
  args: string[];
  cwd?: string;
  env?: NodeJS.ProcessEnv;
}

/**
 * sidecar 管理器。一个实例对应一个引擎子进程；`start` 后可通过 `send` 连续下发请求。
 * 事件按 stdout 行序送达，`seq` 单调性由引擎保证（此处不重排）。
 */
export class SidecarManager {
  private child: ChildProcess | null = null;
  private state: SidecarState = 'stopped';
  private restarts = 0;
  private startOptions: SidecarStartOptions | null = null;
  private stopTimer: NodeJS.Timeout | null = null;
  private killTimer: NodeJS.Timeout | null = null;
  private exitWaiters: Array<() => void> = [];
  private stopped = false;
  private readonly events: SidecarEvents;
  /** 上次 configure 请求（重启后自动重发，api_key 只经 stdin 不落日志）。 */
  private lastConfigure: Request | null = null;

  constructor(events: SidecarEvents) {
    this.events = events;
  }

  getState(): SidecarState {
    return this.state;
  }

  private setState(state: SidecarState): void {
    this.state = state;
    this.events.onStateChange(state);
  }

  isRunning(): boolean {
    return this.state === 'running' || this.state === 'starting' || this.state === 'restarting';
  }

  /** spawn 子进程并绑定行解析。重复调用在运行中直接返回当前状态。 */
  async start(options: SidecarStartOptions): Promise<void> {
    if (this.isRunning()) return;
    this.stopped = false;
    this.startOptions = options;
    await this.spawnChild(options);
  }

  private spawnChild(options: SidecarStartOptions): Promise<void> {
    return new Promise((resolve, reject) => {
      this.setState(this.restarts > 0 ? 'restarting' : 'starting');
      let child: ChildProcess;
      try {
        child = spawn(options.cmd, options.args, {
          cwd: options.cwd,
          env: options.env,
          stdio: ['pipe', 'pipe', 'pipe'],
          // API key 只走 stdin（§11），不进 argv；env 传最小集合
        });
      } catch (error) {
        reject(error instanceof Error ? error : new Error(String(error)));
        return;
      }
      this.child = child;
      if (child.stdin === null || child.stdout === null || child.stderr === null) {
        reject(new Error('sidecar stdio 未开启管道'));
        return;
      }

      const stdout = createInterface({ input: child.stdout });
      stdout.on('line', (line) => {
        const text = line.trim();
        if (text === '') return;
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          this.events.onLog(`sidecar stdout 非 JSON 行：${text.slice(0, 200)}`);
          return;
        }
        if (isEngineEvent(parsed)) {
          this.events.onEvent(parsed);
        } else {
          this.events.onLog(`sidecar stdout 未知事件：${text.slice(0, 200)}`);
        }
      });

      const stderr = createInterface({ input: child.stderr });
      stderr.on('line', (line) => this.events.onLog(line));

      child.on('error', (error) => {
        // spawn 本身失败（ENOENT 等）：与崩溃同路径
        this.handleExit(new Error(error.message), null);
        resolve();
      });

      child.on('exit', (code, signal) => {
        this.handleExit(null, { code, signal });
        resolve();
      });

      // 子进程起来即进入 running（引擎就绪与否由 run_started 事件表达）
      this.setState('running');
      // 重启后自动重发 configure，恢复会话
      if (this.lastConfigure !== null) {
        this.writeLine(this.lastConfigure);
      }
      resolve();
    });
  }

  /**
   * 崩溃 / 退出统一处理：
   * - 主动 `stop()` 触发的退出（`stopping`）→ 进入 `exited`，正常收尾；
   * - 意外退出（`running`）→ 发 `error{fatal}` 合成事件 + 重启一次；
   * - 重启后仍崩 → 不再重启，停留在 `exited`。
   */
  private handleExit(spawnError: Error | null, exit: { code: number | null; signal: NodeJS.Signals | null } | null): void {
    this.clearTimers();
    this.child = null;
    if (this.state === 'stopping' || this.stopped) {
      this.setState('exited');
      this.flushExitWaiters();
      return;
    }

    const detail =
      spawnError !== null
        ? `spawn 失败：${spawnError.message}`
        : `进程退出：code=${exit?.code ?? 'null'} signal=${exit?.signal ?? 'null'}`;

    if (this.restarts < MAX_RESTARTS) {
      this.restarts += 1;
      // 崩溃事件先于重启，渲染端立即可见（§13：崩溃时发 error{fatal} 并重启）
      this.emitSyntheticError(`sidecar 崩溃，正在重启（第 ${this.restarts} 次）：${detail}`);
      const options = this.startOptions;
      if (options !== null) {
        // 异步重启，不阻塞当前事件循环
        setTimeout(() => {
          if (this.stopped) return;
          this.spawnChild(options).catch(() => {
            this.emitSyntheticError('sidecar 重启失败');
            this.setState('exited');
            this.flushExitWaiters();
          });
        }, 0);
        return;
      }
    } else {
      this.emitSyntheticError(`sidecar 崩溃且重启次数已用尽：${detail}`);
    }
    this.setState('exited');
    this.flushExitWaiters();
  }

  /** 主进程合成的 error 事件（引擎无法自己发时兜底）。 */
  private emitSyntheticError(message: string): void {
    const event: EngineEvent = {
      seq: Number.MAX_SAFE_INTEGER,
      ts: Date.now() / 1000,
      type: 'error',
      fatal: true,
      code: 'sidecar_crashed',
      message,
    };
    this.events.onEvent(event);
  }

  private clearTimers(): void {
    if (this.stopTimer !== null) {
      clearTimeout(this.stopTimer);
      this.stopTimer = null;
    }
    if (this.killTimer !== null) {
      clearTimeout(this.killTimer);
      this.killTimer = null;
    }
  }

  private writeLine(request: Request): void {
    const stdin = this.child?.stdin;
    if (stdin === null || stdin === undefined || stdin.destroyed || !stdin.writable) {
      throw new Error('sidecar stdin 不可写（进程未运行）');
    }
    stdin.write(`${JSON.stringify(request)}\n`);
  }

  /** 下发请求。`configure` 会被记住，崩溃重启后自动重发。 */
  send(request: Request): void {
    if (!isRequest(request)) {
      const type = (request as { type?: unknown }).type;
      throw new TypeError(`非法请求形状：${String(type)}`);
    }
    if (request.type === 'configure') {
      this.lastConfigure = request;
    }
    this.writeLine(request);
  }

  /**
   * 主动停止：stdin EOF → 等 `EXIT_GRACE_MS` → kill。
   * 引擎 5s 内自行退出则提前完成（不 kill）。
   */
  async stop(): Promise<void> {
    this.stopped = true;
    const child = this.child;
    if (child === null || child.exitCode !== null || child.signalCode !== null) {
      this.setState('exited');
      return;
    }
    this.setState('stopping');
    const exited = new Promise<void>((resolve) => this.exitWaiters.push(resolve));

    const stdin = child.stdin;
    if (stdin !== null && !stdin.destroyed) {
      stdin.end();
    }

    this.stopTimer = setTimeout(() => {
      this.events.onLog(`sidecar ${EXIT_GRACE_MS}ms 未退出，发送 SIGKILL`);
      this.killTimer = setTimeout(() => {
        this.child = null;
        this.setState('exited');
        this.flushExitWaiters();
      }, 1_000);
      try {
        child.kill('SIGKILL');
      } catch {
        // 进程已不在：走 exit 事件收尾
      }
    }, EXIT_GRACE_MS);

    await exited;
    this.clearTimers();
  }

  private flushExitWaiters(): void {
    const waiters = this.exitWaiters;
    this.exitWaiters = [];
    for (const waiter of waiters) waiter();
  }

  /** 测试 / 关闭时用：重置重启计数。 */
  resetRestarts(): void {
    this.restarts = 0;
  }
}
