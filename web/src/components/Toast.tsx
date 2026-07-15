import { useEffect } from "react";

export type ToastMessage = {
  kind: "success" | "error";
  text: string;
};

type ToastProps = {
  message: ToastMessage | null;
  onClose: () => void;
  durationMs?: number;
};

export function Toast({ message, onClose, durationMs = 3200 }: ToastProps) {
  useEffect(() => {
    if (!message) {
      return;
    }
    const timer = window.setTimeout(onClose, durationMs);
    return () => window.clearTimeout(timer);
  }, [message, onClose, durationMs]);

  if (!message) {
    return null;
  }

  return (
    <div
      className={`toast toast-${message.kind}`}
      role={message.kind === "error" ? "alert" : "status"}
    >
      <span>{message.text}</span>
      <button type="button" className="toast-close" onClick={onClose}>
        关闭
      </button>
    </div>
  );
}
