import { useCallback, useState } from "react";

/** Each tab group has its own key and accepts only its current valid choices. */
export function useRememberedTab<T extends string>(key: string, choices: readonly T[], fallback: T) {
  const [value, setValue] = useState<T>(() => {
    try {
      const saved = localStorage.getItem(key);
      if (saved && choices.includes(saved as T)) return saved as T;
    } catch { /* Storage may be unavailable. */ }
    return fallback;
  });
  const select = useCallback((next: T) => {
    setValue(next);
    try { localStorage.setItem(key, next); } catch { /* Keep the page usable. */ }
  }, [key]);
  return [value, select] as const;
}
