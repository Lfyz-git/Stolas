"""Small terminal renderer; plain output retains the same layout as a TTY."""
import os
import shutil
import sys
import textwrap


class Terminal:
    colors = {"heading": "36", "success": "32", "warning": "33", "error": "31", "prompt": "36"}

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout

    @property
    def width(self):
        return max(32, min(80, shutil.get_terminal_size((80, 24)).columns))

    def style(self, value, kind):
        enabled = self.stream.isatty() and "NO_COLOR" not in os.environ and os.getenv("TERM", "") not in ("", "dumb")
        return f"\033[{self.colors[kind]}m{value}\033[0m" if enabled else value

    def line(self, value="", kind=None):
        # Strip control characters from discovered names and pasted values.
        value = "".join(c for c in str(value) if c == "\n" or c.isprintable())
        for paragraph in value.split("\n"):
            for row in textwrap.wrap(paragraph, width=self.width, replace_whitespace=False) or [""]:
                print(self.style(row, kind) if kind else row, file=self.stream)

    def stage(self, title, step=None):
        self.line()
        self.line("─" * self.width, "heading")
        self.line("STOLAS · " + title, "heading")
        if step:
            self.line(f"Шаг {step} из 4", "heading")
        self.line("─" * self.width, "heading")
        self.line()

    def result(self, value, kind="success"):
        self.line(("Ошибка: " if kind == "error" else "Внимание: " if kind == "warning" else "Готово: ") + value, kind)


def ui():
    # Resolve stdout at call time to support redirection and embedded use.
    return Terminal()
