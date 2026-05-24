import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { useI18n } from "@/i18n";

export function DeleteConfirmDialog({
  cancelLabel,
  confirmLabel,
  description,
  loading,
  onCancel,
  onConfirm,
  open,
  title,
}: DeleteConfirmDialogProps) {
  const { t } = useI18n();

  return (
    <ConfirmDialog
      open={open}
      onCancel={onCancel}
      onConfirm={onConfirm}
      title={title}
      description={description}
      loading={loading}
      destructive
      confirmLabel={confirmLabel ?? t.common.delete}
      cancelLabel={cancelLabel ?? t.common.cancel}
    />
  );
}

interface DeleteConfirmDialogProps {
  cancelLabel?: string;
  confirmLabel?: string;
  /**
   * Required (KR-FE-CONFIRMDIALOG-PROP-AND-COCKPIT-A11Y-SWEEP) —
   * forwarded to ConfirmDialog.aria-describedby. Required for
   * the same screen-reader-announcement reason as the underlying
   * ConfirmDialog.
   */
  description: string;
  loading: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  open: boolean;
  title: string;
}
