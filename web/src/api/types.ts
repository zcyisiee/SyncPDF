/**
 * 由运行中的 `bdt serve` 生成（`pnpm gen:api`）的 OpenAPI 类型，收窄成本模块的具名别名。
 * 生成命令：`PATH="$PWD/.venv/bin:$PATH" bdt serve --root tmp --port 8787` + `pnpm gen:api`。
 * 契约文本：docs/frontend/api.md（冲突时以运行中服务的 /openapi.json 为准）。
 */
import type { components } from './schema';

export type HealthResponse = components['schemas']['HealthResponse'];
export type DocumentListItem = components['schemas']['DocumentListItem'];
export type DocumentDetail = components['schemas']['DocumentDetail'];
export type StageStateItem = components['schemas']['StageStateItem'];
export type StageStateResponse = components['schemas']['StageStateResponse'];
export type ParagraphItem = components['schemas']['ParagraphItem'];
export type GeometryResponse = components['schemas']['GeometryResponse'];
export type CheckResponse = components['schemas']['CheckResponse'];
export type EventsPage = components['schemas']['EventsPage'];
export type ArtifactItem = components['schemas']['ArtifactItem'];
export type DocumentUploaded = components['schemas']['DocumentUploaded'];
export type JobRecord = components['schemas']['JobRecord'];
export type JobCreateRequest = components['schemas']['JobCreateRequest'] & { reviewer_profile?: string | null };
export type JobAccepted = components['schemas']['JobAccepted'];
export type ProfileListItem = components['schemas']['ProfileListItem'];
export type ProfileUpdateRequest = components['schemas']['ProfileUpdateRequest'];
export type DraftResponse = components['schemas']['DraftResponse'];
export type DraftParagraph = components['schemas']['DraftParagraph'];
export type DraftPatchRequest = components['schemas']['DraftPatchRequest'];
export type CandidateItem = components['schemas']['CandidateItem'];
export type CandidateJobAccepted = components['schemas']['CandidateJobAccepted'];
export type CandidateListResponse = components['schemas']['CandidateListResponse'];
export type CandidateRetranslateRequest = components['schemas']['CandidateRetranslateRequest'];
export type VersionItem = components['schemas']['VersionItem'];
export type VersionQuality = components['schemas']['VersionQuality'];
export type VersionsResponse = components['schemas']['VersionsResponse'];
export type GlossaryEntryModel = components['schemas']['GlossaryEntryModel'];
export type GlossaryResponse = components['schemas']['GlossaryResponse'];
export type GlossaryUpdateRequest = components['schemas']['GlossaryUpdateRequest'];

/**
 * 统一错误信封（docs/frontend/api.md §1）。唯一手写形状：FastAPI 的 OpenAPI 不导出异常响应
 * 模型（`ErrorEnvelope` 不在 `components.schemas` 里），所以按 pydantic 模型 \`ErrorBody\` 逐字段照抄。
 */
export interface ErrorEnvelope {
  error: { code: string; message: string; detail?: unknown };
}

/** `stage_summary`：7 个固定阶段 → 状态字符串（见 babeldoc_tools/serve/schemas.py::STAGES）。 */
export type StageSummary = DocumentListItem['stage_summary'];

/** /models metadata deliberately never contains the saved credential. */
export interface ModelConfiguration {
  id: string;
  label: string;
  base_url: string;
  model: string;
  has_api_key: boolean;
}
export type ModelUpdate = Omit<ModelConfiguration, 'has_api_key'> & { api_key?: string };
