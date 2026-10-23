import { useEffect, useRef, type ReactNode } from "react";

// The native <dialog>: focus trap, Escape and the backdrop come with it.
export function Dialog({ title, wide, onClose, children }: { title: string; wide?: boolean; onClose: () => void; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);

  // No close() on unmount: React removing the element takes it out of the top layer, and a
  // close() there would queue a `close` event that fires onClose for the next dialog.
  // showModal() focuses the first focusable element, which is the close button; a field marked
  // data-autofocus takes it instead.
  useEffect(() => {
    const dialog = ref.current;
    if (dialog && !dialog.open) dialog.showModal();
    dialog?.querySelector<HTMLElement>("[data-autofocus]")?.focus();
  }, []);

  return (
    <dialog ref={ref} className={wide ? "dialog wide" : "dialog"} aria-labelledby="dialog-title" onClose={onClose} onCancel={onClose}>
      <header className="dialog-header">
        <h2 id="dialog-title">{title}</h2>
        <button type="button" className="icon-button" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </header>
      {children}
    </dialog>
  );
}

export function Confirm({
  title,
  children,
  action,
  danger,
  busy,
  onConfirm,
  onClose,
}: {
  title: string;
  children: ReactNode;
  action: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  return (
    <Dialog title={title} onClose={onClose}>
      <div className="dialog-body">{children}</div>
      <footer className="dialog-footer">
        <button type="button" className="button" onClick={onClose}>
          Cancel
        </button>
        <button type="button" className={danger ? "button danger" : "button primary"} disabled={busy} onClick={onConfirm}>
          {action}
        </button>
      </footer>
    </Dialog>
  );
}
