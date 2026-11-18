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

/** 带上传进度的文件上传（fetch 无法观测上传进度，走 XHR）。
 * 大项目包（数 GB）上传耗时数分钟，进度可见是"有反应"的关键。 */
export function uploadWithProgress<T>(
  path: string,
  form: FormData,
  onProgress?: (loaded: number, total: number) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api${path}`);
    xhr.setRequestHeader("X-Actor", getActor());
    xhr.upload.onprogress = (e: ProgressEvent) => {
      if (e.lengthComputable) onProgress?.(e.loaded, e.total);
    };
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* 非 JSON 响应体 */
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as T);
        return;
      }
      const b = (body ?? {}) as { code?: string; message?: string; detail?: unknown };
      reject(
        new ApiError(
          xhr.status,
          b.code ?? `HTTP_${xhr.status}`,
          b.message ?? xhr.statusText ?? "上传失败",
          b.detail ?? null,
        ),
      );
    };
    xhr.onerror = () => reject(new Error("网络错误：上传中断，请重试"));
    xhr.onabort = () => reject(new Error("上传已取消"));
    xhr.send(form);
  });
}

export function pageImageUrl(documentId: number, pageNo: number, scale = 2): string {
  return `/api/documents/${documentId}/pages/${pageNo}/image?scale=${scale}`;
}

function withRun(path: string, runId?: number | null): string {
  return runId != null ? `${path}${path.includes("?") ? "&" : "?"}run_id=${runId}` : path;
}

export function reportUrl(projectId: string, runId?: number | null): string {
  return withRun(`/api/projects/${projectId}/report.html`, runId);
}

export function reportFormalUrl(projectId: string, runId?: number | null): string {
  return withRun(`/api/projects/${projectId}/report/formal.html`, runId);
}

export function reportDocxUrl(projectId: string, runId?: number | null): string {
  return withRun(`/api/projects/${projectId}/report/docx`, runId);
}
