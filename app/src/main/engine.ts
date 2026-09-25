/**
 * 翻译队列（主进程）：一个长驻引擎会话 + 串行论文队列。
 *
 * 引擎事件本身不带 doc_id（除 run_started），这里按"当前任务"归属：
 * 落库（Library.ingest）→ 推给渲染进程 → `run_finished` 后取下一篇。
 * 引擎崩溃（SidecarManager 合成的 `sidecar_crashed`）时当前任务记为失败并继续队列。
 */
import type { ConfigureRequest, EngineEvent, Request, RunRequest } from '../shared/protocol';
import type { DocEngineEvent, LibraryDoc } from '../shared/library';
import type { Library } from './library';
import type { SidecarState } from './sidecar';

/** EngineQueue 用到的 SidecarManager 子集（测试注入假实现）。 */
export interface EngineSession {
  send(request: Request): void;
  isRunning(): boolean;
  start(): Promise<void>;
}

export interface EngineQueueOptions {
  library: Library;
  session: EngineSession;
  /** 每次开新会话 / 队列开跑前取最新配置（设置页修改后生效）。 */
  configure: () => ConfigureRequest;
  onEvent: (event: DocEngineEvent) => void;
  /** 论文卡片字段变化。 */
  onDocChanged: (doc: LibraryDoc) => void;
}

export class EngineQueue {
  private readonly options: EngineQueueOptions;
  private readonly queue: string[] = [];
  private current: string | null = null;
  private configured = false;

  constructor(options: EngineQueueOptions) {
    this.options = options;
  }

  get currentDoc(): string | null {
    return this.current;
  }

  queued(): readonly string[] {
    return this.queue;
  }

  /** 加入队列（已在队列 / 正在跑的忽略）。 */
  enqueue(id: string): void {
    if (this.current === id || this.queue.includes(id)) return;
    const { library } = this.options;
    if (library.get(id) === null) throw new Error(`论文不存在：${id}`);
    this.queue.push(id);
    library.setStatus(id, 'queued', { error: null });
    this.changed(id);
    void this.pump();
  }

  /** 取消：排队中的直接出队；正在跑的发 cancel，等引擎 `run_finished` 收尾。 */
  cancel(id: string): void {
    const { library, session } = this.options;
    const index = this.queue.indexOf(id);
    if (index >= 0) {
      this.queue.splice(index, 1);
      library.setStatus(id, 'cancelled');
      this.changed(id);
      return;
    }
    if (this.current === id) {
      library.setStatus(id, 'cancelled');
      this.changed(id);
      session.send({ type: 'cancel' });
    }
  }

  /** 删除论文前调用：出队 / 取消。 */
  forget(id: string): void {
    this.cancel(id);
  }

  /** 引擎会话状态变化（重启后需要重新 configure）。 */
  handleSessionState(state: SidecarState): void {
    if (state === 'starting' || state === 'restarting' || state === 'exited') {
      this.configured = false;
    }
  }

  /** SidecarManager 的 onEvent 入口。 */
  handleEvent(event: EngineEvent): void {
    const { library } = this.options;
    const docId = this.current;
    if (docId !== null) {
      if (library.ingest(docId, event)) this.changed(docId);
    }
    this.options.onEvent({ docId, event });
    if (docId === null) return;
    const crashed = event.type === 'error' && event.code === 'sidecar_crashed';
    if (crashed) {
      library.setStatus(docId, 'failed', { error: event.message });
      this.changed(docId);
    }
    if (event.type === 'run_finished' || crashed) {
      this.current = null;
      void this.pump();
    }
  }

  private async pump(): Promise<void> {
    if (this.current !== null) return;
    const next = this.queue.shift();
    if (next === undefined) return;
    this.current = next;
    const { library, session } = this.options;
    try {
      if (!session.isRunning()) await session.start();
      const configure = this.options.configure();
      if (!this.configured) {
        session.send(configure);
        this.configured = true;
      }
      const doc = library.get(next);
      if (doc === null) throw new Error(`论文已删除：${next}`);
      library.setStatus(next, 'running', { model: configure.model });
      this.changed(next);
      session.send(this.runRequest(doc));
    } catch (error) {
      library.setStatus(next, 'failed', {
        error: error instanceof Error ? error.message : String(error),
      });
      this.changed(next);
      this.current = null;
      void this.pump();
    }
  }

  private runRequest(doc: LibraryDoc): RunRequest {
    return {
      type: 'run',
      doc_id: doc.id,
      input: doc.sourcePath,
      output: this.options.library.translatedPath(doc.id),
      source_lang: 'en',
      target_lang: 'zh-CN',
      pages: null,
      font_profile: null,
      terminology: null,
      mode: 'full',
      store: this.options.library.storePath(doc.id),
    };
  }

  private changed(id: string): void {
    const doc = this.options.library.get(id);
    if (doc !== null) this.options.onDocChanged(doc);
  }
}
