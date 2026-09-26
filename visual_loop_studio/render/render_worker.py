from __future__ import annotations

import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from render.ffmpeg import RenderJob
from render.progress import ProgressParser, RenderProgress
from utils.logger import explain_ffmpeg_error, new_render_log


class RenderWorker(QObject):
    progress = Signal(object)
    output_line = Signal(str)
    finished = Signal(str, str)
    failed = Signal(str, str)
    canceled = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.process: QProcess | None = None
        self.job: RenderJob | None = None
        self.parser: ProgressParser | None = None
        self.log_path: Path | None = None
        self._buffer = ""
        self._lines: list[str] = []
        self._canceling = False

    @property
    def running(self) -> bool:
        return bool(self.process and self.process.state() != QProcess.ProcessState.NotRunning)

    def start(self, job: RenderJob) -> None:
        if self.running:
            raise RuntimeError("Một render khác đang chạy.")
        self.job = job
        self.parser = ProgressParser(job.duration)
        self.log_path = new_render_log()
        self._buffer = ""
        self._lines = []
        self._canceling = False
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self.process.start(job.command[0], job.command[1:])

    def cancel(self) -> None:
        if not self.running:
            return
        self._canceling = True
        self.process.terminate()
        QTimer.singleShot(3000, self._force_kill)

    def _force_kill(self) -> None:
        if self.running:
            self.process.kill()

    def _read_output(self) -> None:
        if not self.process:
            return
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
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

    def _finished(self, exit_code: int, _status) -> None:
        self._read_output()
        text = "\n".join(self._lines)
        if self.log_path:
            self.log_path.write_text("COMMAND\n" + " ".join(self.job.command if self.job else []) + "\n\nOUTPUT\n" + text, encoding="utf-8", errors="replace")
        output = self.job.output if self.job else None
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
        self.process = None

    def _process_error(self, _error) -> None:
        if self.process and self.process.state() == QProcess.ProcessState.NotRunning and not self._canceling:
            message = self.process.errorString()
            self.failed.emit(message, str(self.log_path or ""))
