import { useCallback, useEffect, useRef, useState } from 'react';

import { ACTIVE, api, setUnauthorizedHandler, type ApiError, type Job, type JobItem, type Me, type Model } from './api';
import { EventsCard } from './components/EventsCard';
import { HistoryDrawer } from './components/HistoryDrawer';
import { Home } from './components/Home';
import { Login } from './components/Login';
import { Preview } from './components/Preview';
import { IconDefs, Topbar } from './components/Topbar';
import { useJob } from './run';

/** `#/job/<id>` 是工作区，其余是首页；刷新页面能回到正在看的那篇。 */
const routeJob = () => location.hash.match(/^#\/job\/([0-9a-f]+)$/)?.[1] ?? null;

function useToast() {
  const [message, setMessage] = useState('');
  const [show, setShow] = useState(false);
  const timer = useRef(0);
  const toast = useCallback((text: string) => {
    setMessage(text);
    setShow(true);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setShow(false), 2200);
  }, []);
  return { toast, node: <div className={`toast${show ? ' show' : ''}`}>{message}</div> };
}

export function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [ready, setReady] = useState(false);
  const [jobId, setJobId] = useState(routeJob);
  const [drawer, setDrawer] = useState(false);
  const [items, setItems] = useState<JobItem[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [file, setFile] = useState<File | null>(null);
  const [model, setModel] = useState('');
  const [thinking, setThinking] = useState('low');
  // 本次会话上传过的文件：缓存命中后「换个思考强度」直接回到已选文件
  const files = useRef(new Map<string, File>());
  const { toast, node: toastNode } = useToast();

  const refresh = useCallback(() => {
    api.me().then(setMe, () => {});
    api.jobs().then(setItems, () => {});
  }, []);

  // 模型目录登录后才能取；默认选第一个
  useEffect(() => {
    if (me && !models.length) api.models().then(setModels, () => {});
  }, [me, models.length]);
  useEffect(() => {
    if (models.length && !models.some((m) => m.key === model)) setModel(models[0].key);
  }, [models, model]);
  const chooseModel = (key: string) => {
    setModel(key);
    const efforts = models.find((m) => m.key === key)?.efforts ?? [];
    if (!efforts.includes(thinking)) setThinking(efforts[0]);
  };

  useEffect(() => {
    setUnauthorizedHandler(() => setMe(null));
    api
      .me()
      .then((m) => {
        setMe(m);
        return api.jobs().then(setItems);
      })
      .catch(() => {})
      .finally(() => setReady(true));
    const onHash = () => setJobId(routeJob());
    addEventListener('hashchange', onHash);
    return () => removeEventListener('hashchange', onHash);
  }, []);

  const view = !ready ? 'loading' : !me ? 'login' : jobId ? 'work' : 'home';
  useEffect(() => {
    document.body.dataset.view = view;
    document.body.classList.toggle('drawer-open', drawer && view !== 'login');
  }, [view, drawer]);
  useEffect(() => {
    if (drawer || view === 'home') api.jobs().then(setItems, () => {});
  }, [drawer, view]);

  const openJob = (id: string) => {
    setDrawer(false);
    location.hash = `#/job/${id}`;
  };
  const goHome = () => {
    setDrawer(false);
    const current = items.find((i) => i.id === jobId);
    if (jobId && current && ACTIVE.includes(current.status)) toast('翻译仍在进行，可在首页「最近翻译」查看进度');
    if (location.hash) history.pushState(null, '', location.pathname);
    setJobId(null);
  };

  return (
    <>
      <IconDefs />
      <Topbar
        me={me}
        onDrawer={() => setDrawer(!drawer)}
        onHome={goHome}
        onLogout={async () => {
          await api.logout().catch(() => {});
          setMe(null);
          setItems([]);
          setDrawer(false);
        }}
      />
      {view === 'login' && <Login onLoggedIn={refresh} />}
      {view === 'home' && (
        <Home
          models={models}
          items={items}
          remaining={me?.remaining ?? 0}
          file={file}
          model={model}
          thinking={thinking}
          onFile={setFile}
          onModel={chooseModel}
          onThinking={setThinking}
          onOpen={openJob}
          onHistory={() => setDrawer(true)}
          toast={toast}
          onStarted={(job: Job, picked: File) => {
            files.current.set(job.id, picked);
            setFile(null);
            refresh();
            openJob(job.id);
          }}
        />
      )}
      {view === 'work' && jobId && (
        <Workspace
          id={jobId}
          onChanged={refresh}
          onHome={goHome}
          toast={toast}
          onRetry={() => {
            const picked = files.current.get(jobId);
            setFile(picked ?? null);
            goHome();
            toast(picked ? '选一个不同的思考强度，再开始翻译' : '重新选择这篇论文，再换一个思考强度');
          }}
        />
      )}
      {me && (
        <HistoryDrawer
          items={items}
          current={jobId}
          onClose={() => setDrawer(false)}
          onOpen={openJob}
          onNew={goHome}
          onDelete={async (id) => {
            try {
              await api.remove(id);
              setItems((list) => list.filter((i) => i.id !== id));
              if (id === jobId) goHome();
              toast('已从你的历史中删除');
            } catch (e) {
              toast((e as ApiError).message);
            }
          }}
        />
      )}
      {toastNode}
    </>
  );
}

interface WorkspaceProps {
  id: string;
  onChanged: () => void;
  onHome: () => void;
  onRetry: () => void;
  toast: (message: string) => void;
}

function Workspace({ id, onChanged, onHome, onRetry, toast }: WorkspaceProps) {
  const { job, error, run, status, restart } = useJob(id, onChanged);

  useEffect(() => {
    if (error && error.status !== 401) {
      toast(error.message);
      onHome();
    }
    // 只在出错时回首页；toast/onHome 每次渲染都是新函数，不作依赖
  }, [error]);

  if (!job || !status) return <main className="view v-work workspace" />;
  return (
    <main className="view v-work workspace">
      <Preview job={job} run={run} status={status} />
      <EventsCard
        job={job}
        run={run}
        status={status}
        onHome={onHome}
        onRetry={onRetry}
        onRerun={async (action) => {
          try {
            restart(await api.rerun(id, action));
            onChanged();
          } catch (e) {
            toast((e as ApiError).message);
          }
        }}
        onCancel={async () => {
          try {
            await api.cancel(id);
            onChanged();
            onHome();
            toast('已取消，本次不计入今日额度');
          } catch (e) {
            toast((e as ApiError).message);
          }
        }}
      />
    </main>
  );
}
