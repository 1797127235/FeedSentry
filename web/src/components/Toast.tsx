import { useEffect, useRef } from "react";

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
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!message) {
      return;
    }
    const timer = window.setTimeout(() => onCloseRef.current(), durationMs);
    return () => window.clearTimeout(timer);
  }, [message, durationMs]);

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
