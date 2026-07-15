/**
 * Command palette (DSN-005): Ctrl/Cmd+K, combobox semantics, permission-
 * filtered commands. The trigger button in the top bar advertises the
 * shortcut so it stays discoverable.
 */

import { useNavigate } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { buildNavigationCommands, filterCommands, type Command } from "./commands";
import { useShellSession } from "./ShellContext";

export function CommandPaletteTrigger({ onOpen }: { onOpen: () => void }) {
  return (
    <button type="button" className="soa-palette-trigger" onClick={onOpen}>
      {/* The label hides at narrow widths (shell.css); the kbd hint stays. */}
      <span className="soa-palette-trigger-label">Search or jump to…</span>
      <kbd className="soa-palette-kbd">Ctrl K</kbd>
    </button>
  );
}

export function CommandPalette({ extraCommands = [] }: { extraCommands?: Command[] }) {
  const session = useShellSession();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);

  const commands = useMemo(() => {
    const navCommands = buildNavigationCommands(session, (to, params) => {
      void navigate({ to: to as "/", params: params as never });
    });
    const permitted = extraCommands.filter(
      (command) => !command.permission || session.permissions.has(command.permission),
    );
    return [...navCommands, ...permitted];
  }, [session, navigate, extraCommands]);

  const results = useMemo(() => filterCommands(commands, query), [commands, query]);

  const close = useCallback(() => {
    setOpen(false);
    setQuery("");
    setActiveIndex(0);
    previousFocus.current?.focus();
  }, []);

  const openPalette = useCallback(() => {
    // Re-triggering while open must not overwrite the focus-restore target
    // with the palette's own soon-unmounted input.
    setOpen((already) => {
      if (!already) {
        previousFocus.current = document.activeElement as HTMLElement | null;
      }
      return true;
    });
  }, []);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        openPalette();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [openPalette]);

  useEffect(() => {
    if (open) {
      inputRef.current?.focus();
    }
  }, [open]);

  if (!open) {
    return <CommandPaletteTrigger onOpen={openPalette} />;
  }

  const activeId = results[activeIndex] ? `soa-cmd-${results[activeIndex].id}` : undefined;

  return (
    <>
      <CommandPaletteTrigger onOpen={openPalette} />
      <div
        className="soa-palette-overlay"
        role="presentation"
        onClick={(event) => {
          if (event.target === event.currentTarget) {
            close();
          }
        }}
      >
        <div
          className="soa-palette"
          role="dialog"
          aria-modal="true"
          aria-label="Command palette"
          onKeyDown={(event) => {
            // Dialog-level guards: Escape closes from anywhere inside, and
            // Tab is trapped on the input — the palette is a single-field
            // modal, so focus never escapes behind the overlay.
            if (event.key === "Escape") {
              event.preventDefault();
              close();
            } else if (event.key === "Tab") {
              event.preventDefault();
              inputRef.current?.focus();
            }
          }}
        >
          <input
            ref={inputRef}
            className="soa-palette-input"
            role="combobox"
            aria-expanded="true"
            aria-controls="soa-palette-listbox"
            aria-activedescendant={activeId}
            aria-label="Search commands"
            placeholder="Type a command or destination…"
            value={query}
            onChange={(event) => {
              setQuery(event.target.value);
              setActiveIndex(0);
            }}
            onKeyDown={(event) => {
              if (event.key === "ArrowDown") {
                event.preventDefault();
                setActiveIndex((index) => Math.min(index + 1, results.length - 1));
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setActiveIndex((index) => Math.max(index - 1, 0));
              } else if (event.key === "Enter" && results[activeIndex]) {
                event.preventDefault();
                results[activeIndex].perform();
                close();
              } else if (event.key === "Escape") {
                event.preventDefault();
                close();
              }
            }}
          />
          <ul
            id="soa-palette-listbox"
            role="listbox"
            aria-label="Commands"
            className="soa-palette-list"
          >
            {results.length === 0 ? (
              <li className="soa-palette-empty" role="presentation">
                No matching commands. Try a navigation target like “documents”.
              </li>
            ) : (
              results.map((command, index) => (
                <li
                  key={command.id}
                  id={`soa-cmd-${command.id}`}
                  role="option"
                  aria-selected={index === activeIndex}
                  className="soa-palette-item"
                  data-active={index === activeIndex}
                  onMouseEnter={() => setActiveIndex(index)}
                  onClick={() => {
                    command.perform();
                    close();
                  }}
                >
                  <span>{command.label}</span>
                  <span className="soa-palette-section">{command.section}</span>
                </li>
              ))
            )}
          </ul>
        </div>
      </div>
    </>
  );
}
