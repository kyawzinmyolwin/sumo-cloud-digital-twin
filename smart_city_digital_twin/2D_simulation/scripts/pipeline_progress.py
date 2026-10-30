"""
Two-row stderr progress bars — shared format across the pipeline.

Row 1: ``(step): (current_file_name)``
Row 2: ``(progress_bar) ##0.0% (###,##0/###,##0)``
"""
from __future__ import annotations

import math
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

# --- Helpers ------------------------------------------------------------------

_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")
_DUAROUTER_STEP_RE = re.compile(r"Reading up to time step:\s*([\d.]+)")


def _fmt_n(n: int) -> str:
    """Thousands-separated integer (###,##0)."""
    return format(int(n), ",d")


def _fmt_pct(index: int, total: int) -> str:
    """Percent with one decimal (##0.0%)."""
    if total <= 0:
        frac = 1.0
    else:
        frac = min(1.0, index / total)
    return f"{100.0 * frac:.1f}%"


def _bar_and_pct_fragments(
    index: int,
    total: int,
    *,
    bar_width: int,
    color: bool | None = None,
) -> tuple[str, str]:
    if color is None:
        color = sys.stderr.isatty()
    if total <= 0:
        frac = 1.0
    else:
        frac = min(1.0, index / total)
    filled = int(round(bar_width * frac))
    filled = min(max(filled, 0), bar_width)
    empty = bar_width - filled
    pct_s = _fmt_pct(index, total)
    if color:
        bar_s = (
            f"\033[36m{'━' * filled}\033[0m"
            f"\033[90m{'─' * empty}\033[0m"
        )
        pct_s = f"\033[33m{pct_s}\033[0m"
    else:
        bar_s = "━" * filled + "─" * empty
    return bar_s, pct_s


def _visible_char_len(s: str) -> int:
    return len(_ANSI_SGR_RE.sub("", s))


def _stderr_progress_width() -> int:
    try:
        cols = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        cols = 80
    return max(48, cols - 2)


def _pad_current_to_total_width(current_s: str, total_s: str) -> str:
    return current_s.rjust(len(total_s))


def _count_suffix(index: int, total: int, *, compact: bool) -> str:
    if compact:
        def short(n: int) -> str:
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 10_000:
                return f"{n / 1000:.1f}k"
            return _fmt_n(n)

        tot_s = short(total)
        cur_s = _pad_current_to_total_width(short(index), tot_s)
    else:
        tot_s = _fmt_n(total)
        cur_s = _pad_current_to_total_width(_fmt_n(index), tot_s)
    return f" ({cur_s}/{tot_s})"


def _truncate_visible_plain(s: str, max_width: int) -> str:
    plain = _ANSI_SGR_RE.sub("", s)
    if len(plain) <= max_width:
        return s
    cut = max(1, max_width - 1)
    return plain[:cut] + "…"


def _ellipsis_end_filename(name: str, budget: int) -> str:
    if budget <= 0:
        return ""
    if len(name) <= budget:
        return name
    if budget <= 1:
        return "…"[:budget]
    return name[: budget - 1] + "…"


def _two_phase_progress_rows(
    step: str,
    filename: str,
    index: int,
    total: int,
    *,
    bar_width: int = 36,
    max_width: int | None = None,
) -> tuple[str, str]:
    """
    Row 1: ``step: filename``; row 2: ``bar NNN.N% (current/total)``.
    """
    bar_opts = (bar_width, 32, 28, 22, 18, 14, 12, 10, 8)

    fn_row1 = filename
    if max_width is not None:
        prefix_len = len(f"{step}: ")
        room = max_width - prefix_len
        if room < 1:
            fn_row1 = "…"
        elif len(filename) > room:
            fn_row1 = _ellipsis_end_filename(filename, room)

    def rows(bar_w: int, compact: bool) -> tuple[str, str]:
        bw = min(bar_w, bar_width)
        bar_s, pct_s = _bar_and_pct_fragments(
            index, total, bar_width=bw, color=None
        )
        suffix = _count_suffix(index, total, compact=compact)
        if sys.stderr.isatty():
            row1 = f"\033[93m{step}:\033[0m \033[97m{fn_row1}\033[0m"
        else:
            row1 = f"{step}: {fn_row1}"
        row2 = f"{bar_s} {pct_s}{suffix}"
        return row1, row2

    r1, r2 = rows(bar_width, False)
    if max_width is None:
        return r1, r2

    if (
        _visible_char_len(r1) <= max_width
        and _visible_char_len(r2) <= max_width
    ):
        return r1, r2

    for compact in (False, True):
        for bw in bar_opts:
            r1, r2 = rows(bw, compact)
            if (
                _visible_char_len(r1) <= max_width
                and _visible_char_len(r2) <= max_width
            ):
                return r1, r2

    r1, r2 = rows(8, True)
    if _visible_char_len(r2) > max_width:
        r2 = _truncate_visible_plain(r2, max_width)
    return r1, r2


def _fmt_duration(sec: float) -> str:
    """Human-readable duration (e.g. 14s, 2m 05s)."""
    if sec < 0 or not math.isfinite(sec):
        return "—"
    sec_i = int(sec + 0.5)
    if sec_i < 60:
        return f"{sec_i}s"
    minutes, seconds = divmod(sec_i, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def _colour_status_message(message: str, *, enable: bool | None = None) -> str:
    if enable is None:
        enable = sys.stderr.isatty()
    if not enable:
        return message
    if message.startswith("UPDATED:"):
        return f"\033[32mUPDATED:\033[0m{message[len('UPDATED:'):]}"
    if message.startswith("SKIPPED:"):
        return f"\033[31mSKIPPED:\033[0m{message[len('SKIPPED:'):]}"
    if message.startswith("COMPLETED:"):
        return f"\033[38;5;19mCOMPLETED:\033[0m{message[len('COMPLETED:'):]}"
    return message


# --- Pipeline progress API ----------------------------------------------------

PROGRESS_TICK_INTERVAL_SEC = 1.0


def _tick_refresh_due(
    index: int,
    total: int,
    last_paint_at: float,
    phase_key: str,
    last_phase_key: str,
    *,
    force: bool = False,
) -> bool:
    """True when the progress display should redraw (start/end, phase change, >=1s)."""
    if force:
        return True
    if phase_key != last_phase_key:
        return True
    if index <= 1 or (total > 0 and index >= total):
        return True
    if last_paint_at <= 0.0:
        return True
    return time.perf_counter() - last_paint_at >= PROGRESS_TICK_INTERVAL_SEC


class PipelineStepProgress:
    """Live two-row progress on stderr; no-op when disabled or not a TTY."""

    __slots__ = (
        "_can_rewrite",
        "_enabled",
        "_filename",
        "_index",
        "_last_paint_at",
        "_last_phase_key",
        "_step",
        "_term_cols",
        "_total",
    )

    def __init__(self, step: str, *, enabled: bool = True) -> None:
        self._step = step.upper()
        self._enabled = enabled and sys.stderr.isatty()
        self._can_rewrite = False
        self._term_cols: int | None = None
        self._filename = ""
        self._index = 0
        self._total = 1
        self._last_paint_at = 0.0
        self._last_phase_key = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _write_rows(self) -> None:
        if self._term_cols is None:
            self._term_cols = _stderr_progress_width()
        r1, r2 = _two_phase_progress_rows(
            self._step,
            self._filename,
            self._index,
            self._total,
            max_width=self._term_cols,
        )
        body = f"{r1}\033[K\n{r2}\033[K\n"
        if self._can_rewrite:
            sys.stderr.write(f"\033[2A\r{body}")
        else:
            sys.stderr.write(body)
            self._can_rewrite = True
        sys.stderr.flush()

    def tick(
        self,
        filename: str,
        index: int,
        total: int,
        *,
        force: bool = False,
    ) -> None:
        if not self._enabled:
            return
        self._filename = filename
        self._index = index
        self._total = total
        phase_key = self._step
        if not _tick_refresh_due(
            index,
            total,
            self._last_paint_at,
            phase_key,
            self._last_phase_key,
            force=force,
        ):
            return
        self._last_phase_key = phase_key
        self._last_paint_at = time.perf_counter()
        self._write_rows()

    def repaint(self) -> None:
        """Redraw the last index/total if >=1s elapsed (timer-driven subprocess updates)."""
        if not self._enabled or self._last_paint_at <= 0.0:
            return
        if not _tick_refresh_due(
            self._index,
            self._total,
            self._last_paint_at,
            self._step,
            self._last_phase_key,
        ):
            return
        self._last_paint_at = time.perf_counter()
        self._write_rows()

    def dismiss(self) -> None:
        if not self._enabled:
            return
        if self._can_rewrite:
            sys.stderr.write(
                "\033[2A\r\033[2K"
                "\033[1B\r\033[2K"
                "\033[1A"
            )
            self._can_rewrite = False
        sys.stderr.flush()

    def finish(self, message: str) -> None:
        if not self._enabled:
            print(message)
            return
        msg = message.rstrip("\r\n")
        coloured = _colour_status_message(msg)
        if self._can_rewrite:
            sys.stderr.write(f"\033[2A\r{coloured}\033[K\n\033[2K")
            self._can_rewrite = False
        else:
            sys.stderr.write(f"{coloured}\n")
        sys.stderr.flush()


class TripsStepProgress:
    """Trips-step progress — same two-row format as PipelineStepProgress."""

    __slots__ = (
        "_can_rewrite",
        "_default_name",
        "_display_name",
        "_enabled",
        "_index",
        "_last_paint_at",
        "_last_phase_key",
        "_step",
        "_t0",
        "_term_cols",
        "_total",
    )

    def __init__(self, *, enabled: bool = True, filename: str = "") -> None:
        self._enabled = enabled and sys.stderr.isatty()
        self._default_name = filename
        self._t0 = time.perf_counter()
        self._can_rewrite = False
        self._term_cols: int | None = None
        self._step = ""
        self._display_name = filename
        self._index = 0
        self._total = 1
        self._last_paint_at = 0.0
        self._last_phase_key = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._t0

    def _write_rows(self) -> None:
        if self._term_cols is None:
            self._term_cols = _stderr_progress_width()
        r1, r2 = _two_phase_progress_rows(
            self._step,
            self._display_name,
            self._index,
            self._total,
            max_width=self._term_cols,
        )
        body = f"{r1}\033[K\n{r2}\033[K\n"
        if self._can_rewrite:
            sys.stderr.write(f"\033[2A\r{body}")
        else:
            sys.stderr.write(body)
            self._can_rewrite = True
        sys.stderr.flush()

    def tick(
        self,
        step: str,
        index: int,
        total: int,
        *,
        filename: str | None = None,
        force: bool = False,
    ) -> None:
        if not self._enabled:
            return
        self._step = step
        self._display_name = filename if filename is not None else self._default_name
        self._index = index
        self._total = total
        phase_key = step
        if not _tick_refresh_due(
            index,
            total,
            self._last_paint_at,
            phase_key,
            self._last_phase_key,
            force=force,
        ):
            return
        self._last_phase_key = phase_key
        self._last_paint_at = time.perf_counter()
        self._write_rows()

    def repaint(self) -> None:
        """Redraw the last index/total if >=1s elapsed."""
        if not self._enabled or self._last_paint_at <= 0.0:
            return
        if not _tick_refresh_due(
            self._index,
            self._total,
            self._last_paint_at,
            self._step,
            self._last_phase_key,
        ):
            return
        self._last_paint_at = time.perf_counter()
        self._write_rows()

    def dismiss(self) -> None:
        if not self._enabled:
            return
        if self._can_rewrite:
            sys.stderr.write(
                "\033[2A\r\033[2K"
                "\033[1B\r\033[2K"
                "\033[1A"
            )
            self._can_rewrite = False
        sys.stderr.flush()

    def finish(self, message: str) -> None:
        msg = message.rstrip("\r\n")
        if msg.startswith("COMPLETED:") and "(" not in msg:
            msg = f"{msg} ({_fmt_duration(self.elapsed)})"
        if not self._enabled:
            print(msg)
            return
        coloured = _colour_status_message(msg)
        if self._can_rewrite:
            sys.stderr.write(f"\033[2A\r{coloured}\033[K\n\033[2K")
            self._can_rewrite = False
        else:
            sys.stderr.write(f"{coloured}\n")
        sys.stderr.flush()


def _subprocess_output_segments(chunk: str) -> list[str]:
    """Split SUMO console chunks on newlines and carriage returns."""
    return [s.strip() for s in re.split(r"[\r\n]+", chunk) if s.strip()]


def run_subprocess_with_progress(
    cmd: list[str],
    prog: PipelineStepProgress,
    filename: str,
    *,
    cwd: Path | str,
    on_line: Callable[[str], tuple[int, int] | None] | None = None,
    progress_total: int | None = None,
) -> None:
    """Run a command; echo all output; optional parser updates a step progress bar."""
    print("running:", " ".join(cmd))
    if not prog.enabled:
        subprocess.run(cmd, check=True, cwd=str(cwd))
        return

    total = max(int(progress_total or 0), 1)
    prog.tick(filename, 0, total, force=True)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=0,
        cwd=str(cwd),
    )
    assert proc.stdout is not None
    line_queue: queue.Queue[tuple[str, str | int]] = queue.Queue()
    keep_bar = on_line is not None

    def _reader() -> None:
        for raw in proc.stdout:
            for segment in _subprocess_output_segments(raw):
                line_queue.put(("line", segment))
        line_queue.put(("rc", proc.wait()))

    threading.Thread(target=_reader, daemon=True).start()
    rc = 0
    while True:
        try:
            kind, payload = line_queue.get(timeout=PROGRESS_TICK_INTERVAL_SEC)
        except queue.Empty:
            prog.repaint()
            continue
        if kind == "rc":
            rc = int(payload)
            break
        for line in _subprocess_output_segments(str(payload)):
            parsed = None
            if on_line is not None:
                parsed = on_line(line)
            if parsed is None and progress_total and _DUAROUTER_STEP_RE.search(line):
                parsed = parse_duarouter_timestep(line, total)
            if parsed is not None:
                idx, tick_total = parsed
                prog.tick(filename, idx, tick_total)
                continue
            if line:
                if not keep_bar:
                    prog.dismiss()
                print(line)
    if rc != 0:
        prog.dismiss()
        raise subprocess.CalledProcessError(rc, cmd)
    prog.tick(filename, total, total, force=True)


def parse_duarouter_timestep(line: str, horizon: int) -> tuple[int, int] | None:
    m = _DUAROUTER_STEP_RE.search(line)
    if not m or horizon <= 0:
        return None
    step = int(float(m.group(1)))
    return min(step, horizon), horizon
