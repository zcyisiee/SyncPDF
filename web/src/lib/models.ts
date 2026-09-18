import { useQuery } from '@tanstack/react-query';
import type { ModelConfiguration } from '../api/types';
import { ApiError, apiGet } from './api';

export const modelsKey = ['models'] as const;
export function useModels() {
  return useQuery({ queryKey: modelsKey, queryFn: () => apiGet<ModelConfiguration[]>('/models') });
}

/** Never display provider messages, validation detail, or request input near credentials. */
export function modelError(error: unknown): string {
  const messages: Record<string, string> = {
    model_auth: '认证失败，请检查已保存的 API Key。',
    model_address: '无法连接 Base URL 或地址发生重定向，请检查最终接口地址。',
    model_not_found: '接口地址或模型不存在，请检查 Base URL 和模型名称。',
    model_timeout: '连接超时，请稍后重试。',
    model_request: '服务商拒绝请求，请检查模型权限或额度。',
    model_response: '返回格式不是有效的 Chat Completions 响应。',
    model_invalid: '配置无效，请检查标识、名称、模型、地址及 API Key。',
    profile_collision: '此标识已被高级脚本配置使用，请换一个标识。',
    unknown_model: '配置已不存在，请刷新列表。',
    network_error: '无法连接本机后端，请检查 bdt serve。',
  };
  return error instanceof ApiError ? (messages[error.code] ?? '操作失败，请检查本机配置后重试。') : '操作失败，请重试。';
}
