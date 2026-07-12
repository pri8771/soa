import { Button as AriaButton, type ButtonProps as AriaButtonProps } from "react-aria-components";

export type ButtonVariant = "primary" | "secondary" | "subtle" | "destructive";
export type ButtonSize = "sm" | "md";

export interface ButtonProps extends AriaButtonProps {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

/** Real <button> semantics via react-aria; never a clickable div. */
export function Button({ variant = "secondary", size = "md", ...props }: ButtonProps) {
  return (
    <AriaButton
      {...props}
      data-variant={variant}
      data-size={size}
      className={`soa-button ${props.className ?? ""}`}
    />
  );
}
