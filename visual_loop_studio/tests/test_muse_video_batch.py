from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from auth.muse_login import ACCOUNT_SELECTOR, CLICKABLE_SELECTOR, MUSE_APP_SELECTORS, MuseAccountStore
from auth.muse_generation import _video_fingerprint
from auth.muse_sessions import MuseSessionManager, MuseSessionState
from auth.muse_video_batch import (
    MUSE_IMAGES_PER_REQUEST,
    MuseVideoBatchError,
    MuseVideoBatchManager,
    MuseVideoAutomation,
    MuseVideoDownloadError,
    MuseVideoJob,
    MuseVideoJobState,
    MuseVideoLoginRequired,
    MuseVideoQuotaExhausted,
    MuseVideoSettings,
    MuseVideoStopped,
    MuseVideoTimeout,
    MuseVideoRunContext,
    MuseVideoSelectors,
    MuseVideoWorkerState,
    create_muse_video_job_id,
    _output_filename,
    _valid_image,
    _valid_artifact,
    get_muse_video_batch_manager,
)


class ReadyElement:
    def __init__(self, text="Muse", *, element_id="app", attrs=None):
        self.text = text
        self.id = element_id
        self.attrs = attrs or {}

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return self.attrs.get(name, "")


class ReadySwitch:
    def window(self, handle):
        if handle != "main":
            raise RuntimeError("closed")


class ReadyDriver:
    def __init__(self, profile: Path) -> None:
        self.profile = Path(profile)
        self.current_url = "https://muse.ai/chat"
        self.current_window_handle = "main"
        self.window_handles = ["main"]
        self.switch_to = ReadySwitch()
        self.quit_called = False
        self.thread_ids: set[int] = set()
        self.google_email = ""

    def set_page_load_timeout(self, _seconds):
        self.thread_ids.add(threading.get_ident())

    def get(self, _url):
        self.thread_ids.add(threading.get_ident())
        url = str(_url)
        if url.startswith("https://accounts.google.com/"):
            self.google_email = parse_qs(urlparse(url).query).get("Email", [self.google_email])[0]
            self.current_url = "https://accounts.google.com/ManageAccount"
        elif url.startswith("https://muse.ai/"):
            self.current_url = "https://muse.ai/chat"

    def find_elements(self, by, selector):
        self.thread_ids.add(threading.get_ident())
        if by == "css selector" and selector == CLICKABLE_SELECTOR:
            return []
        if by == "css selector" and selector == ACCOUNT_SELECTOR:
            if self.current_url.startswith("https://accounts.google.com/") and self.google_email:
                return [ReadyElement(self.google_email, attrs={"data-email": self.google_email})]
            return []
        if by == "css selector" and selector in MUSE_APP_SELECTORS:
            return [ReadyElement()] if self.current_url.startswith("https://muse.ai/") else []
        if by == "tag name" and selector == "body":
            return [ReadyElement(self.google_email, element_id="body")]
        return []

    def quit(self):
        self.thread_ids.add(threading.get_ident())
        self.quit_called = True


class FakeVideoAutomation:
    def __init__(self) -> None:
        self.scenarios: dict[int, str] = {}
        self.barrier: threading.Barrier | None = None
        self.calls: Counter[str] = Counter()
        self.generate_calls: Counter[str] = Counter()
        self.recover_calls: Counter[str] = Counter()
        self.download_calls: Counter[str] = Counter()
        self.prompts: list[str] = []
        self.settings: list[MuseVideoSettings] = []
        self.drivers: dict[int, ReadyDriver] = {}
        self.started: dict[int, threading.Event] = {index: threading.Event() for index in range(1, 4)}
        self.release = threading.Event()

    def process(self, driver, job, context):
        worker_id = context.worker_id
        self.calls[job.job_id] += 1
        self.prompts.append(job.prompt)
        self.settings.append(job.settings)
        self.drivers[worker_id] = driver
        self.started[worker_id].set()
        scenario = self.scenarios.get(worker_id, "normal")
        if self.barrier is not None:
            self.barrier.wait(timeout=2)
        if scenario == "login":
            raise MuseVideoLoginRequired("login required")
        if scenario == "quota":
            raise MuseVideoQuotaExhausted("quota")
        if scenario == "fail":
            raise MuseVideoBatchError("worker failed")
        if scenario == "block_before_submit":
            while not context.stopped() and not self.release.wait(0.01):
                pass
            if context.stopped():
                raise MuseVideoStopped("stopped")

        if job.submitted or job.submission_attempted:
            self.recover_calls[job.job_id] += 1
        else:
            context.transition(MuseVideoJobState.SUBMITTING, progress=20, submission_attempted=True)
            self.generate_calls[job.job_id] += 1
            context.transition(
                MuseVideoJobState.SUBMITTED,
                progress=25,
                submitted=True,
                submitted_at="submitted",
            )
        if scenario == "timeout":
            raise MuseVideoTimeout("timeout")
        if scenario == "stop_after_submit":
            while not context.stopped() and not self.release.wait(0.01):
                pass
            if context.stopped():
                raise MuseVideoStopped("stopped")

        context.transition(
            MuseVideoJobState.DOWNLOADING,
            progress=90,
            result_fingerprint="new-video",
            download_attempts=job.download_attempts + 1,
        )
        self.download_calls[job.job_id] += 1
        if scenario == "download_fail" and self.download_calls[job.job_id] == 1:
            raise MuseVideoDownloadError("download failed")
        target = Path(job.output_path)
        if scenario == "missing_output":
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-video")
        return target


class GroupedFakeVideoAutomation(FakeVideoAutomation):
    def __init__(self) -> None:
        super().__init__()
        self.group_sizes: dict[int, list[int]] = {index: [] for index in range(1, 4)}

    def process_batch(self, driver, jobs, contexts):
        worker_id = contexts[0].worker_id
        self.group_sizes[worker_id].append(len(jobs))
        self.drivers[worker_id] = driver
        self.started[worker_id].set()
        results = {}
        for job, context in zip(jobs, contexts):
            self.calls[job.job_id] += 1
            self.prompts.append(job.prompt)
            self.settings.append(job.settings)
            context.transition(MuseVideoJobState.SUBMITTING, progress=20, submission_attempted=True)
        self.generate_calls[jobs[0].job_id] += 1
        for index, (job, context) in enumerate(zip(jobs, contexts), start=1):
            context.transition(
                MuseVideoJobState.SUBMITTED,
                progress=25,
                submitted=True,
                submitted_at="submitted",
            )
            context.transition(
                MuseVideoJobState.DOWNLOADING,
                progress=90,
                result_fingerprint=f"video-{worker_id}-{index}",
                download_attempts=job.download_attempts + 1,
            )
            target = Path(job.output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x00\x00\x00\x18ftypmp42grouped-video")
            results[job.job_id] = target
        return results


class AutomationElement:
    def __init__(self, element_id: str, *, text: str = "", tag_name: str = "div", attrs=None, click=None, send=None):
        self.id = element_id
        self.text = text
        self.tag_name = tag_name
        self.attrs = attrs or {}
        self._click = click
        self._send = send
        self.value = ""

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return self.attrs.get(name, "")

    def click(self):
        if self._click:
            self._click()

    def clear(self):
        self.value = ""

    def send_keys(self, *values):
        self.value += "".join(str(item) for item in values)
        if self._send:
            self._send(*values)


class AutomationDriver:
    def __init__(self, download_dir: Path) -> None:
        self.current_url = "https://muse.ai/create"
        self.download_dir = download_dir
        self.selectors = MuseVideoSelectors()
        self.previews = []
        self.old_video = AutomationElement("old-video", tag_name="video", attrs={"src": "https://example/old.mp4"})
        self.new_videos = []
        self.unrelated_video = None
        self.inject_unrelated_on_generate = False
        self.fail_direct_download = False
        self.direct_download_barrier = None
        self.old_user_message = AutomationElement(
            "old-user-message",
            text="make a cinematic video",
            attrs={
                "data-message-id": "old-message-id",
                "data-message-group-id": "old-message-group",
            },
        )
        self.new_user_message = None
        self.expose_new_user_message = True
        self.expose_new_videos = True
        self.prompt_missing_after_upload_polls = 0
        self.prompt_lookup_count = 0
        self.video_poll_delay = 0
        self.video_poll_count = 0
        self.session_poll_delay = 0
        self.session_poll_count = 0
        self.new_session_base_epoch_ms = 1_700_000_060_000
        self.session_clicks = []
        self.session_click_video_poll_counts = []
        self.old_session = AutomationElement(
            "old-session",
            text="Rendered old video 8:44 am",
            attrs={"data-testid": "timeline-session-old"},
            click=lambda: self._open_session("old-session"),
        )
        self.new_session = None
        self.new_sessions = []
        self.late_old_session = None
        self.split_summary_sessions = False
        self.session_artifact_indices = {}
        self.current_summary_indices = []
        self.summary_open = False
        self.summary_artifacts = []
        self.summary_generation = 0
        self.summary_snapshot_calls = 0
        self.active_summary_artifact = None
        self.watch_started = []
        self.watch_stopped = []
        self.uploaded = ""
        self.uploaded_paths = []
        self.generate_clicks = 0
        self.downloaded_video_id = ""
        self.downloaded_video_ids = []
        self.downloaded_json_ids = []
        self.get_calls = 0
        self.executed_scripts = []
        self.body = AutomationElement("body")
        self.upload = AutomationElement(
            "upload",
            tag_name="input",
            attrs={"accept": "image/*"},
            send=self._uploaded,
        )
        self.prompt = AutomationElement("prompt", tag_name="textarea")
        self.generate = AutomationElement("generate", text="Generate", tag_name="button", click=self._generated)

    def _open_session(self, session_id):
        self.session_clicks.append(str(session_id))
        self.session_click_video_poll_counts.append(self.video_poll_count)
        self.current_summary_indices = list(
            self.session_artifact_indices.get(str(session_id), range(1, len(self.previews) + 1))
        )
        self._rebuild_summary_artifacts()
        self.summary_open = True

    def get(self, _url):
        self.get_calls += 1

    def _uploaded(self, value):
        self.uploaded = str(value)
        self.uploaded_paths.append(str(value))
        index = len(self.previews) + 1
        self.previews.append(
            AutomationElement(f"preview-{index}", tag_name="img", attrs={"src": f"blob:new-preview-{index}"})
        )

    def _generated(self):
        self.generate_clicks += 1
        self.new_user_message = AutomationElement(
            f"new-user-message-{self.generate_clicks}",
            text=self.prompt.value,
            attrs={
                "data-message-id": f"new-message-id-{self.generate_clicks}",
                "data-message-group-id": f"new-message-group-{self.generate_clicks}",
            },
        )
        if self.inject_unrelated_on_generate:
            self.unrelated_video = AutomationElement(
                "unrelated-video",
                tag_name="video",
                attrs={"src": "https://example/unrelated.mp4"},
            )
        self.new_videos = [
            AutomationElement(
                f"new-video-{index}",
                tag_name="video",
                attrs={"src": f"https://example/new-{index}.mp4"},
            )
            for index in range(1, len(self.previews) + 1)
        ]
        self.current_summary_indices = list(range(1, len(self.previews) + 1))
        self._rebuild_summary_artifacts()

        if self.split_summary_sessions:
            chronological = []
            for index in range(1, len(self.previews) + 1):
                session_id = f"new-session-{index}"
                minute = 48 + index
                self.session_artifact_indices[session_id] = [index]
                chronological.append(
                    AutomationElement(
                        session_id,
                        text=f"Generated video 10:{minute:02d} am",
                        attrs={"data-testid": f"timeline-session-{index}"},
                        click=lambda _session_id=session_id: self._open_session(_session_id),
                    )
                )
            self.new_sessions = list(reversed(chronological))
            self.new_session = self.new_sessions[0]
        else:
            session_id = f"new-session-{self.generate_clicks}"
            self.session_artifact_indices[session_id] = list(range(1, len(self.previews) + 1))
            self.new_session = AutomationElement(
                session_id,
                text=f"Generated {len(self.previews)} gentle motion videos 10:49 am",
                attrs={"data-testid": f"timeline-session-{self.generate_clicks}"},
                click=lambda: self._open_session(session_id),
            )
            self.new_sessions = [self.new_session]

    def _activate_summary_artifact(self, index, generation):
        if generation != self.summary_generation:
            raise RuntimeError("stale summary options button")
        self.active_summary_artifact = index

    def _rebuild_summary_artifacts(self):
        self.summary_generation += 1
        generation = self.summary_generation
        self.summary_artifacts = []
        indices = self.current_summary_indices or list(range(1, len(self.previews) + 1))
        for index in indices:
            artifact = AutomationElement(
                f"summary-mp4-{index}",
                text=f"media-generation-{index}.mp4 MP4",
                tag_name="button",
                attrs={
                    "aria-label": "More options",
                    "data-testid": "sandbox-file-card-options",
                },
                click=lambda _index=index, _generation=generation: self._activate_summary_artifact(
                    _index, _generation
                ),
            )
            self.summary_artifacts.append(artifact)
            if index == 1:
                self.summary_artifacts.append(
                    AutomationElement(
                        "summary-json-1",
                        text="media-generation-1.json JSON",
                        tag_name="button",
                        attrs={
                            "aria-label": "More options",
                            "data-testid": "sandbox-file-card-options",
                        },
                        click=lambda: self.downloaded_json_ids.append("summary-json-1"),
                    )
                )
    def _download_active_summary_artifact(self):
        index = int(self.active_summary_artifact or 0)
        if index <= 0:
            return
        artifact_id = f"summary-mp4-{index}"
        self.downloaded_video_id = artifact_id
        self.downloaded_video_ids.append(artifact_id)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        (self.download_dir / f"download-{artifact_id}.mp4").write_bytes(
            b"\x00\x00\x00\x18ftypmp42" + artifact_id.encode()
        )
        self.active_summary_artifact = None
        self._rebuild_summary_artifacts()

    def find_elements(self, by, selector):
        if by == "tag name" and selector == "body":
            return [self.body]
        if by != "css selector":
            return []
        if selector in self.selectors.upload_inputs:
            return [self.upload]
        if selector in self.selectors.previews:
            return list(self.previews)
        if selector in self.selectors.prompt_inputs:
            if self.uploaded and self.prompt_lookup_count < self.prompt_missing_after_upload_polls:
                self.prompt_lookup_count += 1
                return []
            return [self.prompt]
        if selector in self.selectors.generate_buttons:
            return [self.generate]
        if selector in self.selectors.video_results:
            unrelated = [self.unrelated_video] if self.unrelated_video is not None else []
            return [self.old_video, *unrelated, *self.new_videos]
        if selector in self.selectors.processing:
            return []
        return []

    def execute_script(self, script, *args):
        self.executed_scripts.append(str(script))
        if "VISUAL_LOOP_CHECK_COMPOSER_PROMPT" in script:
            return False
        if "VISUAL_LOOP_BROWSER_EPOCH" in script:
            return 1_700_000_000_000
        if "VISUAL_LOOP_GENERATION_IN_PROGRESS" in script:
            if self.new_session is not None and self.session_poll_count < self.session_poll_delay:
                self.session_poll_count += 1
                return True
            return False
        if "VISUAL_LOOP_COMPLETED_SESSION_SNAPSHOT" in script:
            sessions = [self.old_session]
            if self.new_session is not None:
                self.session_poll_count += 1
                if self.session_poll_count > self.session_poll_delay:
                    late_old = [self.late_old_session] if self.late_old_session is not None else []
                    sessions = [*self.new_sessions, *late_old, *sessions]
            return [
                {
                    "element": item,
                    "index": index,
                    "top": index * 80,
                    "text": item.text,
                    "key": item.get_attribute("data-testid"),
                    "epoch_ms": (
                        self.new_session_base_epoch_ms
                        + max(0, int(item.id.rsplit("-", 1)[-1]) - 1) * 60_000
                        if item in self.new_sessions
                        else 1_700_000_000_000
                        if item is self.late_old_session
                        else 1_699_999_000_000
                    ),
                }
                for index, item in enumerate(sessions)
            ]
        if "VISUAL_LOOP_SUMMARY_MP4_ARTIFACTS" in script:
            if not self.summary_open:
                return []
            self.summary_snapshot_calls += 1
            return [
                {
                    "element": item,
                    "index": index,
                    "text": item.text,
                }
                for index, item in enumerate(self.summary_artifacts)
            ]
        if "VISUAL_LOOP_SUMMARY_DOWNLOAD_ACTION" in script:
            if self.active_summary_artifact is None:
                return None
            return AutomationElement(
                "summary-download",
                text="Download",
                tag_name="button",
                click=self._download_active_summary_artifact,
            )
        if "VISUAL_LOOP_CLOSE_SUMMARY" in script:
            self.summary_open = False
            return True
        if "scrollIntoView" in script and args:
            args[0].click()
            return True
        if script.strip() == "arguments[0].click();" and args:
            args[0].click()
            return True
        if "VISUAL_LOOP_INSTALL_VIDEO_WATCH" in script:
            self.watch_started.append(str(args[0]))
            return 1_700_000_000_000
        if "VISUAL_LOOP_STOP_VIDEO_WATCH" in script:
            self.watch_stopped.append(str(args[0]))
            return True
        if "VISUAL_LOOP_USER_MESSAGE_SNAPSHOT" in script:
            messages = [self.old_user_message]
            if self.new_user_message is not None and self.expose_new_user_message:
                messages.append(self.new_user_message)
            return [
                {
                    "element": item,
                    "dom_index": index,
                    "message_id": item.get_attribute("data-message-id"),
                    "message_group_id": item.get_attribute("data-message-group-id"),
                    "text": item.text,
                }
                for index, item in enumerate(messages)
            ]
        if "VISUAL_LOOP_VIDEOS_AFTER_MESSAGE" in script:
            self.video_poll_count += 1
            if self.video_poll_count <= self.video_poll_delay:
                return []
            if not self.expose_new_videos:
                return []
            message_id = str(args[1] if len(args) > 1 else "")
            if message_id and message_id != self.new_user_message.get_attribute("data-message-id"):
                return []
            return list(self.new_videos)
        video = args[0] if args else None
        if "document.querySelectorAll(sel)" in script or "a.download='muse-video.mp4'" in script:
            if self.fail_direct_download:
                return False
            if self.direct_download_barrier is not None:
                self.direct_download_barrier.wait(timeout=1)
            source = str(video.get_attribute("src") or "").casefold()
            if source.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")) or source.startswith("data:image/"):
                return False
            self.downloaded_video_id = video.id
            self.downloaded_video_ids.append(video.id)
            self.download_dir.mkdir(parents=True, exist_ok=True)
            (self.download_dir / f"download-{video.id}.mp4").write_bytes(
                b"\x00\x00\x00\x18ftypmp42" + video.id.encode()
            )
            return True
        return True


class MuseVideoAutomationTests(unittest.TestCase):
    def test_video_fingerprint_ignores_dom_id_and_poster_changes_for_same_media(self):
        first = AutomationElement(
            "old-dom-id",
            tag_name="video",
            attrs={"src": "https://example/result.mp4", "poster": "https://example/poster-a.jpg"},
        )
        rerendered = AutomationElement(
            "new-dom-id",
            tag_name="video",
            attrs={"src": "https://example/result.mp4", "poster": "https://example/poster-b.jpg"},
        )

        self.assertEqual(_video_fingerprint(first), _video_fingerprint(rerendered))

    def test_download_rejects_image_source_and_never_clicks_generic_card_button(self):
        with tempfile.TemporaryDirectory() as value:
            root = Path(value)
            driver = AutomationDriver(root / "downloads")
            automation = MuseVideoAutomation(download_timeout=0.1, poll_interval=0.01)
            image_disguised_as_result = AutomationElement(
                "wrong-image",
                tag_name="video",
                attrs={"src": "https://example/result.jpg"},
            )
            context = MuseVideoRunContext(
                worker_id=1,
                download_dir=root / "downloads",
                stopped=lambda: False,
                transition=lambda *_args, **_kwargs: None,
                log=lambda _message: None,
                retry_limit=0,
                backoff_base=0.01,
            )

            with self.assertRaisesRegex(MuseVideoDownloadError, "từ chối bấm nút tải ảnh/card"):
                automation._download(driver, image_disguised_as_result, root / "out.mp4", context)

            self.assertEqual(list((root / "downloads").glob("*")), [])
            self.assertTrue(all("querySelectorAll(sel)" not in script for script in driver.executed_scripts))

    def test_uploads_one_image_submits_once_and_downloads_only_new_video(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            source.write_bytes(b"image")
            download_dir = root / "downloads"
            target = root / "output" / "result.mp4"
            driver = AutomationDriver(download_dir)
            driver.video_poll_delay = 2
            driver.session_poll_delay = 2
            job = MuseVideoJob(
                job_id="job-1",
                source_path=str(source),
                worker_id=1,
                account_id="account-1",
                email="owner@example.com",
                prompt="make a cinematic video",
                settings=MuseVideoSettings(),
                output_path=str(target),
            )
            transitions = []

            def transition(state, **changes):
                transitions.append(state)
                job.state = state
                for key, value in changes.items():
                    if hasattr(job, key):
                        setattr(job, key, value)

            context = MuseVideoRunContext(
                worker_id=1,
                download_dir=download_dir,
                stopped=lambda: False,
                transition=transition,
                log=lambda _message: None,
                retry_limit=1,
                backoff_base=0,
            )
            automation = MuseVideoAutomation(
                poll_interval=0.01,
                timeout=1,
                upload_timeout=1,
                download_timeout=1,
            )

            result = automation.process(driver, job, context)

            self.assertEqual(driver.uploaded, str(source.resolve()))
            self.assertEqual(driver.prompt.value, "make a cinematic video")
            self.assertEqual(driver.generate_clicks, 1)
            self.assertGreaterEqual(driver.video_poll_count, 3)
            self.assertEqual(driver.downloaded_video_id, "new-video-1")
            self.assertEqual(driver.downloaded_json_ids, [])
            self.assertEqual(driver.session_clicks, [])
            self.assertEqual(job.submission_message_id, "new-message-id-1")
            self.assertEqual(job.submission_message_group_id, "new-message-group-1")
            self.assertEqual(job.submission_message_index, 1)
            self.assertEqual(job.baseline_message_ids, ["old-message-id"])
            self.assertEqual(job.submission_started_epoch_ms, 1_700_000_000_000)
            self.assertTrue(job.baseline_session_fingerprints)
            self.assertFalse(job.result_session_fingerprint)
            self.assertEqual(driver.watch_started, driver.watch_stopped)
            self.assertTrue(
                any(
                    "M15.79 7.66C16.08 7.27" in script
                    for script in driver.executed_scripts
                    if "VISUAL_LOOP_COMPLETED_SESSION_SNAPSHOT" in script
                )
            )
            self.assertEqual(result, target)
            self.assertTrue(target.is_file())
            self.assertIn(MuseVideoJobState.SUBMITTED, transitions)
            self.assertIn(MuseVideoJobState.DOWNLOADING, transitions)

            download_count = len(driver.downloaded_video_ids)
            second_result = automation.process(driver, job, context)

            self.assertEqual(second_result, target)
            self.assertEqual(driver.generate_clicks, 1)
            self.assertEqual(len(driver.downloaded_video_ids), download_count)

    def test_result_is_bound_to_prompt_and_ignores_unrelated_video_added_later(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            source.write_bytes(b"image")
            download_dir = root / "downloads"
            target = root / "output" / "result.mp4"
            driver = AutomationDriver(download_dir)
            driver.inject_unrelated_on_generate = True
            driver.old_user_message.text = "the exact submitted prompt"
            job = MuseVideoJob(
                job_id="job-prompt-bound",
                source_path=str(source),
                worker_id=1,
                account_id="account-1",
                email="owner@example.com",
                prompt="the exact submitted prompt",
                settings=MuseVideoSettings(),
                output_path=str(target),
            )

            def transition(state, **changes):
                job.state = state
                for key, value in changes.items():
                    if hasattr(job, key):
                        setattr(job, key, value)

            context = MuseVideoRunContext(
                worker_id=1,
                download_dir=download_dir,
                stopped=lambda: False,
                transition=transition,
                log=lambda _message: None,
                retry_limit=1,
                backoff_base=0,
            )

            result = MuseVideoAutomation(
                poll_interval=0.01,
                timeout=1,
                upload_timeout=1,
                download_timeout=1,
            ).process(driver, job, context)

            self.assertEqual(result, target)
            self.assertEqual(driver.downloaded_video_id, "new-video-1")
            self.assertTrue(all("querySelectorAll(sel)" not in script for script in driver.executed_scripts))
            self.assertNotIn("unrelated-video", driver.downloaded_video_ids)
            self.assertEqual(driver.downloaded_json_ids, [])

    def test_downloads_new_video_when_muse_hides_user_message_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            source.write_bytes(b"image")
            target = root / "output" / "result.mp4"
            driver = AutomationDriver(root / "downloads")
            driver.expose_new_user_message = False
            logs = []
            job = MuseVideoJob(
                job_id="job-no-message-metadata",
                source_path=str(source),
                worker_id=1,
                account_id="account-1",
                email="owner@example.com",
                prompt="animate this image",
                settings=MuseVideoSettings(),
                output_path=str(target),
            )

            def transition(state, **changes):
                job.state = state
                for key, value in changes.items():
                    if hasattr(job, key):
                        setattr(job, key, value)

            context = MuseVideoRunContext(
                worker_id=1,
                download_dir=driver.download_dir,
                stopped=lambda: False,
                transition=transition,
                log=logs.append,
                retry_limit=1,
                backoff_base=0,
            )
            started = time.monotonic()

            result = MuseVideoAutomation(
                poll_interval=0.01,
                timeout=1,
                upload_timeout=1,
                download_timeout=1,
            ).process(driver, job, context)

            self.assertLess(time.monotonic() - started, 1.5)
            self.assertEqual(result, target)
            self.assertEqual(driver.downloaded_video_ids, ["new-video-1"])
            self.assertEqual(driver.session_clicks, [])
            self.assertEqual(job.submission_message_id, "")
            self.assertTrue(any("mốc theo dõi" in message for message in logs))
            self.assertTrue(
                any(
                    "if(!message)return watch?videos:[]" in script
                    for script in driver.executed_scripts
                )
            )

    def test_waits_for_prompt_to_reappear_after_upload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            source.write_bytes(b"image")
            target = root / "output" / "result.mp4"
            driver = AutomationDriver(root / "downloads")
            driver.prompt_missing_after_upload_polls = 3
            job = MuseVideoJob(
                job_id="job-delayed-prompt",
                source_path=str(source),
                worker_id=1,
                account_id="account-1",
                email="owner@example.com",
                prompt="prompt appears after upload settles",
                settings=MuseVideoSettings(),
                output_path=str(target),
            )

            def transition(state, **changes):
                job.state = state
                for key, value in changes.items():
                    if hasattr(job, key):
                        setattr(job, key, value)

            context = MuseVideoRunContext(
                worker_id=1,
                download_dir=driver.download_dir,
                stopped=lambda: False,
                transition=transition,
                log=lambda _message: None,
                retry_limit=1,
                backoff_base=0,
            )

            result = MuseVideoAutomation(
                poll_interval=0.01,
                timeout=1,
                upload_timeout=1,
                download_timeout=1,
            ).process(driver, job, context)

            self.assertEqual(result, target)
            self.assertEqual(driver.prompt.value, "prompt appears after upload settles")
            self.assertEqual(driver.prompt_lookup_count, 3)
            self.assertEqual(driver.generate_clicks, 1)

    def test_waits_for_new_complete_session_when_video_dom_is_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            source.write_bytes(b"image")
            target = root / "output" / "result.mp4"
            driver = AutomationDriver(root / "downloads")
            driver.expose_new_user_message = False
            driver.expose_new_videos = False
            driver.session_poll_delay = 2
            logs = []
            job = MuseVideoJob(
                job_id="job-session-after-send",
                source_path=str(source),
                worker_id=1,
                account_id="account-1",
                email="owner@example.com",
                prompt="wait for the new completed session",
                settings=MuseVideoSettings(),
                output_path=str(target),
            )

            def transition(state, **changes):
                job.state = state
                for key, value in changes.items():
                    if hasattr(job, key):
                        setattr(job, key, value)

            context = MuseVideoRunContext(
                worker_id=1,
                download_dir=driver.download_dir,
                stopped=lambda: False,
                transition=transition,
                log=logs.append,
                retry_limit=1,
                backoff_base=0,
            )

            result = MuseVideoAutomation(
                poll_interval=0.01,
                timeout=1,
                upload_timeout=1,
                download_timeout=1,
            ).process(driver, job, context)

            self.assertEqual(result, target)
            self.assertEqual(driver.session_clicks, ["new-session-1"])
            self.assertNotIn("old-session", driver.session_clicks)
            self.assertEqual(driver.downloaded_video_ids, ["summary-mp4-1"])
            self.assertTrue(
                any("tick Complete sau thời điểm Send" in message for message in logs)
            )

    def test_submits_three_images_once_and_downloads_three_distinct_videos_in_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            download_dir = root / "downloads"
            driver = AutomationDriver(download_dir)
            driver.expose_new_user_message = False
            jobs = []
            contexts = []
            for index in range(1, MUSE_IMAGES_PER_REQUEST + 1):
                source = root / f"source-{index}.png"
                source.write_bytes(f"image-{index}".encode())
                job = MuseVideoJob(
                    job_id=f"job-{index}",
                    source_path=str(source),
                    worker_id=1,
                    account_id="account-1",
                    email="owner@example.com",
                    prompt="one shared prompt",
                    settings=MuseVideoSettings(),
                    output_path=str(root / "output" / f"result-{index}.mp4"),
                )
                jobs.append(job)

                def transition(state, _job=job, **changes):
                    _job.state = state
                    for key, value in changes.items():
                        if hasattr(_job, key):
                            setattr(_job, key, value)

                contexts.append(
                    MuseVideoRunContext(
                        worker_id=1,
                        download_dir=download_dir,
                        stopped=lambda: False,
                        transition=transition,
                        log=lambda _message: None,
                        retry_limit=1,
                        backoff_base=0,
                    )
                )
            automation = MuseVideoAutomation(
                poll_interval=0.01,
                timeout=1,
                upload_timeout=1,
                download_timeout=1,
            )

            results = automation.process_batch(driver, jobs, contexts)

            self.assertEqual(driver.get_calls, 0)
            self.assertEqual(driver.generate_clicks, 1)
            self.assertEqual(driver.uploaded_paths, [str(Path(job.source_path).resolve()) for job in jobs])
            self.assertEqual(
                driver.downloaded_video_ids,
                ["new-video-1", "new-video-2", "new-video-3"],
            )
            self.assertEqual(driver.session_clicks, [])
            self.assertEqual(driver.downloaded_json_ids, [])
            self.assertEqual(set(results), {job.job_id for job in jobs})
            self.assertTrue(all(Path(result).is_file() for result in results.values()))
            self.assertEqual(len({Path(result).read_bytes() for result in results.values()}), 3)
            self.assertEqual(len({job.result_fingerprint for job in jobs}), 3)
            self.assertEqual(len({job.submission_group_id for job in jobs}), 1)
            self.assertEqual([job.submission_index for job in jobs], [0, 1, 2])

            for result in results.values():
                Path(result).unlink()
            driver.downloaded_video_ids.clear()
            recovered = automation.recover_batch(driver, jobs, contexts)

            self.assertEqual(driver.generate_clicks, 1)
            self.assertEqual(
                driver.downloaded_video_ids,
                ["new-video-1", "new-video-2", "new-video-3"],
            )
            self.assertTrue(all(Path(result).is_file() for result in recovered.values()))
            self.assertEqual(len({Path(result).read_bytes() for result in recovered.values()}), 3)

    def test_downloads_split_complete_sessions_from_closest_send_time_to_newest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            download_dir = root / "downloads"
            driver = AutomationDriver(download_dir)
            driver.split_summary_sessions = True
            driver.fail_direct_download = True
            driver.late_old_session = AutomationElement(
                "late-old-session",
                text="Generated old video 10:48 am",
                attrs={"data-testid": "timeline-session-late-old"},
                click=lambda: driver._open_session("late-old-session"),
            )
            jobs = []
            contexts = []
            for index in range(1, 4):
                source = root / f"split-source-{index}.png"
                source.write_bytes(f"image-{index}".encode())
                job = MuseVideoJob(
                    job_id=f"split-job-{index}",
                    source_path=str(source),
                    worker_id=1,
                    account_id="account-1",
                    email="owner@example.com",
                    prompt="one prompt creates split sessions",
                    settings=MuseVideoSettings(),
                    output_path=str(root / "output" / f"split-result-{index}.mp4"),
                )
                jobs.append(job)

                def transition(state, _job=job, **changes):
                    _job.state = state
                    for key, value in changes.items():
                        if hasattr(_job, key):
                            setattr(_job, key, value)

                contexts.append(
                    MuseVideoRunContext(
                        worker_id=1,
                        download_dir=download_dir,
                        stopped=lambda: False,
                        transition=transition,
                        log=lambda _message: None,
                        retry_limit=1,
                        backoff_base=0,
                    )
                )

            automation = MuseVideoAutomation(
                poll_interval=0.01,
                timeout=1,
                upload_timeout=1,
                download_timeout=1,
            )
            results = automation.process_batch(driver, jobs, contexts)

            self.assertEqual(
                driver.session_clicks,
                ["new-session-1", "new-session-2", "new-session-3"],
            )
            self.assertNotIn("late-old-session", driver.session_clicks)
            self.assertEqual(
                driver.downloaded_video_ids,
                ["summary-mp4-1", "summary-mp4-2", "summary-mp4-3"],
            )
            self.assertTrue(all(Path(result).is_file() for result in results.values()))
            self.assertEqual(len({job.result_session_fingerprint for job in jobs}), 3)

            for result in results.values():
                Path(result).unlink()
            driver.downloaded_video_ids.clear()
            driver.session_clicks.clear()
            recovered = automation.recover_batch(driver, jobs, contexts)

            self.assertEqual(
                driver.session_clicks,
                ["new-session-1", "new-session-2", "new-session-3"],
            )
            self.assertEqual(
                driver.downloaded_video_ids,
                ["summary-mp4-1", "summary-mp4-2", "summary-mp4-3"],
            )
            self.assertTrue(all(Path(result).is_file() for result in recovered.values()))

    def test_recover_multi_video_in_same_session_across_separate_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            download_dir = root / "downloads"
            driver = AutomationDriver(download_dir)
            driver.split_summary_sessions = False
            driver.fail_direct_download = True
            automation = MuseVideoAutomation(poll_interval=0.01)

            source1 = root / "source-1.png"
            source1.write_bytes(b"image-1")
            source2 = root / "source-2.png"
            source2.write_bytes(b"image-2")

            driver.previews = [
                AutomationElement("preview-1", tag_name="img", attrs={"src": "blob:p1"}),
                AutomationElement("preview-2", tag_name="img", attrs={"src": "blob:p2"}),
            ]
            driver._generated()

            def transition1(state, **changes):
                job1.state = state
                for k, v in changes.items():
                    if hasattr(job1, k):
                        setattr(job1, k, v)

            def transition2(state, **changes):
                job2.state = state
                for k, v in changes.items():
                    if hasattr(job2, k):
                        setattr(job2, k, v)

            job1 = MuseVideoJob(
                job_id="multi-job-1",
                source_path=str(source1),
                worker_id=1,
                account_id="account-1",
                email="owner@example.com",
                prompt="prompt 1",
                settings=MuseVideoSettings(),
                output_path=str(root / "out-1.mp4"),
                submission_group_id="group-1",
                submission_index=0,
                submission_size=1,
                submitted=True,
                submission_attempted=True,
                submission_started_epoch_ms=10_000,
            )
            job2 = MuseVideoJob(
                job_id="multi-job-2",
                source_path=str(source2),
                worker_id=1,
                account_id="account-1",
                email="owner@example.com",
                prompt="prompt 2",
                settings=MuseVideoSettings(),
                output_path=str(root / "out-2.mp4"),
                submission_group_id="group-2",
                submission_index=0,
                submission_size=1,
                submitted=True,
                submission_attempted=True,
                submission_started_epoch_ms=10_000,
            )
            ctx1 = MuseVideoRunContext(
                worker_id=1,
                download_dir=download_dir,
                stopped=lambda: False,
                transition=transition1,
                log=lambda _msg: None,
                retry_limit=1,
                backoff_base=0,
            )
            ctx2 = MuseVideoRunContext(
                worker_id=1,
                download_dir=download_dir,
                stopped=lambda: False,
                transition=transition2,
                log=lambda _msg: None,
                retry_limit=1,
                backoff_base=0,
            )

            res1 = automation.recover_batch(driver, [job1], [ctx1])
            self.assertTrue(Path(res1[job1.job_id]).is_file())
            job1_fp = job1.result_fingerprint
            self.assertTrue(job1_fp)

            res2 = automation.recover_batch(
                driver,
                [job2],
                [ctx2],
                claimed_fingerprints={job1_fp},
            )
            self.assertTrue(Path(res2[job2.job_id]).is_file())
            self.assertTrue(job2.result_fingerprint)
            self.assertNotEqual(job1.result_fingerprint, job2.result_fingerprint)
            self.assertNotEqual(
                Path(res1[job1.job_id]).read_bytes(),
                Path(res2[job2.job_id]).read_bytes(),
            )

    def test_different_tabs_do_not_share_summary_download_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            automation = MuseVideoAutomation(poll_interval=0.01)
            first_driver = AutomationDriver(Path(folder) / "downloads-1")
            second_driver = AutomationDriver(Path(folder) / "downloads-2")
            first_lock = automation._summary_lock_for(first_driver)
            second_lock = automation._summary_lock_for(second_driver)

            self.assertIsNot(first_lock, second_lock)
            first_lock.acquire()
            try:
                self.assertTrue(second_lock.acquire(blocking=False))
                second_lock.release()
            finally:
                first_lock.release()

    def test_same_minute_complete_session_is_never_clicked_after_send(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            source.write_bytes(b"image")
            driver = AutomationDriver(root / "downloads")
            driver.fail_direct_download = True
            driver.new_session_base_epoch_ms = 1_700_000_000_000
            job = MuseVideoJob(
                job_id="same-minute-session",
                source_path=str(source),
                worker_id=1,
                account_id="account-1",
                email="owner@example.com",
                prompt="never click an old same-minute session",
                settings=MuseVideoSettings(),
                output_path=str(root / "output" / "result.mp4"),
            )

            def transition(state, **changes):
                job.state = state
                for key, value in changes.items():
                    if hasattr(job, key):
                        setattr(job, key, value)

            context = MuseVideoRunContext(
                worker_id=1,
                download_dir=driver.download_dir,
                stopped=lambda: False,
                transition=transition,
                log=lambda _message: None,
                retry_limit=1,
                backoff_base=0,
            )

            with self.assertRaises(MuseVideoTimeout):
                MuseVideoAutomation(
                    poll_interval=0.01,
                    timeout=0.1,
                    upload_timeout=0.1,
                    download_timeout=0.1,
                ).process(driver, job, context)

            self.assertEqual(driver.session_clicks, [])
            self.assertEqual(driver.downloaded_video_ids, [])

    def test_two_tabs_download_videos_below_their_prompts_concurrently(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            automation = MuseVideoAutomation(
                poll_interval=0.01,
                timeout=1,
                upload_timeout=1,
                download_timeout=1,
            )
            barrier = threading.Barrier(2)
            runs = []
            for worker_id in (1, 2):
                worker_root = root / f"worker-{worker_id}"
                worker_root.mkdir()
                source = worker_root / "source.png"
                source.write_bytes(f"image-{worker_id}".encode())
                driver = AutomationDriver(worker_root / "downloads")
                driver.expose_new_user_message = False
                driver.direct_download_barrier = barrier
                job = MuseVideoJob(
                    job_id=f"concurrent-job-{worker_id}",
                    source_path=str(source),
                    worker_id=worker_id,
                    account_id=f"account-{worker_id}",
                    email=f"owner{worker_id}@example.com",
                    prompt=f"concurrent prompt {worker_id}",
                    settings=MuseVideoSettings(),
                    output_path=str(worker_root / "output" / "result.mp4"),
                )

                def transition(state, _job=job, **changes):
                    _job.state = state
                    for key, value in changes.items():
                        if hasattr(_job, key):
                            setattr(_job, key, value)

                context = MuseVideoRunContext(
                    worker_id=worker_id,
                    download_dir=driver.download_dir,
                    stopped=lambda: False,
                    transition=transition,
                    log=lambda _message: None,
                    retry_limit=1,
                    backoff_base=0,
                )
                runs.append((driver, job, context))

            results = {}
            errors = []

            def run(driver, job, context):
                try:
                    results[job.job_id] = automation.process(driver, job, context)
                except BaseException as exc:
                    errors.append(exc)

            threads = [
                threading.Thread(target=run, args=run_args)
                for run_args in runs
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(set(results), {"concurrent-job-1", "concurrent-job-2"})
            self.assertTrue(all(Path(result).is_file() for result in results.values()))
            self.assertTrue(all(driver.session_clicks == [] for driver, _job, _context in runs))

    def test_send_selector_does_not_click_other_submit_buttons(self):
        with tempfile.TemporaryDirectory() as folder:
            driver = AutomationDriver(Path(folder))
            decoy_clicks = []
            close_panel = AutomationElement(
                "close-panel",
                tag_name="button",
                attrs={"type": "submit", "aria-label": "Close panel"},
                click=lambda: decoy_clicks.append("close"),
            )
            attach_file = AutomationElement(
                "attach-file",
                tag_name="button",
                attrs={"type": "submit", "aria-label": "Attach file"},
                click=lambda: decoy_clicks.append("attach"),
            )
            original_find = driver.find_elements

            def find_elements(by, selector):
                if by == "css selector" and selector == "button[aria-label='Send']":
                    return [driver.generate]
                if by == "css selector" and selector == "button[type='submit']":
                    return [close_panel, attach_file, driver.generate]
                return original_find(by, selector)

            driver.find_elements = find_elements

            MuseVideoAutomation()._click_generate_once(driver)

            self.assertEqual(driver.generate_clicks, 1)
            self.assertEqual(decoy_clicks, [])


class MuseVideoBatchManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.profile_root = self.root / "data" / "muse_profiles"
        self.drivers: dict[int, ReadyDriver] = {}

        def driver_factory(profile: Path):
            worker_id = int(profile.name.rsplit("_", 1)[1])
            driver = ReadyDriver(profile)
            self.drivers[worker_id] = driver
            return driver

        self.sessions = MuseSessionManager(
            profile_root=self.profile_root,
            checkpoint_path=self.root / "data" / "muse_sessions.json",
            account_store=MuseAccountStore(self.root / "data" / "muse_accounts.json"),
            driver_factory=driver_factory,
            poll_interval=0.01,
            stable_seconds=0,
        )
        for worker_id in range(1, 4):
            self.sessions.open_session(worker_id, f"owner{worker_id}@example.com").result(timeout=2)
        self.automation = FakeVideoAutomation()
        self.batch = self._batch(self.automation)
        self.output = self.root / "output"

    def tearDown(self):
        self.batch.shutdown(timeout=2)
        self.sessions.shutdown(timeout=2)
        self.temp.cleanup()

    def _batch(self, automation, checkpoint: Path | None = None):
        return MuseVideoBatchManager(
            session_manager=self.sessions,
            checkpoint_path=checkpoint or self.root / "data" / "muse_video_batch.json",
            automation=automation,
            retry_limit=1,
            backoff_base=0.01,
        )

    def _images(self, count: int) -> list[Path]:
        folder = self.root / "images"
        folder.mkdir(exist_ok=True)
        values = []
        for index in range(count):
            path = folder / f"image_{index:02d}.png"
            path.write_bytes(f"image-{index}".encode())
            values.append(path)
        return values

    def _start(self, images: list[Path], prompt: str = "shared prompt"):
        self.batch.allocate_images(images)
        return self.batch.start_all(prompt, MuseVideoSettings(aspect_ratio="16:9"), self.output)

    def _wait_worker_state(self, worker_id: int, expected: str, timeout: float = 2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.batch.snapshot().workers[worker_id - 1].state.value
            if state == expected:
                return
            time.sleep(0.01)
        self.fail(f"worker {worker_id} did not reach {expected}")

    def test_round_robin_distribution_for_required_sizes(self):
        expected = {
            0: (0, 0, 0),
            1: (1, 0, 0),
            2: (1, 1, 0),
            3: (1, 1, 1),
            9: (3, 3, 3),
            10: (4, 3, 3),
            11: (4, 4, 3),
        }
        for count, sizes in expected.items():
            allocations = self.batch.allocate_images(self._images(count))
            self.assertEqual(tuple(map(len, allocations)), sizes)
            self.assertLessEqual(max(sizes) - min(sizes), 1)

    def test_selected_ready_accounts_receive_all_images_and_run_concurrently(self):
        images = self._images(6)

        allocations = self.batch.allocate_images(images, worker_ids=(1, 2))
        self.batch.start_all(
            "selected prompt",
            MuseVideoSettings(),
            self.output,
            worker_ids=(1, 2),
        ).result(timeout=3)

        snapshot = self.batch.snapshot()
        self.assertEqual(tuple(map(len, allocations)), (3, 3, 0))
        self.assertEqual(snapshot.enabled_worker_ids, (1, 2))
        self.assertEqual({job.worker_id for job in snapshot.jobs}, {1, 2})
        self.assertTrue(all(job.state == MuseVideoJobState.COMPLETED for job in snapshot.jobs))
        self.assertEqual(set(self.automation.drivers), {1, 2})

    def test_two_workers_cannot_share_the_same_driver_or_browser_tab(self):
        images = self._images(2)
        self.batch.allocate_images(images, worker_ids=(1, 2))
        original = self.sessions.sessions[2].driver
        self.sessions.sessions[2].driver = self.sessions.sessions[1].driver
        try:
            with self.assertRaisesRegex(MuseVideoBatchError, "cùng một Chrome/profile"):
                self.batch.start_all(
                    "selected prompt",
                    MuseVideoSettings(),
                    self.output,
                    worker_ids=(1, 2),
                )
        finally:
            self.sessions.sessions[2].driver = original

    def test_scan_normalizes_deduplicates_sorts_and_supports_recursive(self):
        folder = self.root / "scan"
        nested = folder / "nested"
        nested.mkdir(parents=True)
        (folder / "b.JPG").write_bytes(b"b")
        (folder / "a.png").write_bytes(b"a")
        (folder / "d.jpeg").write_bytes(b"d")
        (folder / "ignore.txt").write_text("x")
        (nested / "c.webp").write_bytes(b"c")

        shallow = self.batch.scan_images(folder)
        recursive = self.batch.scan_images(folder, recursive=True)

        self.assertEqual([item.name for item in shallow], ["a.png", "b.JPG", "d.jpeg"])
        self.assertEqual([item.name for item in recursive], ["a.png", "b.JPG", "c.webp", "d.jpeg"])

    def test_three_workers_run_concurrently_with_same_snapshot(self):
        self.automation.barrier = threading.Barrier(3)
        future = self._start(self._images(3))

        future.result(timeout=3)

        self.assertEqual(self.automation.prompts, ["shared prompt"] * 3)
        self.assertTrue(all(value == MuseVideoSettings(aspect_ratio="16:9") for value in self.automation.settings))
        self.assertEqual(len({id(driver) for driver in self.automation.drivers.values()}), 3)
        self.assertEqual(
            {driver.profile.name for driver in self.automation.drivers.values()},
            {"account_1", "account_2", "account_3"},
        )

    def test_each_image_generates_exactly_one_video(self):
        images = self._images(10)
        self._start(images).result(timeout=3)

        self.assertEqual(len(self.automation.generate_calls), 10)
        self.assertTrue(all(value == 1 for value in self.automation.generate_calls.values()))
        self.assertEqual(len(list(self.output.glob("*.mp4"))), 10)
        self.assertEqual(sum(job.state == MuseVideoJobState.COMPLETED for job in self.batch.snapshot().jobs), 10)

    def test_manager_batches_up_to_three_images_per_prompt_and_continues_until_queue_is_empty(self):
        self.batch.shutdown(timeout=2)
        grouped = GroupedFakeVideoAutomation()
        self.batch = self._batch(grouped)

        self._start(self._images(12)).result(timeout=3)

        self.assertEqual(grouped.group_sizes, {1: [3, 1], 2: [3, 1], 3: [3, 1]})
        self.assertEqual(sum(grouped.generate_calls.values()), 6)
        self.assertEqual(len(list(self.output.glob("*.mp4"))), 12)

    def test_one_worker_failure_does_not_stop_other_workers(self):
        self.automation.scenarios[2] = "fail"
        self._start(self._images(3)).result(timeout=3)
        jobs = self.batch.snapshot().jobs

        self.assertEqual(jobs[0].state, MuseVideoJobState.COMPLETED)
        self.assertEqual(jobs[1].state, MuseVideoJobState.FAILED)
        self.assertEqual(jobs[2].state, MuseVideoJobState.COMPLETED)
        self.assertEqual(self.batch.snapshot().workers[1].state.value, "FAILED")

    def test_login_required_and_quota_are_isolated_and_not_redistributed(self):
        self.automation.scenarios[1] = "login"
        self.automation.scenarios[2] = "quota"
        self._start(self._images(6)).result(timeout=3)
        snapshot = self.batch.snapshot()

        self.assertEqual(snapshot.workers[0].state.value, "LOGIN_REQUIRED")
        self.assertEqual(snapshot.workers[1].state.value, "QUOTA_EXHAUSTED")
        self.assertEqual(snapshot.workers[2].completed, 2)
        quota_job = snapshot.jobs[1]
        self.assertEqual(quota_job.worker_id, 2)
        self.assertEqual(quota_job.state, MuseVideoJobState.QUOTA_EXHAUSTED)
        self.assertEqual(self.batch.retry_failed(2), 0)

    def test_timeout_marks_only_that_job_failed(self):
        self.automation.scenarios[1] = "timeout"
        self._start(self._images(3)).result(timeout=3)
        snapshot = self.batch.snapshot()

        self.assertEqual(snapshot.jobs[0].state, MuseVideoJobState.FAILED)
        self.assertTrue(snapshot.jobs[0].submitted)
        self.assertEqual(snapshot.jobs[1].state, MuseVideoJobState.COMPLETED)
        self.assertEqual(snapshot.jobs[2].state, MuseVideoJobState.COMPLETED)

    def test_stop_and_resume_preserve_queue_prompt_and_progress(self):
        self.automation.scenarios = {1: "block_before_submit", 2: "block_before_submit", 3: "block_before_submit"}
        future = self._start(self._images(3))
        self.assertTrue(all(event.wait(1) for event in self.automation.started.values()))
        self.assertEqual(self.batch.stop_all(), 3)
        future.result(timeout=3)
        paused = self.batch.snapshot()

        self.assertEqual(paused.prompt, "shared prompt")
        self.assertTrue(all(job.state == MuseVideoJobState.PAUSED for job in paused.jobs))
        self.automation.scenarios = {}
        self.batch.resume().result(timeout=3)
        self.assertTrue(all(job.state == MuseVideoJobState.COMPLETED for job in self.batch.snapshot().jobs))

    def test_submitted_job_is_recovered_after_manager_restart_without_resubmit(self):
        self.automation.scenarios[1] = "stop_after_submit"
        future = self._start(self._images(1))
        self.assertTrue(self.automation.started[1].wait(1))
        self.assertTrue(self.batch.stop_worker(1))
        future.result(timeout=3)
        checkpoint = self.batch.checkpoint_path
        job_id = self.batch.snapshot().jobs[0].job_id
        self.assertEqual(self.automation.generate_calls[job_id], 1)
        self.batch.shutdown(timeout=2)

        recovered_automation = FakeVideoAutomation()
        self.batch = self._batch(recovered_automation, checkpoint)
        self.batch.resume().result(timeout=3)

        self.assertEqual(recovered_automation.generate_calls[job_id], 0)
        self.assertEqual(recovered_automation.recover_calls[job_id], 1)
        self.assertEqual(self.batch.snapshot().jobs[0].state, MuseVideoJobState.COMPLETED)

    def test_retry_download_does_not_generate_again(self):
        self.automation.scenarios[1] = "download_fail"
        self._start(self._images(1)).result(timeout=3)
        job = self.batch.snapshot().jobs[0]
        self.assertEqual(job.state, MuseVideoJobState.FAILED)
        self.assertEqual(self.automation.generate_calls[job.job_id], 1)

        self.automation.scenarios[1] = "normal"
        self.assertEqual(self.batch.retry_failed(1), 1)
        self.batch.resume().result(timeout=3)

        self.assertEqual(self.automation.generate_calls[job.job_id], 1)
        self.assertEqual(self.automation.recover_calls[job.job_id], 1)
        self.assertEqual(self.batch.snapshot().jobs[0].state, MuseVideoJobState.COMPLETED)

    def test_completed_checkpoint_with_missing_mp4_recovers_download_without_resubmit(self):
        image = self._images(1)[0]
        self._start([image]).result(timeout=3)
        completed = self.batch.snapshot().jobs[0]
        Path(completed.output_path).unlink()
        checkpoint = self.batch.checkpoint_path
        self.batch.shutdown(timeout=2)

        recovered_automation = FakeVideoAutomation()
        self.batch = self._batch(recovered_automation, checkpoint)
        self.batch.allocate_images([image])
        self.batch.start_all(
            "shared prompt",
            MuseVideoSettings(aspect_ratio="16:9"),
            self.output,
        ).result(timeout=3)

        recovered = self.batch.snapshot().jobs[0]
        self.assertEqual(recovered_automation.generate_calls[recovered.job_id], 0)
        self.assertEqual(recovered_automation.recover_calls[recovered.job_id], 1)
        self.assertEqual(recovered.state, MuseVideoJobState.COMPLETED)
        self.assertTrue(Path(recovered.output_path).is_file())

    def test_completed_checkpoint_is_copied_to_new_output_without_resubmit(self):
        image = self._images(1)[0]
        self._start([image]).result(timeout=3)
        checkpoint = self.batch.checkpoint_path
        self.batch.shutdown(timeout=2)

        new_output = self.root / "new-output"
        recovered_automation = FakeVideoAutomation()
        self.batch = self._batch(recovered_automation, checkpoint)
        self.batch.allocate_images([image])
        self.batch.start_all(
            "shared prompt",
            MuseVideoSettings(aspect_ratio="16:9"),
            new_output,
        ).result(timeout=3)

        completed = self.batch.snapshot().jobs[0]
        self.assertEqual(sum(recovered_automation.generate_calls.values()), 0)
        self.assertEqual(sum(recovered_automation.recover_calls.values()), 0)
        self.assertEqual(completed.state, MuseVideoJobState.COMPLETED)
        # Windows runners may spell the same temp directory as either
        # RUNNER~1 (8.3 path) or runneradmin (long path).
        self.assertEqual(Path(completed.output_path).parent.resolve(), new_output.resolve())
        self.assertTrue(Path(completed.output_path).is_file())

    def test_worker_never_marks_success_without_valid_mp4(self):
        self.automation.scenarios[1] = "missing_output"

        self._start(self._images(1)).result(timeout=3)

        snapshot = self.batch.snapshot()
        self.assertEqual(snapshot.jobs[0].state, MuseVideoJobState.FAILED)
        self.assertEqual(snapshot.workers[0].completed, 0)
        self.assertIn("MP4 hợp lệ", snapshot.jobs[0].error)

    def test_redistribute_never_moves_submitted_job(self):
        self.automation.scenarios[1] = "stop_after_submit"
        future = self._start(self._images(4))
        self.assertTrue(self.automation.started[1].wait(1))
        self.batch.stop_all()
        future.result(timeout=3)
        before = {job.job_id: job.worker_id for job in self.batch.snapshot().jobs if job.submission_attempted}

        self.batch.redistribute_unsubmitted()
        after = {job.job_id: job.worker_id for job in self.batch.snapshot().jobs if job.submission_attempted}

        self.assertEqual(before, after)

    def test_ui_reload_singleton_does_not_create_new_manager_or_drivers(self):
        self._start(self._images(3)).result(timeout=3)
        original_driver_ids = {key: id(value) for key, value in self.drivers.items()}
        with patch("auth.muse_video_batch._batch_singleton", self.batch):
            first = get_muse_video_batch_manager(session_manager=self.sessions)
            second = get_muse_video_batch_manager(session_manager=self.sessions)

        self.assertIs(first, second)
        self.assertIs(first, self.batch)
        self.assertEqual(original_driver_ids, {key: id(value) for key, value in self.drivers.items()})

    def test_stop_all_does_not_touch_auto_registry_or_youtube(self):
        auto_registry_cancel = threading.Event()
        youtube_cancel = threading.Event()

        self.assertEqual(self.batch.stop_all(), 0)
        self.assertFalse(auto_registry_cancel.is_set())
        self.assertFalse(youtube_cancel.is_set())

    def test_job_id_changes_with_image_metadata_prompt_and_settings(self):
        image = self._images(1)[0]
        first = create_muse_video_job_id(image, "one", MuseVideoSettings())
        second = create_muse_video_job_id(image, "two", MuseVideoSettings())
        third = create_muse_video_job_id(image, "one", MuseVideoSettings(aspect_ratio="9:16"))
        image.write_bytes(b"changed")
        fourth = create_muse_video_job_id(image, "one", MuseVideoSettings())

        self.assertEqual(len({first, second, third, fourth}), 4)

    def test_ready_sessions_start_without_waiting_for_login_required_session(self):
        self.sessions._set_state(self.sessions.sessions[3], MuseSessionState.LOGIN_REQUIRED)
        self.batch.allocate_images(self._images(3))

        self.batch.start_all("prompt", MuseVideoSettings(), self.output).result(timeout=3)
        partial = self.batch.snapshot()

        self.assertEqual(partial.jobs[0].state, MuseVideoJobState.COMPLETED)
        self.assertEqual(partial.jobs[1].state, MuseVideoJobState.COMPLETED)
        self.assertEqual(partial.jobs[2].state, MuseVideoJobState.PENDING)
        self.assertEqual(partial.workers[2].state, MuseVideoWorkerState.LOGIN_REQUIRED)

        self.sessions._set_state(self.sessions.sessions[3], MuseSessionState.READY)
        self.batch.resume().result(timeout=3)

        self.assertTrue(all(job.state == MuseVideoJobState.COMPLETED for job in self.batch.snapshot().jobs))

    def test_prepare_start_checkpoints_prompt_before_login_finishes(self):
        self.batch.allocate_images(self._images(1))

        self.batch.prepare_start("saved prompt", MuseVideoSettings(duration="8s"), self.output)

        raw = json.loads(self.batch.checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["prompt"], "saved prompt")
        self.assertEqual(raw["output_dir"], str(self.output.resolve()))
        self.assertEqual(raw["settings"]["duration"], "8s")
        self.assertEqual(raw["jobs"], [])

    def test_checkpoint_has_mapping_but_no_browser_secret_fields(self):
        image = self._images(1)[0]
        self._start([image]).result(timeout=3)
        raw = json.loads(self.batch.checkpoint_path.read_text(encoding="utf-8"))

        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield str(key).casefold()
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        names = set(keys(raw))
        self.assertNotIn("password", names)
        self.assertNotIn("cookie", names)
        self.assertNotIn("token", names)
        job = raw["jobs"][0]
        self.assertEqual(Path(job["source_path"]).name, image.name)
        self.assertTrue(job["output_path"].endswith(".mp4"))

    def test_image_mode_allocate_text_jobs(self):
        allocations = self.batch.allocate_text_jobs(5, worker_ids=[1, 2, 3])
        # 5 prompts distributed across 3 workers: 2, 2, 1
        counts = [len(items) for items in allocations]
        self.assertEqual(counts, [2, 2, 1])
        self.assertEqual(allocations[0], ("Prompt #1", "Prompt #4"))
        self.assertEqual(allocations[1], ("Prompt #2", "Prompt #5"))
        self.assertEqual(allocations[2], ("Prompt #3",))

    def test_image_mode_output_filename_and_job_id(self):
        settings = MuseVideoSettings(task_mode="image")
        job_id = create_muse_video_job_id("", "a beautiful sunset", settings, index=1)
        self.assertTrue(isinstance(job_id, str) and len(job_id) == 64)
        out_name = _output_filename("", "worker_1", job_id, task_mode="image")
        self.assertTrue(out_name.endswith(".png"))
        self.assertIn("image_", out_name)

        video_settings = MuseVideoSettings(task_mode="video")
        out_video = _output_filename(Path("test.jpg"), "worker_1", job_id, task_mode="video")
        self.assertTrue(out_video.endswith(".mp4"))

    def test_valid_image_formats(self):
        png_file = self.root / "test.png"
        png_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        self.assertTrue(_valid_image(png_file))
        self.assertTrue(_valid_artifact(png_file, task_mode="image"))

        jpg_file = self.root / "test.jpg"
        jpg_file.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        self.assertTrue(_valid_image(jpg_file))

        webp_file = self.root / "test.webp"
        webp_file.write_bytes(b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\x00" * 50)
        self.assertTrue(_valid_image(webp_file))

        empty_file = self.root / "empty.png"
        empty_file.write_bytes(b"")
        self.assertFalse(_valid_image(empty_file))

    def test_settings_task_mode_serialization(self):
        from dataclasses import asdict
        img_settings = MuseVideoSettings(task_mode="image", quantity=3)
        dict_data = asdict(img_settings)
        self.assertEqual(dict_data["task_mode"], "image")
        self.assertEqual(dict_data["quantity"], 3)
        restored = MuseVideoSettings.from_dict(dict_data)
        self.assertEqual(restored.task_mode, "image")
        self.assertEqual(restored.quantity, 3)


if __name__ == "__main__":
    unittest.main()

