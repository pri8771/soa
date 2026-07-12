import { axe as rawAxe } from "vitest-axe";

/**
 * Axe with the color-contrast rule disabled: jsdom has no canvas, and
 * contrast is verified exhaustively by the token test suite instead.
 */
export function axe(container: Element) {
  return rawAxe(container, {
    rules: { "color-contrast": { enabled: false } },
  });
}
