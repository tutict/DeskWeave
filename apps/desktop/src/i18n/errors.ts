import { RuntimeApiError, RuntimeUnavailableError } from "../lib/runtimeClient";
import { i18n, translateMessage } from "./index";

/**
 * A failure captured for display.
 *
 * Errors are kept *structured* instead of as pre-rendered strings: the message is
 * produced during render, so switching language re-renders a visible error in the
 * new language too.
 *
 * `detail` is the transport/runtime diagnostic (HTTP status, the server's own
 * sentence, a URL). It is intentionally not translated: it is evidence, and it is
 * what the user pastes into a bug report.
 */
export type AppErrorKind = "unavailable" | "api" | "unknown";

export interface AppError {
  kind: AppErrorKind;
  /** Stable machine code from the runtime, when there is one. */
  code: string | null;
  detail: string;
}

export function toAppError(caught: unknown): AppError {
  if (caught instanceof RuntimeUnavailableError) {
    return { kind: "unavailable", code: null, detail: caught.message };
  }
  if (caught instanceof RuntimeApiError) {
    return { kind: "api", code: caught.code, detail: caught.message };
  }
  if (caught instanceof Error) {
    return { kind: "unknown", code: null, detail: caught.message };
  }
  return { kind: "unknown", code: null, detail: String(caught) };
}

function codeText(code: string): string | null {
  const key = `errors.codes.${code}`;
  return i18n.global.te(key) ? translateMessage(key) : null;
}

/**
 * Localised text for a captured error.
 *
 * Order of work: a known code maps to a sentence; an unknown code falls back to a
 * generic sentence that *names* the code; the raw detail is appended when there
 * is one. Nothing is ever swallowed — an unmapped failure still shows its text.
 */
export function appErrorMessage(error: AppError | null): string {
  if (!error) return translateMessage("common.unknownError");

  if (error.kind === "unavailable") {
    return join(
      translateMessage("errors.unavailable"),
      error.detail ? translateMessage("errors.detail", { detail: error.detail }) : "",
    );
  }

  if (error.kind === "api" && error.code) {
    const mapped =
      codeText(error.code) ?? translateMessage("errors.codeUnknown", { code: error.code });
    return join(
      mapped,
      error.detail ? translateMessage("errors.detail", { detail: error.detail }) : "",
    );
  }

  return translateMessage("errors.unknown", { detail: error.detail });
}

/**
 * Localised text for a task-level failure stored in `task.error`.
 *
 * Known codes are explained in the active language. An unknown code has no
 * trustworthy explanation, so the runtime's own message is shown as-is rather than
 * inventing one.
 */
export function taskErrorText(code: string, detail: string): string {
  return codeText(code) ?? (detail || code);
}

function join(primary: string, secondary: string): string {
  return secondary ? `${primary} ${secondary}` : primary;
}
