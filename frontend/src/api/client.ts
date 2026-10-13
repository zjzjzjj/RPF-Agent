/** API 客户端：统一错误信封、X-Actor 操作者标识。 */

const ACTOR_KEY = "bid-eval-actor";

export function getActor(): string {
  return localStorage.getItem(ACTOR_KEY) || "reviewer";
}

export function setActor(name: string): void {
  localStorage.setItem(ACTOR_KEY, name);
}

export class ApiError extends Error {
  code: string;
  detail: unknown;
  status: number;

  constructor(status: number, code: string, message: string, detail: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "X-Actor": getActor(),
    ...((init?.headers as Record<string, string>) || {}),
  };
  if (init?.body && !(init.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(`/api${path}`, { ...init, headers });
  if (!resp.ok) {
    let code = `HTTP_${resp.status}`;
    let message = resp.statusText;
    let detail: unknown = null;
    try {
      const body = await resp.json();
      code = body.code ?? code;
      message = body.message ?? message;
      detail = body.detail ?? null;
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new ApiError(resp.status, code, message, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export const api = {
  get: <T,>(path: string) => request<T>(path),
  post: <T,>(path: string, body?: unknown) =>
    request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T,>(path: string, body: unknown) =>
    request<T>(path, { method: "PATCH", body: JSON.stringify(body) }),
  del: <T,>(path: string) => request<T>(path, { method: "DELETE" }),
  upload: <T,>(path: string, form: FormData) => request<T>(path, { method: "POST", body: form }),
};

export function pageImageUrl(documentId: number, pageNo: number, scale = 2): string {
  return `/api/documents/${documentId}/pages/${pageNo}/image?scale=${scale}`;
}

export function reportUrl(projectId: string): string {
  return `/api/projects/${projectId}/report.html`;
}
