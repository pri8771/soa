import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import type { StatusTone } from "./status";

export interface ToastItem {
  id: number;
  title: string;
  tone: StatusTone;
}

interface ToastContextValue {
  publish: (title: string, options?: { tone?: StatusTone; durationMs?: number }) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function useToast(): ToastContextValue {
  const context = useContext(ToastContext);
  if (context === null) {
    throw new Error("useToast requires a <ToastProvider> ancestor");
  }
  return context;
}

/** Toast queue announced through an ARIA live region. */
export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const nextId = useRef(1);

  const publish = useCallback(
    (title: string, options?: { tone?: StatusTone; durationMs?: number }) => {
      const id = nextId.current++;
      setToasts((current) => [...current, { id, title, tone: options?.tone ?? "neutral" }]);
      const duration = options?.durationMs ?? 5000;
      setTimeout(() => {
        setToasts((current) => current.filter((toast) => toast.id !== id));
      }, duration);
    },
    [],
  );

  const value = useMemo(() => ({ publish }), [publish]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="soa-toast-region" role="region" aria-label="Notifications">
        <div aria-live="polite" aria-atomic="false">
          {toasts.map((toast) => (
            <div key={toast.id} className="soa-toast" data-tone={toast.tone}>
              {toast.title}
              <button
                type="button"
                className="soa-toast-dismiss"
                aria-label="Dismiss notification"
                onClick={() =>
                  setToasts((current) => current.filter((item) => item.id !== toast.id))
                }
              >
                ×
              </button>
            </div>
          ))}
        </div>
      </div>
    </ToastContext.Provider>
  );
}
