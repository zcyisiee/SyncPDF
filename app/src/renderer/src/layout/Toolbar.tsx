/**
 * 工具条（M2-09）：打开 PDF → 配置 → 运行 / 取消 / 导出 + 滚动同步开关。
 *
 * - "打开 PDF"：`app:openFile`（主进程弹系统框并把所在目录加入 `syncpdf-file://`
 *   与 `readFileBytes` 的白名单），随后 documentStore.openDocument 记下源 / 译文路径；
 * - "配置"：内联表单，凭据经 `credentials:*` 落 0600 文件，并立刻下发 `configure`
 *   （api_key 只经 stdin，不进渲染进程日志）；
 * - "运行"：`run{ input, output, mode… }`；译文路径 = 源文件同目录 `<name>.zh.pdf`。
 */
import { useCallback, useEffect, useState } from 'react';
import { documentStore } from '../store/documentStore';
import { useDocumentStore } from '../store/documentStoreStoreHooks';
import { useUiStore } from '../store/uiStore';

/** `/a/b/c.pdf` → `/a/b/c.zh.pdf`（同目录，已在白名单内）。 */
export function targetPathFor(sourcePath: string, targetLang: string): string {
  const dot = sourcePath.lastIndexOf('.');
  const slash = Math.max(sourcePath.lastIndexOf('/'), sourcePath.lastIndexOf('\\'));
  const stem = dot > slash ? sourcePath.slice(0, dot) : sourcePath;
  return `${stem}.${targetLang}.pdf`;
}

/** `/a/b/c.pdf` → `c.pdf`。 */
export function baseName(path: string): string {
  const slash = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
  return slash >= 0 ? path.slice(slash + 1) : path;
}

interface ConfigForm {
  provider: string;
  base_url: string;
  model: string;
  api_key: string;
  concurrency: number;
}

const DEFAULT_CONFIG: ConfigForm = {
  provider: 'openai_compatible',
  base_url: 'https://api.openai.com/v1',
  model: 'gpt-4o-mini',
  api_key: '',
  concurrency: 4,
};

export function Toolbar(): JSX.Element {
  const sourcePath = useDocumentStore((state) => state.sourcePath);
  const targetPath = useDocumentStore((state) => state.targetPath);
  const runState = useDocumentStore((state) => state.runState);
  const docId = useDocumentStore((state) => state.docId);
  const output = useDocumentStore((state) => state.output);
  const syncScroll = useUiStore((state) => state.scrollSyncEnabled);
  const setScrollSync = useUiStore((state) => state.setScrollSync);

  const [config, setConfig] = useState<ConfigForm>(DEFAULT_CONFIG);
  const [configOpen, setConfigOpen] = useState(false);
  const [configured, setConfigured] = useState(false);
  const [sourceLang, setSourceLang] = useState('en');
  const [targetLang, setTargetLang] = useState('zh');
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  // 启动时读回已保存的凭据（api_key 只驻留内存 + 0600 文件）
  useEffect(() => {
    void window.syncpdf
      .readCredentials()
      .then((saved) => {
        if (saved === null) return;
        setConfig((previous) => ({ ...previous, ...saved }));
      })
      .catch(() => {
        // 凭据文件损坏：保持默认，用户重新填
      });
  }, []);

  const openPdf = useCallback(async () => {
    setNotice(null);
    const path = await window.syncpdf.openFile();
    if (path === null) return;
    documentStore.getState().openDocument({
      sourcePath: path,
      targetPath: targetPathFor(path, targetLang),
      docId: baseName(path),
    });
  }, [targetLang]);

  const sendConfigure = useCallback(async () => {
    setBusy(true);
    setNotice(null);
    try {
      await window.syncpdf.writeCredentials({
        provider: config.provider,
        base_url: config.base_url,
        model: config.model,
        api_key: config.api_key,
      });
      await window.syncpdf.configure({
        provider: config.provider,
        base_url: config.base_url,
        model: config.model,
        api_key: config.api_key,
        concurrency: config.concurrency,
        cache_dir: '',
      });
      setConfigured(true);
      setConfigOpen(false);
      setNotice('配置已下发');
    } catch (error) {
      setNotice(describe(error));
    } finally {
      setBusy(false);
    }
  }, [config]);

  const startRun = useCallback(async () => {
    if (sourcePath === null) return;
    const out = targetPath ?? targetPathFor(sourcePath, targetLang);
    setBusy(true);
    setNotice(null);
    try {
      await window.syncpdf.startRun({
        type: 'run',
        doc_id: docId ?? baseName(sourcePath),
        input: sourcePath,
        output: out,
        source_lang: sourceLang,
        target_lang: targetLang,
        mode: 'full',
      });
      documentStore.setState({ targetPath: out });
    } catch (error) {
      setNotice(describe(error));
    } finally {
      setBusy(false);
    }
  }, [sourcePath, targetPath, targetLang, sourceLang, docId]);

  const cancel = useCallback(async () => {
    setBusy(true);
    try {
      await window.syncpdf.cancel();
      setNotice('已请求取消');
    } catch (error) {
      setNotice(describe(error));
    } finally {
      setBusy(false);
    }
  }, []);

  const exportDocument = useCallback(async () => {
    if (docId === null || targetPath === null) return;
    setBusy(true);
    try {
      await window.syncpdf.exportDocument({
        type: 'export',
        doc_id: docId,
        output: targetPath,
        mode: 'full',
      });
      setNotice('已请求导出');
    } catch (error) {
      setNotice(describe(error));
    } finally {
      setBusy(false);
    }
  }, [docId, targetPath]);

  const running = runState === 'running';

  return (
    <div
      style={{
        flex: '0 0 auto',
        display: 'flex',
        flexDirection: 'column',
        borderBottom: '1px solid var(--vscode-panel-border)',
        background: 'var(--vscode-editorWidget-background, #252526)',
      }}
    >
      <div
        role="toolbar"
        aria-label="任务工具栏"
        style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '4px 8px', fontSize: 12 }}
      >
        <ToolButton label="打开 PDF" icon="codicon-folder-opened" onClick={() => void openPdf()} />
        <ToolButton
          label="配置"
          icon={configured ? 'codicon-pass' : 'codicon-settings-gear'}
          onClick={() => setConfigOpen((open) => !open)}
        />
        <ToolButton
          label="运行"
          icon="codicon-play"
          primary
          disabled={sourcePath === null || running || busy}
          onClick={() => void startRun()}
        />
        <ToolButton
          label="取消"
          icon="codicon-debug-stop"
          disabled={!running || busy}
          onClick={() => void cancel()}
        />
        <ToolButton
          label="导出"
          icon="codicon-export"
          disabled={output === null || busy}
          onClick={() => void exportDocument()}
        />

        <label style={{ display: 'flex', alignItems: 'center', gap: 4, marginLeft: 8, opacity: 0.85 }}>
          <span>语言</span>
          <input
            aria-label="源语言"
            value={sourceLang}
            onChange={(domEvent) => setSourceLang(domEvent.target.value)}
            style={{ ...inputStyle, width: 44 }}
          />
          <span className="codicon codicon-arrow-right" />
          <input
            aria-label="目标语言"
            value={targetLang}
            onChange={(domEvent) => setTargetLang(domEvent.target.value)}
            style={{ ...inputStyle, width: 44 }}
          />
        </label>

        <label style={{ display: 'flex', alignItems: 'center', gap: 4, marginLeft: 8 }}>
          <input
            type="checkbox"
            checked={syncScroll}
            onChange={(domEvent) => setScrollSync(domEvent.target.checked)}
          />
          同步滚动
        </label>

        <span style={{ marginLeft: 'auto', opacity: 0.7, maxWidth: 360, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {notice ?? (sourcePath === null ? '未打开文档' : baseName(sourcePath))}
        </span>
      </div>

      {configOpen && (
        <form
          aria-label="引擎配置"
          onSubmit={(domEvent) => {
            domEvent.preventDefault();
            void sendConfigure();
          }}
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
            gap: 8,
            padding: '8px 10px',
            borderTop: '1px solid var(--vscode-panel-border)',
          }}
        >
          <Field label="provider">
            <select
              aria-label="provider"
              value={config.provider}
              onChange={(domEvent) => setConfig({ ...config, provider: domEvent.target.value })}
              style={inputStyle}
            >
              <option value="openai_compatible">openai_compatible</option>
              <option value="anthropic">anthropic</option>
            </select>
          </Field>
          <Field label="base_url">
            <input
              aria-label="base_url"
              value={config.base_url}
              onChange={(domEvent) => setConfig({ ...config, base_url: domEvent.target.value })}
              style={inputStyle}
            />
          </Field>
          <Field label="model">
            <input
              aria-label="model"
              value={config.model}
              onChange={(domEvent) => setConfig({ ...config, model: domEvent.target.value })}
              style={inputStyle}
            />
          </Field>
          <Field label="api_key">
            <input
              aria-label="api_key"
              type="password"
              value={config.api_key}
              onChange={(domEvent) => setConfig({ ...config, api_key: domEvent.target.value })}
              style={inputStyle}
            />
          </Field>
          <Field label="concurrency">
            <input
              aria-label="concurrency"
              type="number"
              min={1}
              max={32}
              value={config.concurrency}
              onChange={(domEvent) =>
                setConfig({ ...config, concurrency: Number(domEvent.target.value) || 1 })
              }
              style={inputStyle}
            />
          </Field>
          <div style={{ display: 'flex', alignItems: 'flex-end' }}>
            <ToolButton label="下发配置" icon="codicon-check" primary disabled={busy} onClick={() => void sendConfigure()} />
          </div>
        </form>
      )}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  background: 'var(--vscode-input-background, #3c3c3c)',
  color: 'var(--vscode-input-foreground, #cccccc)',
  border: '1px solid var(--vscode-input-border, transparent)',
  borderRadius: 2,
  padding: '2px 6px',
  fontSize: 12,
  minWidth: 0,
};

function Field({ label, children }: { label: string; children: JSX.Element }): JSX.Element {
  return (
    <label style={{ display: 'flex', flexDirection: 'column', gap: 2, fontSize: 11, opacity: 0.9 }}>
      <span>{label}</span>
      {children}
    </label>
  );
}

function ToolButton({
  label,
  icon,
  onClick,
  disabled,
  primary,
}: {
  label: string;
  icon: string;
  onClick: () => void;
  disabled?: boolean;
  primary?: boolean;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        padding: '3px 9px',
        fontSize: 12,
        border: '1px solid var(--vscode-button-border, transparent)',
        borderRadius: 2,
        cursor: disabled === true ? 'default' : 'pointer',
        opacity: disabled === true ? 0.45 : 1,
        background: primary === true
          ? 'var(--vscode-button-background, #0e639c)'
          : 'var(--vscode-button-secondaryBackground, #3a3d41)',
        color: primary === true
          ? 'var(--vscode-button-foreground, #ffffff)'
          : 'var(--vscode-button-secondaryForeground, #ffffff)',
      }}
    >
      <span className={`codicon ${icon}`} />
      {label}
    </button>
  );
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
