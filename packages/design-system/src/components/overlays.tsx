import type { ReactNode } from "react";
import {
  Dialog as AriaDialog,
  type DialogProps as AriaDialogProps,
  DialogTrigger,
  Heading,
  Menu as AriaMenu,
  MenuItem as AriaMenuItem,
  type MenuItemProps,
  type MenuProps as AriaMenuProps,
  MenuTrigger,
  Modal,
  ModalOverlay,
  OverlayArrow,
  Popover as AriaPopover,
  type PopoverProps as AriaPopoverProps,
  Tooltip as AriaTooltip,
  type TooltipProps as AriaTooltipProps,
  TooltipTrigger,
} from "react-aria-components";

export { DialogTrigger, MenuTrigger, TooltipTrigger };

export interface TooltipProps extends Omit<AriaTooltipProps, "children"> {
  children: ReactNode;
}

export function Tooltip({ children, ...props }: TooltipProps) {
  return (
    <AriaTooltip {...props} className={`soa-tooltip ${props.className ?? ""}`}>
      <OverlayArrow>
        <svg width={8} height={8} viewBox="0 0 8 8" aria-hidden="true">
          <path d="M0 0 L4 4 L8 0" />
        </svg>
      </OverlayArrow>
      {children}
    </AriaTooltip>
  );
}

export interface PopoverProps extends Omit<AriaPopoverProps, "children"> {
  children: ReactNode;
}

export function Popover({ children, ...props }: PopoverProps) {
  return (
    <AriaPopover {...props} className={`soa-popover ${props.className ?? ""}`}>
      {children}
    </AriaPopover>
  );
}

export interface MenuProps<T extends object> extends AriaMenuProps<T> {}

export function Menu<T extends object>(props: MenuProps<T>) {
  return (
    <Popover>
      <AriaMenu {...props} className={`soa-menu ${props.className ?? ""}`} />
    </Popover>
  );
}

export function MenuItem(props: MenuItemProps & { children: ReactNode }) {
  return <AriaMenuItem {...props} className={`soa-menu-item ${props.className ?? ""}`} />;
}

export interface DialogProps extends Omit<AriaDialogProps, "children"> {
  title: string;
  children: AriaDialogProps["children"];
  /** Alert dialogs interrupt for consequential decisions. */
  alert?: boolean;
  isDismissable?: boolean;
}

/** Modal dialog with focus trap, Esc dismissal, and focus restoration. */
export function Dialog({
  title,
  children,
  alert = false,
  isDismissable = true,
  ...props
}: DialogProps) {
  return (
    <ModalOverlay className="soa-modal-overlay" isDismissable={isDismissable}>
      <Modal className="soa-modal">
        <AriaDialog
          {...props}
          role={alert ? "alertdialog" : "dialog"}
          className={`soa-dialog ${props.className ?? ""}`}
        >
          {(renderProps) => (
            <>
              <Heading slot="title" className="soa-dialog-title">
                {title}
              </Heading>
              {typeof children === "function" ? children(renderProps) : children}
            </>
          )}
        </AriaDialog>
      </Modal>
    </ModalOverlay>
  );
}

export interface DrawerProps extends Omit<AriaDialogProps, "children"> {
  title: string;
  children: ReactNode;
}

/** Right-side utility drawer (360–440px per UI_UX_BLUEPRINT §3.4). */
export function Drawer({ title, children, ...props }: DrawerProps) {
  return (
    <ModalOverlay className="soa-modal-overlay" isDismissable>
      <Modal className="soa-drawer">
        <AriaDialog {...props} className={`soa-dialog ${props.className ?? ""}`}>
          <Heading slot="title" className="soa-dialog-title">
            {title}
          </Heading>
          {children}
        </AriaDialog>
      </Modal>
    </ModalOverlay>
  );
}
