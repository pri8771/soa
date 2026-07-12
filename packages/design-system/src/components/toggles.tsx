import type { ReactNode } from "react";
import {
  Checkbox as AriaCheckbox,
  type CheckboxProps as AriaCheckboxProps,
  Label,
  Radio as AriaRadio,
  RadioGroup as AriaRadioGroup,
  type RadioGroupProps as AriaRadioGroupProps,
  type RadioProps as AriaRadioProps,
  Switch as AriaSwitch,
  type SwitchProps as AriaSwitchProps,
} from "react-aria-components";

export interface CheckboxProps extends AriaCheckboxProps {
  children: ReactNode;
}

export function Checkbox(props: CheckboxProps) {
  return (
    <AriaCheckbox {...props} className={`soa-checkbox ${props.className ?? ""}`}>
      {({ isSelected, isIndeterminate }) => (
        <>
          <span aria-hidden="true" className="soa-checkbox-box" data-selected={isSelected}>
            {isIndeterminate ? "–" : isSelected ? "✓" : ""}
          </span>
          {props.children}
        </>
      )}
    </AriaCheckbox>
  );
}

export interface RadioGroupProps extends AriaRadioGroupProps {
  label: string;
  children: ReactNode;
}

export function RadioGroup({ label, children, ...props }: RadioGroupProps) {
  return (
    <AriaRadioGroup {...props} className={`soa-radio-group ${props.className ?? ""}`}>
      <Label className="soa-field-label">{label}</Label>
      {children}
    </AriaRadioGroup>
  );
}

export interface RadioProps extends AriaRadioProps {
  children: ReactNode;
}

export function Radio(props: RadioProps) {
  return (
    <AriaRadio {...props} className={`soa-radio ${props.className ?? ""}`}>
      {({ isSelected }) => (
        <>
          <span aria-hidden="true" className="soa-radio-dot" data-selected={isSelected} />
          {props.children}
        </>
      )}
    </AriaRadio>
  );
}

export interface SwitchProps extends AriaSwitchProps {
  children: ReactNode;
}

export function Switch(props: SwitchProps) {
  return (
    <AriaSwitch {...props} className={`soa-switch ${props.className ?? ""}`}>
      {({ isSelected }) => (
        <>
          <span aria-hidden="true" className="soa-switch-track" data-selected={isSelected}>
            <span className="soa-switch-thumb" />
          </span>
          {props.children}
        </>
      )}
    </AriaSwitch>
  );
}
