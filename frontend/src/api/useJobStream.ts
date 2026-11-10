/** 任务进度订阅：SSE 优先（/jobs/{id}/events），失败自动降级为 useJob 轮询。
 *
 * 对外仍返回 JobOut，调用方与既有 useJob 用法完全一致（无需改类型）。
 * 后端 SSE 端点不依赖 X-Actor——EventSource 无法自定义请求头，故该端点刻意只读。
 */
import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "./client";
import { useJob } from "./hooks";
import type { JobOut } from "../types";

const TERMINAL = ["succeeded", "failed", "cancelled"];

/** 一旦 SSE 在本环境不可用（代理缓冲/浏览器策略），本会话整体降级，避免反复重连失败。 */
let sseDisabled = false;

export function useJobProgress(jobId: number | null): JobOut | null {
  const qc = useQueryClient();
  const [streamed, setStreamed] = useState<JobOut | null>(null);
  const [fallback, setFallback] = useState(sseDisabled);
  const closedRef = useRef(false);

  useEffect(() => {
    setStreamed(null);
    closedRef.current = false;
    if (jobId == null || fallback) return;

    let es: EventSource | null = null;
    try {
      es = new EventSource(`/api/jobs/${jobId}/events`);
    } catch {
      sseDisabled = true;
      setFallback(true);
      return;
    }

    es.addEventListener("progress", (ev) => {
      let d: Partial<JobOut>;
      try {
        d = JSON.parse((ev as MessageEvent).data);
      } catch {
        return;
      }
      setStreamed((prev) => ({ ...(prev ?? { id: jobId }), ...d } as JobOut));
      if (d.status && TERMINAL.includes(d.status)) {
        closedRef.current = true;
        es?.close();
        // 补一次完整 JobOut（含 project_id/attempts 等流里没推的字段）并写入缓存
        api
          .get<JobOut>(`/jobs/${jobId}`)
          .then((full) => {
            setStreamed(full);
            qc.setQueryData(["job", jobId], full);
          })
          .catch(() => {
            /* 终态已由流拿到，补全失败可忽略 */
          });
      }
    });

    es.addEventListener("error", () => {
      es?.close();
      sseDisabled = true;
      setFallback(true);
    });

    return () => {
      if (!closedRef.current) es?.close();
    };
  }, [jobId, fallback, qc]);

  // 兜底轮询：SSE 可用时禁用（jobId 传 null）
  const polled = useJob(fallback ? jobId : null);
  return fallback ? (polled.data ?? null) : streamed;
}
