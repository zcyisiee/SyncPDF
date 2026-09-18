import { useEffect, useState } from 'react';

import { describeApiError } from '../../lib/api';
import { useDocument, useDraft, useExportMutation, useJobs } from '../../lib/queries';
import { Button } from '../ui/Button';

export function ExportButton({ did }: { did: string }) {
  const draft = useDraft(did);
  const document = useDocument(did);
  const jobs = useJobs(did);
  const mutation = useExportMutation(did);
  const storageKey = `bdt.export.${did}`;
  const [jobId, setJobId] = useState<string | null>(() => sessionStorage.getItem(storageKey));
  const job = jobs.data?.find((item) => item.job_id === jobId);
  const active = jobs.data?.some((item) => item.status === 'queued' || item.status === 'running');
  useEffect(() => {
    if (job?.status !== 'succeeded' || jobId === null) return;
    sessionStorage.removeItem(storageKey);
    const link = window.document.createElement('a');
    link.href = `/api/v1/documents/${encodeURIComponent(did)}/exports/latest`;
    link.download = `${did}.pdf`;
    link.click();
  }, [did, job?.status, jobId, storageKey]);
  const changed = Object.keys(draft.data?.paragraphs ?? {}).length;
  const failure = job?.status === 'failed' || job?.status === 'interrupted' || job?.status === 'canceled';
  return (
    <span className="flex items-center gap-s2">
      <Button disabled={active || mutation.isPending || !draft.data} onClick={() => {
        if (!draft.data) return;
        mutation.mutate(draft.data.revision, { onSuccess: (accepted) => {
          sessionStorage.setItem(storageKey, accepted.job_id);
          setJobId(accepted.job_id);
        } });
      }}>
        {active && jobId ? '正在导出…' : `导出最新 PDF${changed ? `（${changed} 块修改）` : ''}`}
      </Button>
      {failure || mutation.error ? <span role="alert" className="text-micro text-err">
        {mutation.error ? describeApiError(mutation.error).message : job?.error_message ?? '导出失败，请重试'}
      </span> : null}
      {document.data?.export_revision != null ? <a
        href={`/api/v1/documents/${encodeURIComponent(did)}/exports/latest?allow_previous=true`}
        download className="text-micro underline"
      >下载上次成功版本 r{document.data.export_revision}</a> : null}
    </span>
  );
}
