/**
 * 统一的响应读取：**绝不让 `Unexpected token 'I', "Internal S"... is not valid JSON` 冒到页面上**。
 *
 * 2026-09-13 用户实测：后端某个未处理异常被 FastAPI 回成纯文本 `Internal Server Error`，
 * 前端直接 `response.json()`，于是页面只显示一句 JSON 解析错误 —— 不知道是哪个接口、什么原因。
 * 这里把「非 2xx」和「不是 JSON」两种情况都翻成人话，并把后端的 detail（现在 500 也是 JSON）
 * 带出来。
 */

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export async function readJson<T>(input: Response | Promise<Response>, fallback = "请求失败"): Promise<T> {
  const response = await input;
  const status = response.status;
  if (!response.ok) {
    let raw = "";
    try {
      raw = (await response.text()).trim();
    } catch {
      // 读不出正文就只用状态码
    }
    let detail = raw.slice(0, 300);
    if (detail.startsWith("{")) {
      try {
        const parsed = JSON.parse(detail) as { detail?: unknown; error?: unknown };
        detail = String(parsed?.detail || parsed?.error || detail);
      } catch {
        // 保留原始文本
      }
    }
    throw new ApiError(detail ? `HTTP ${status}：${detail}` : `${fallback}（HTTP ${status}）`, status);
  }
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(`${fallback}：服务返回的不是 JSON（HTTP ${status}）`, status);
  }
}

/** 204（无内容）返回 null，其余一律走 `readJson`。 */
export async function readJsonOrNull<T>(
  input: Response | Promise<Response>,
  fallback = "请求失败",
): Promise<T | null> {
  const response = await input;
  if (response.status === 204) return null;
  return readJson<T>(response, fallback);
}
