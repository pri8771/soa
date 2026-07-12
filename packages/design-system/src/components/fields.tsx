import type { ReactNode } from "react";
import {
  FieldError,
  Input,
  Label,
  Text,
  TextArea,
  TextField as AriaTextField,
  type TextFieldProps as AriaTextFieldProps,
  type ValidationResult,
} from "react-aria-components";

export interface TextFieldProps extends AriaTextFieldProps {
  label: string;
  description?: string;
  errorMessage?: string | ((validation: ValidationResult) => string);
  placeholder?: string;
  multiline?: boolean;
  children?: ReactNode;
}

/** Labeled text input with description and error slots wired for AT. */
export function TextField({
  label,
  description,
  errorMessage,
  placeholder,
  multiline = false,
  ...props
}: TextFieldProps) {
  return (
    <AriaTextField {...props} className={`soa-field ${props.className ?? ""}`}>
      <Label className="soa-field-label">{label}</Label>
      {multiline ? (
        <TextArea className="soa-field-input" placeholder={placeholder} rows={4} />
      ) : (
        <Input className="soa-field-input" placeholder={placeholder} />
      )}
      {description ? (
        <Text slot="description" className="soa-field-description">
          {description}
        </Text>
      ) : null}
      <FieldError className="soa-field-error">{errorMessage}</FieldError>
    </AriaTextField>
  );
}
