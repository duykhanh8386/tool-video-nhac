from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from render.ffmpeg import RenderJob
from render.progress import ProgressParser, RenderProgress
from utils.logger import explain_ffmpeg_error, new_render_log
from utils.process import hidden_process_kwargs


class RenderWorker(QObject):
    progress = Signal(object)
    output_line = Signal(str)
    finished = Signal(str, str)
    failed = Signal(str, str)
    canceled = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.process: subprocess.Popen | None = None
        self.job: RenderJob | None = None
        self.parser: ProgressParser | None = None
        self.log_path: Path | None = None
        self._buffer = ""
        self._lines: list[str] = []
        self._canceling = False
        self._worker_active = False

    @property
    def running(self) -> bool:
        return self._worker_active

    def start(self, job: RenderJob) -> None:
        if self.running:
            raise RuntimeError("Một render khác đang chạy.")
        self.job = job
        self.parser = ProgressParser(job.duration)
        self.log_path = new_render_log()
        self._buffer = ""
        self._lines = []
        self._canceling = False
        self._worker_active = True
        try:
            self.process = subprocess.Popen(
                job.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **hidden_process_kwargs(),
            )
        except Exception:
            self._worker_active = False
            self.process = None
            raise
        threading.Thread(target=self._read_process, args=(self.process,), daemon=True).start()

    def cancel(self) -> None:
        if not self.running:
            return
        self._canceling = True
        process = self.process
        if process and process.poll() is None:
            process.terminate()
            timer = threading.Timer(3, self._force_kill)
            timer.daemon = True
            timer.start()

    def _force_kill(self) -> None:
        process = self.process
        if self.running and process and process.poll() is None:
            process.kill()

    def _read_process(self, process: subprocess.Popen) -> None:
        failure = ""
        try:
            if process.stdout:
                for text in process.stdout:
                    self._consume_output(text)
            exit_code = process.wait()
        except Exception as exc:
            exit_code = -1
            failure = str(exc)
        self._finish_process(process, exit_code, failure)

    def _consume_output(self, text: str) -> None:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            self._lines.append(line)
            self.output_line.emit(line)
            if self.parser:
                progress = self.parser.feed_line(line)
                if progress:
                    self.progress.emit(progress)

    def _finish_process(self, process: subprocess.Popen, exit_code: int, failure: str = "") -> None:
        if self._buffer:
            self._consume_output("\n")
        text = "\n".join(self._lines)
        if failure:
            text = (text + "\n" + failure).strip()
        if self.log_path:
            self.log_path.write_text("COMMAND\n" + " ".join(self.job.command if self.job else []) + "\n\nOUTPUT\n" + text, encoding="utf-8", errors="replace")
        output = self.job.output if self.job else None
        if self.process is process:
            self.process = None
        self._worker_active = False
        if self._canceling:
            if output and output.exists():
                try:
                    output.unlink()
                except OSError:
                    pass
            self.canceled.emit()
        elif exit_code == 0 and output and output.exists():
            self.progress.emit(RenderProgress(percent=100, current_seconds=self.job.duration, total_size=output.stat().st_size))
            self.finished.emit(str(output), str(self.log_path or ""))
        else:
            if output and output.exists():
                try:
                    output.unlink()
                except OSError:
                    pass
            self.failed.emit(explain_ffmpeg_error(text), str(self.log_path or ""))
