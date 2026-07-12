import type { ReactNode } from "react";
import {
  Button,
  ComboBox as AriaComboBox,
  type ComboBoxProps as AriaComboBoxProps,
  FieldError,
  Input,
  Label,
  ListBox,
  ListBoxItem,
  type ListBoxItemProps,
  Popover,
  Select as AriaSelect,
  type SelectProps as AriaSelectProps,
  SelectValue,
} from "react-aria-components";

export interface SelectOption {
  id: string;
  label: string;
}

export interface SelectProps<T extends SelectOption> extends Omit<AriaSelectProps<T>, "children"> {
  label: string;
  items: Iterable<T>;
  errorMessage?: string;
}

export function Select<T extends SelectOption>({
  label,
  items,
  errorMessage,
  ...props
}: SelectProps<T>) {
  return (
    <AriaSelect {...props} className={`soa-field ${props.className ?? ""}`}>
      <Label className="soa-field-label">{label}</Label>
      <Button className="soa-select-trigger">
        <SelectValue />
        <span aria-hidden="true" className="soa-select-caret">
          ▾
        </span>
      </Button>
      <FieldError className="soa-field-error">{errorMessage}</FieldError>
      <Popover className="soa-popover">
        <ListBox className="soa-listbox" items={items}>
          {(item) => <Option>{item.label}</Option>}
        </ListBox>
      </Popover>
    </AriaSelect>
  );
}

export interface ComboBoxProps<T extends SelectOption> extends Omit<
  AriaComboBoxProps<T>,
  "children"
> {
  label: string;
  items: Iterable<T>;
  errorMessage?: string;
}

export function ComboBox<T extends SelectOption>({
  label,
  items,
  errorMessage,
  ...props
}: ComboBoxProps<T>) {
  return (
    <AriaComboBox {...props} className={`soa-field ${props.className ?? ""}`}>
      <Label className="soa-field-label">{label}</Label>
      <div className="soa-combobox-frame">
        <Input className="soa-field-input" />
        <Button className="soa-combobox-button" aria-label="Show suggestions">
          ▾
        </Button>
      </div>
      <FieldError className="soa-field-error">{errorMessage}</FieldError>
      <Popover className="soa-popover">
        <ListBox className="soa-listbox" items={items}>
          {(item) => <Option>{item.label}</Option>}
        </ListBox>
      </Popover>
    </AriaComboBox>
  );
}

export function Option(props: ListBoxItemProps & { children: ReactNode }) {
  return <ListBoxItem {...props} className="soa-option" />;
}
