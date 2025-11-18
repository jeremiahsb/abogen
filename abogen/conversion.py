import os
import re
import time
import hashlib  # For generating unique cache filenames
from platformdirs import user_desktop_dir
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QTimer
from PyQt6.QtWidgets import QCheckBox, QVBoxLayout, QDialog, QLabel, QDialogButtonBox
import soundfile as sf
from abogen.utils import (
    clean_text,
    create_process,
    get_user_cache_path,
    detect_encoding,
)
from abogen.constants import (
    LANGUAGE_DESCRIPTIONS,
    SAMPLE_VOICE_TEXTS,
    COLORS,
    CHAPTER_OPTIONS_COUNTDOWN,
    SUBTITLE_FORMATS,
    SUPPORTED_SOUND_FORMATS,
    SUPPORTED_SUBTITLE_FORMATS,
)
from abogen.voice_formulas import get_new_voice
import abogen.hf_tracker as hf_tracker
import static_ffmpeg
import threading  # for efficient waiting
import subprocess
import platform


def clean_subtitle_text(text):
    """Remove chapter markers and metadata tags from subtitle text."""
    # Remove metadata tags
    text = re.sub(r"<<METADATA_[^:]+:[^>]*>>", "", text)
    # Remove chapter markers
    text = re.sub(r"<<CHAPTER_MARKER:[^>]*>>", "", text)
    return text.strip()


def parse_srt_file(file_path):
    """
    Parse an SRT subtitle file and return a list of subtitle entries.

    Args:
        file_path: Path to the SRT file

    Returns:
        List of tuples: [(start_time_seconds, end_time_seconds, text), ...]
    """
    encoding = detect_encoding(file_path)
    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        content = f.read()

    # Split by double newlines to get individual subtitle blocks
    blocks = re.split(r"\n\s*\n", content.strip())

    subtitles = []
    for block in blocks:
        if not block.strip():
            continue

        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue

        # First line is index, second line is timestamp, rest is text
        try:
            timestamp_line = lines[1]
            match = re.match(
                r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})",
                timestamp_line,
            )
            if not match:
                continue

            start_str = match.group(1)
            end_str = match.group(2)
            text = "\n".join(lines[2:])

            # Convert timestamp to seconds
            def time_to_seconds(t):
                h, m, s_ms = t.split(":")
                s, ms = s_ms.split(",")
                return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

            start_sec = time_to_seconds(start_str)
            end_sec = time_to_seconds(end_str)

            # Clean text of any styling tags
            text = re.sub(r"<[^>]+>", "", text)
            # Remove chapter markers and metadata tags
            text = clean_subtitle_text(text)

            if text:  # Only add non-empty subtitles
                subtitles.append((start_sec, end_sec, text))
        except (ValueError, IndexError):
            continue

    return subtitles


def parse_vtt_file(file_path):
    """
    Parse a VTT (WebVTT) subtitle file and return a list of subtitle entries.

    Args:
        file_path: Path to the VTT file

    Returns:
        List of tuples: [(start_time_seconds, end_time_seconds, text), ...]
    """
    encoding = detect_encoding(file_path)
    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        content = f.read()

    # Remove WEBVTT header and any style/note blocks
    content = re.sub(r"^WEBVTT.*?\n", "", content, flags=re.MULTILINE)
    content = re.sub(r"STYLE\s*\n.*?(?=\n\n|$)", "", content, flags=re.DOTALL)
    content = re.sub(r"NOTE\s*\n.*?(?=\n\n|$)", "", content, flags=re.DOTALL)

    # Split by double newlines to get individual subtitle blocks
    blocks = re.split(r"\n\s*\n", content.strip())

    subtitles = []
    for block in blocks:
        if not block.strip():
            continue

        lines = block.strip().split("\n")
        if len(lines) < 2:
            continue

        # VTT can have optional identifier on first line, timestamp on second or first
        timestamp_line = None
        text_start_idx = 0

        # Check if first line is timestamp
        if "-->" in lines[0]:
            timestamp_line = lines[0]
            text_start_idx = 1
        elif len(lines) > 1 and "-->" in lines[1]:
            timestamp_line = lines[1]
            text_start_idx = 2
        else:
            continue

        try:
            # VTT format: 00:00:00.000 --> 00:00:05.000 or 00:00.000 --> 00:05.000
            match = re.match(r"([\d:.]+)\s*-->\s*([\d:.]+)", timestamp_line)
            if not match:
                continue

            start_str = match.group(1)
            end_str = match.group(2)
            text = "\n".join(lines[text_start_idx:])

            # Convert timestamp to seconds
            def time_to_seconds(t):
                parts = t.split(":")
                if len(parts) == 3:  # HH:MM:SS.mmm
                    h, m, s = parts
                    s, ms = s.split(".")
                    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0
                elif len(parts) == 2:  # MM:SS.mmm
                    m, s = parts
                    s, ms = s.split(".")
                    return int(m) * 60 + int(s) + int(ms) / 1000.0
                return 0

            start_sec = time_to_seconds(start_str)
            end_sec = time_to_seconds(end_str)

            # Clean text of any styling tags and cue settings
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"{[^}]+}", "", text)  # Remove voice tags
            # Remove chapter markers and metadata tags
            text = clean_subtitle_text(text)

            if text:  # Only add non-empty subtitles
                subtitles.append((start_sec, end_sec, text))
        except (ValueError, IndexError, AttributeError):
            continue

    return subtitles


def detect_timestamps_in_text(file_path):
    """Detect if text file contains timestamp markers (HH:MM:SS or HH:MM:SS,ms format) on separate lines."""
    try:
        encoding = detect_encoding(file_path)
        with open(file_path, "r", encoding=encoding, errors="replace") as f:
            lines = [
                line.strip() for line in f.readlines()[:50] if line.strip()
            ]  # Check first 50 non-empty lines

        # Count lines that are ONLY timestamps (no other text)
        # Supports HH:MM:SS or HH:MM:SS,ms format
        timestamp_pattern = r"^(\d{1,2}:\d{2}:\d{2}(?:,\d{1,3})?)$"
        timestamp_lines = sum(1 for line in lines if re.match(timestamp_pattern, line))

        # Must have at least 2 timestamp-only lines and they should be >5% of total lines
        return timestamp_lines >= 2 and (timestamp_lines / max(len(lines), 1)) > 0.05
    except Exception:
        return False


def parse_timestamp_text_file(file_path):
    """Parse text file with timestamps. Returns list of (start_time, end_time, text) tuples.
    Supports HH:MM:SS or HH:MM:SS,ms format. Returns time in seconds as float."""
    encoding = detect_encoding(file_path)
    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        content = f.read()

    # Split by timestamp pattern (supports HH:MM:SS or HH:MM:SS,ms)
    pattern = r"^(\d{1,2}:\d{2}:\d{2}(?:,\d{1,3})?)$"
    lines = content.split("\n")

    def parse_time(time_str):
        """Convert HH:MM:SS or HH:MM:SS,ms to seconds as float."""
        if "," in time_str:
            time_part, ms_part = time_str.split(",")
            parts = time_part.split(":")
            seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            milliseconds = int(ms_part.ljust(3, "0"))  # Pad to 3 digits
            return seconds + milliseconds / 1000.0
        else:
            parts = time_str.split(":")
            return float(int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))

    entries = []
    current_time = None
    current_text = []
    pre_timestamp_text = []  # Text before first timestamp

    for line in lines:
        match = re.match(pattern, line.strip())
        if match:
            # Save previous entry
            if current_time is not None and current_text:
                text = "\n".join(current_text).strip()
                if text:
                    entries.append((current_time, text))
            elif current_time is None and pre_timestamp_text:
                # First timestamp found, save pre-timestamp text with time 0
                text = "\n".join(pre_timestamp_text).strip()
                if text:
                    entries.append((0.0, text))
                pre_timestamp_text = []

            # Start new entry
            time_str = match.group(1)
            current_time = parse_time(time_str)
            current_text = []
        elif current_time is not None:
            current_text.append(line)
        else:
            # Text before first timestamp
            pre_timestamp_text.append(line)

    # Save last entry
    if current_time is not None and current_text:
        text = "\n".join(current_text).strip()
        if text:
            entries.append((current_time, text))
    elif not entries and pre_timestamp_text:
        # No timestamps found at all, treat entire file as starting at 0
        text = "\n".join(pre_timestamp_text).strip()
        if text:
            entries.append((0.0, text))

    # Convert to subtitle format with end times
    subtitles = []
    for i, (start_time, text) in enumerate(entries):
        end_time = entries[i + 1][0] if i + 1 < len(entries) else None
        # Remove chapter markers and metadata tags
        text = clean_subtitle_text(text)
        if text:  # Only add non-empty entries
            subtitles.append((start_time, end_time, text))

    return subtitles


def parse_ass_file(file_path):
    """
    Parse an ASS/SSA subtitle file and return a list of subtitle entries.

    Args:
        file_path: Path to the ASS/SSA file

    Returns:
        List of tuples: [(start_time_seconds, end_time_seconds, text), ...]
    """
    encoding = detect_encoding(file_path)
    with open(file_path, "r", encoding=encoding, errors="replace") as f:
        lines = f.readlines()

    subtitles = []
    in_events = False
    format_indices = {}

    for line in lines:
        line = line.strip()

        if line.startswith("[Events]"):
            in_events = True
            continue

        if line.startswith("[") and in_events:
            # New section, stop processing
            break

        if in_events and line.startswith("Format:"):
            # Parse format line to know column positions
            parts = line.split(":", 1)[1].strip().split(",")
            for i, part in enumerate(parts):
                format_indices[part.strip().lower()] = i
            continue

        if in_events and (line.startswith("Dialogue:") or line.startswith("Comment:")):
            if line.startswith("Comment:"):
                continue  # Skip comments

            parts = line.split(":", 1)[1].strip().split(",", len(format_indices) - 1)

            if (
                "start" in format_indices
                and "end" in format_indices
                and "text" in format_indices
            ):
                start_str = parts[format_indices["start"]].strip()
                end_str = parts[format_indices["end"]].strip()
                text = parts[format_indices["text"]].strip()

                # Convert timestamp to seconds (ASS format: H:MM:SS.CS where CS is centiseconds)
                def ass_time_to_seconds(t):
                    parts = t.split(":")
                    if len(parts) == 3:
                        h, m, s = parts
                        s_parts = s.split(".")
                        seconds = float(s_parts[0])
                        centiseconds = float(s_parts[1]) if len(s_parts) > 1 else 0
                        return (
                            int(h) * 3600 + int(m) * 60 + seconds + centiseconds / 100.0
                        )
                    return 0

                start_sec = ass_time_to_seconds(start_str)
                end_sec = ass_time_to_seconds(end_str)

                # Clean text of ASS styling tags
                text = re.sub(r"\{[^}]+\}", "", text)  # Remove {tags}
                text = re.sub(r"\\N", "\n", text)  # Convert \N to newline
                text = re.sub(r"\\n", "\n", text)  # Convert \n to newline
                # Remove chapter markers and metadata tags
                text = clean_subtitle_text(text)

                if text:  # Only add non-empty subtitles
                    subtitles.append((start_sec, end_sec, text))

    return subtitles


def get_sample_voice_text(lang_code):
    return SAMPLE_VOICE_TEXTS.get(lang_code, SAMPLE_VOICE_TEXTS["a"])


def sanitize_name_for_os(name, is_folder=True):
    """
    Sanitize a filename or folder name based on the operating system.

    Args:
        name: The name to sanitize
        is_folder: Whether this is a folder name (default: True)

    Returns:
        Sanitized name safe for the current OS
    """
    if not name:
        return "audiobook"

    system = platform.system()

    if system == "Windows":
        # Windows illegal characters: < > : " / \ | ? *
        # Also can't end with space or dot
        sanitized = re.sub(r'[<>:"/\\|?*]', "_", name)
        # Remove control characters (0-31)
        sanitized = re.sub(r"[\x00-\x1f]", "_", sanitized)
        # Remove trailing spaces and dots
        sanitized = sanitized.rstrip(". ")
        # Windows reserved names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
        reserved = (
            ["CON", "PRN", "AUX", "NUL"]
            + [f"COM{i}" for i in range(1, 10)]
            + [f"LPT{i}" for i in range(1, 10)]
        )
        if sanitized.upper() in reserved or sanitized.upper().split(".")[0] in reserved:
            sanitized = f"_{sanitized}"
    elif system == "Darwin":  # macOS
        # macOS illegal characters: : (colon is converted to / by the system)
        # Also can't start with dot (hidden file) for folders typically
        sanitized = re.sub(r"[:]", "_", name)
        # Remove control characters
        sanitized = re.sub(r"[\x00-\x1f]", "_", sanitized)
        # Avoid leading dot for folders (creates hidden folders)
        if is_folder and sanitized.startswith("."):
            sanitized = "_" + sanitized[1:]
    else:  # Linux and others
        # Linux illegal characters: / and null character
        # Though / is illegal, most other chars are technically allowed
        sanitized = re.sub(r"[/\x00]", "_", name)
        # Remove other control characters for safety
        sanitized = re.sub(r"[\x01-\x1f]", "_", sanitized)
        # Avoid leading dot for folders (creates hidden folders)
        if is_folder and sanitized.startswith("."):
            sanitized = "_" + sanitized[1:]

    # Ensure the name is not empty after sanitization
    if not sanitized or sanitized.strip() == "":
        sanitized = "audiobook"

    # Limit length to 255 characters (common limit across filesystems)
    if len(sanitized) > 255:
        sanitized = sanitized[:255].rstrip(". ")

    return sanitized


class CountdownDialog(QDialog):
    """Base dialog with auto-accept countdown functionality"""

    def __init__(self, title, countdown_seconds, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(350)
        self.setWindowFlags(
            self.windowFlags()
            & ~Qt.WindowType.WindowCloseButtonHint
            & ~Qt.WindowType.WindowContextHelpButtonHint
        )

        self.countdown_seconds = countdown_seconds
        self.layout = QVBoxLayout(self)
        self._timer = None
        self._button_box = None

    def add_countdown_and_buttons(self):
        """Add countdown label and OK button - call this after adding custom content"""
        self.countdown_label = QLabel(
            f"Auto-accepting in {self.countdown_seconds} seconds..."
        )
        self.countdown_label.setStyleSheet(f"color: {COLORS['GREEN']};")
        self.layout.addWidget(self.countdown_label)

        self._button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        self._button_box.accepted.connect(self.accept)
        self.layout.addWidget(self._button_box)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_timer_tick)
        self._timer.start(1000)

    def _on_timer_tick(self):
        self.countdown_seconds -= 1
        if self.countdown_seconds > 0:
            self.countdown_label.setText(
                f"Auto-accepting in {self.countdown_seconds} seconds..."
            )
        else:
            self._timer.stop()
            self._button_box.accepted.emit()

    def closeEvent(self, event):
        event.ignore()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            event.ignore()
        else:
            super().keyPressEvent(event)


class ChapterOptionsDialog(CountdownDialog):
    def __init__(self, chapter_count, parent=None):
        super().__init__("Chapter Options", CHAPTER_OPTIONS_COUNTDOWN, parent)

        self.layout.addWidget(
            QLabel(f"Detected {chapter_count} chapters in the text file.")
        )
        self.layout.addWidget(QLabel("How would you like to process these chapters?"))

        self.save_separately_checkbox = QCheckBox("Save each chapter separately")
        self.merge_at_end_checkbox = QCheckBox("Create a merged version at the end")

        self.save_separately_checkbox.setChecked(False)
        self.merge_at_end_checkbox.setChecked(True)

        self.save_separately_checkbox.stateChanged.connect(
            self.update_merge_checkbox_state
        )

        self.layout.addWidget(self.save_separately_checkbox)
        self.layout.addWidget(self.merge_at_end_checkbox)

        self.add_countdown_and_buttons()
        self.update_merge_checkbox_state()

    def update_merge_checkbox_state(self):
        self.merge_at_end_checkbox.setEnabled(self.save_separately_checkbox.isChecked())

    def get_options(self):
        return {
            "save_chapters_separately": self.save_separately_checkbox.isChecked(),
            "merge_chapters_at_end": self.merge_at_end_checkbox.isChecked()
            and self.merge_at_end_checkbox.isEnabled(),
        }


class TimestampDetectionDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Timestamps Detected")
        self.setMinimumWidth(350)
        self.use_timestamps_result = True
        self.countdown_seconds = CHAPTER_OPTIONS_COUNTDOWN

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("This file contains timestamps in HH:MM:SS format."))
        layout.addWidget(
            QLabel("Do you want to use these timestamps for precise audio timing?")
        )

        yes_label = QLabel(
            "• Yes: Generate audio that matches each timestamp (subtitle mode will be ignored)"
        )
        yes_label.setStyleSheet(f"color: {COLORS['BLUE_BORDER_HOVER']};")
        layout.addWidget(yes_label)

        no_label = QLabel("• No: Ignore timestamps and process as regular text")
        no_label.setStyleSheet(f"color: {COLORS['ORANGE']};")
        layout.addWidget(no_label)

        # Countdown label
        self.countdown_label = QLabel(
            f"Auto-accepting in {self.countdown_seconds} seconds..."
        )
        self.countdown_label.setStyleSheet(f"color: {COLORS['GREEN']};")
        layout.addWidget(self.countdown_label)

        button_box = QDialogButtonBox()
        yes_button = button_box.addButton("Yes", QDialogButtonBox.ButtonRole.AcceptRole)
        no_button = button_box.addButton("No", QDialogButtonBox.ButtonRole.RejectRole)

        yes_button.clicked.connect(lambda: self._set_result(True))
        no_button.clicked.connect(lambda: self._set_result(False))

        layout.addWidget(button_box)

        # Timer for countdown
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_timer_tick)
        self._timer.start(1000)

    def _on_timer_tick(self):
        self.countdown_seconds -= 1
        if self.countdown_seconds > 0:
            self.countdown_label.setText(
                f"Auto-accepting in {self.countdown_seconds} seconds..."
            )
        else:
            self._timer.stop()
            self._set_result(True)

    def _set_result(self, use_timestamps):
        if self._timer:
            self._timer.stop()
        self.use_timestamps_result = use_timestamps
        self.accept()

    def use_timestamps(self):
        return self.use_timestamps_result


class ConversionThread(QThread):
    progress_updated = pyqtSignal(int, str)  # Add str for ETR
    conversion_finished = pyqtSignal(object, object)  # Pass output path as second arg
    log_updated = pyqtSignal(object)  # Updated signal for log updates
    chapters_detected = pyqtSignal(int)  # Signal for chapter detection

    # Default split pattern for TTS processing
    DEFAULT_SPLIT_PATTERN = r"\n+"

    # Languages that should not use split pattern (better handled by Kokoro internally)
    # These languages have different text segmentation rules (no spaces, character-based, etc.)
    NO_SPLIT_LANGUAGES = {"z", "j"}  # Chinese, Japanese

    # Language-specific punctuation patterns for subtitle splitting
    LANGUAGE_PUNCTUATION = {
        "z": {
            "sentence": r"[。！？]",  # Chinese: period, exclamation, question
            "comma": r"[。！？、，]",  # Chinese: includes enumeration comma and comma
        },
        "j": {
            "sentence": r"[。！？]",  # Japanese: period, exclamation, question
            "comma": r"[。！？、，]",  # Japanese: includes enumeration comma and comma
        },
    }

    def __init__(
        self,
        file_name,
        lang_code,
        speed,
        voice,
        save_option,
        output_folder,
        subtitle_mode,
        output_format,
        np_module,
        kpipeline_class,
        start_time,
        total_char_count,
        use_gpu=True,
        from_queue=False,
        save_base_path=None,
    ):  # Add use_gpu parameter
        super().__init__()
        self._chapter_options_event = threading.Event()
        self.np = np_module
        self.KPipeline = kpipeline_class
        self.file_name = file_name
        self.lang_code = lang_code
        self.speed = speed
        self.voice = voice
        self.save_option = save_option
        self.output_folder = output_folder
        self.subtitle_mode = subtitle_mode
        self.cancel_requested = False
        self.should_cancel = False
        self.process = None
        self.output_format = output_format
        self.from_queue = from_queue
        self.start_time = start_time  # Store start_time
        self.total_char_count = total_char_count  # Use passed total character count
        self.processed_char_count = 0  # Initialize processed character count
        self.display_path = None  # Add variable for display path
        self.save_base_path = save_base_path  # Store the save base path
        self.is_direct_text = (
            False  # Flag to indicate if input is from textbox rather than file
        )
        self.chapter_options_set = False
        self.waiting_for_user_input = False
        self.use_gpu = use_gpu  # Store the GPU setting
        self.max_subtitle_words = 50  # Default value, will be overridden from GUI
        self.silence_duration = 2.0  # Default value, will be overridden from GUI
        # Set split pattern based on language - some languages handle splitting better internally
        self.split_pattern = (
            None if lang_code in self.NO_SPLIT_LANGUAGES else self.DEFAULT_SPLIT_PATTERN
        )

    def _stream_audio_in_chunks(
        self, segments, process_func, progress_prefix="Processing"
    ):
        """
        Process audio segments in memory-efficient chunks

        Args:
            segments: List of audio segments to process
            process_func: Function that takes (segment_bytes, is_last) and processes a chunk
            progress_prefix: Prefix for progress messages

        Returns:
            Total samples processed
        """
        # Calculate total size for progress reporting
        total_samples = sum(len(segment) for segment in segments)
        samples_processed = 0

        self.log_updated.emit(f"\n{progress_prefix} segments...")

        # Stream each segment individually
        for i, segment in enumerate(segments):
            try:
                # Handle both NumPy arrays and PyTorch tensors
                if hasattr(segment, "astype"):
                    segment_bytes = segment.astype("float32").tobytes()
                else:
                    segment_bytes = segment.cpu().numpy().astype("float32").tobytes()
                is_last = i == len(segments) - 1

                # Update progress periodically - skip if there's only one segment
                if (i % 20 == 0 or is_last) and len(segments) > 1:
                    progress_percent = int((samples_processed / total_samples) * 100)
                    self.log_updated.emit(
                        f"{progress_prefix} segment {i+1}/{len(segments)} ({progress_percent}% complete)"
                    )

                # Process this segment
                process_func(segment_bytes, is_last)

                # Update samples processed
                samples_processed += len(segment)

                # Clear segment bytes from memory
                del segment_bytes
            except Exception as e:
                self.log_updated.emit(f"Error processing segment {i}: {str(e)}")
                raise

        return samples_processed

    def run(self):
        print(
            f"\nVoice: {self.voice}\nLanguage: {self.lang_code}\nSpeed: {self.speed}\nGPU: {self.use_gpu}\nFile: {self.file_name}\nSubtitle mode: {self.subtitle_mode}\nOutput format: {self.output_format}\nSave option: {self.save_option}\n"
        )
        try:
            hf_tracker.set_log_callback(lambda msg: self.log_updated.emit(msg))
            # Show configuration
            self.log_updated.emit("Configuration:")

            # Determine input file and processing file
            if getattr(self, "from_queue", False):
                input_file = self.save_base_path or self.file_name
                processing_file = self.file_name
            else:
                input_file = self.display_path if self.display_path else self.file_name
                processing_file = self.file_name

            # Normalize paths for consistent display (fixes Windows path separator issues)
            input_file = os.path.normpath(input_file) if input_file else input_file
            processing_file = (
                os.path.normpath(processing_file)
                if processing_file
                else processing_file
            )

            self.log_updated.emit(f"- Input File: {input_file}")
            if input_file != processing_file:
                self.log_updated.emit(f"- Processing File: {processing_file}")

            # Use file_name for logs if from_queue, otherwise use display_path if available
            if getattr(self, "from_queue", False):
                base_path = (
                    self.save_base_path or self.file_name
                )  # Use save_base_path if available
            else:
                base_path = self.display_path if self.display_path else self.file_name

            # Use file size string passed from GUI
            if hasattr(self, "file_size_str"):
                self.log_updated.emit(f"- File size: {self.file_size_str}")

            self.log_updated.emit(f"- Total characters: {int(self.total_char_count):,}")

            self.log_updated.emit(
                f"- Language: {self.lang_code} ({LANGUAGE_DESCRIPTIONS.get(self.lang_code, 'Unknown')})"
            )
            self.log_updated.emit(f"- Voice: {self.voice}")
            self.log_updated.emit(f"- Speed: {self.speed}")
            self.log_updated.emit(f"- Subtitle mode: {self.subtitle_mode}")
            self.log_updated.emit(f"- Output format: {self.output_format}")
            self.log_updated.emit(
                f"- Subtitle format: {next((label for value, label in SUBTITLE_FORMATS if value == getattr(self, 'subtitle_format', 'srt')), getattr(self, 'subtitle_format', 'srt'))}"
            )
            self.log_updated.emit(f"- Save option: {self.save_option}")
            if self.replace_single_newlines:
                self.log_updated.emit(f"- Replace single newlines: Yes")

            # Check if input is a subtitle file for additional configuration
            is_subtitle_input = False
            if not self.is_direct_text and self.file_name:
                file_ext = os.path.splitext(self.file_name)[1].lower()
                if file_ext in [".srt", ".ass", ".vtt"]:
                    is_subtitle_input = True

            # Display subtitle-specific options if processing subtitle file
            if is_subtitle_input:
                if getattr(self, "use_silent_gaps", False):
                    self.log_updated.emit("- Use silent gaps: Yes")
                speed_method = getattr(self, "subtitle_speed_method", "tts")
                method_label = (
                    "TTS Regeneration"
                    if speed_method == "tts"
                    else "FFmpeg Time-stretch"
                )
                self.log_updated.emit(f"- Speed adjustment method: {method_label}")

            # Display save_chapters_separately flag if it's set
            if hasattr(self, "save_chapters_separately"):
                self.log_updated.emit(
                    (
                        f"- Save chapters separately: {'Yes' if self.save_chapters_separately else 'No'}"
                    )
                )
                # Display merge_chapters_at_end flag if save_chapters_separately is True
                if self.save_chapters_separately:
                    merge_at_end = getattr(self, "merge_chapters_at_end", True)
                    self.log_updated.emit(
                        f"- Merge chapters at the end: {'Yes' if merge_at_end else 'No'}"
                    )
                    # Display the separate chapters format if it's set
                    separate_format = getattr(self, "separate_chapters_format", "wav")
                    self.log_updated.emit(
                        f"- Separate chapters format: {separate_format}"
                    )

            # If merge_at_end is True, display the silence duration
            if getattr(self, "merge_chapters_at_end", True):
                self.log_updated.emit(
                    f"- Silence between chapters: {self.silence_duration} seconds"
                )

            if self.save_option == "Choose output folder":
                self.log_updated.emit(
                    f"- Output folder: {self.output_folder or os.getcwd()}"
                )

            self.log_updated.emit("\nInitializing TTS pipeline...")

            # Set device based on use_gpu setting and platform
            if self.use_gpu:
                if platform.system() == "Darwin" and platform.processor() == "arm":
                    device = "mps"  # Use MPS for Apple Silicon
                else:
                    device = "cuda"  # Use CUDA for other platforms
            else:
                device = "cpu"

            tts = self.KPipeline(
                lang_code=self.lang_code, repo_id="hexgrad/Kokoro-82M", device=device
            )

            # Check if the input is a subtitle file or timestamp text file
            is_subtitle_file = False
            is_timestamp_text = False
            if not self.is_direct_text and self.file_name:
                file_ext = os.path.splitext(self.file_name)[1].lower()
                if file_ext in [".srt", ".ass", ".vtt"]:
                    is_subtitle_file = True
                    self.log_updated.emit(
                        f"\nDetected subtitle file format: {file_ext}"
                    )
                elif file_ext == ".txt" and detect_timestamps_in_text(self.file_name):
                    is_timestamp_text = True
                    self.log_updated.emit("\nDetected timestamps in text file")
                    # Signal to ask user (-1 indicates timestamp detection)
                    self.chapters_detected.emit(-1)
                    # Wait for user response
                    while not hasattr(self, "_timestamp_response"):
                        if self.cancel_requested:
                            self.conversion_finished.emit("Cancelled", None)
                            return
                        time.sleep(0.1)
                    if not self._timestamp_response:
                        is_timestamp_text = False
                    delattr(self, "_timestamp_response")

            # Process subtitle files separately
            if is_subtitle_file or is_timestamp_text:
                self._process_subtitle_file(tts, base_path, is_timestamp_text)
                return

            if self.is_direct_text:
                text = self.file_name  # Treat file_name as direct text input
            else:
                encoding = detect_encoding(self.file_name)
                with open(
                    self.file_name, "r", encoding=encoding, errors="replace"
                ) as file:
                    text = file.read()

            # Clean up text using utility function
            text = clean_text(text)

            # Remove metadata markers from the text to be processed
            metadata_pattern = r"<<METADATA_[^:]+:[^>]*>>"
            text = re.sub(metadata_pattern, "", text)

            # --- Chapter splitting logic ---
            chapter_pattern = r"<<CHAPTER_MARKER:(.*?)>>"
            chapter_splits = list(re.finditer(chapter_pattern, text))
            chapters = []
            if chapter_splits:
                # prepend Introduction for content before first marker
                first_start = chapter_splits[0].start()
                if first_start > 0:
                    intro_text = text[:first_start].strip()
                    if intro_text:
                        chapters.append(("Introduction", intro_text))
                for idx, match in enumerate(chapter_splits):
                    start = match.end()
                    end = (
                        chapter_splits[idx + 1].start()
                        if idx + 1 < len(chapter_splits)
                        else len(text)
                    )
                    chapter_name = match.group(1).strip()
                    chapter_text = text[start:end].strip()
                    chapters.append((chapter_name, chapter_text))
            else:
                chapters = [("text", text)]
            total_chapters = len(chapters)

            # For text files with chapters, prompt user for options if not already set
            is_txt_file = not self.is_direct_text and (
                self.file_name.lower().endswith(".txt")
                or (self.display_path and self.display_path.lower().endswith(".txt"))
            )

            if (
                is_txt_file
                and total_chapters > 1
                and (
                    not hasattr(self, "save_chapters_separately")
                    or not hasattr(self, "merge_chapters_at_end")
                )
                and not self.chapter_options_set
            ):

                # Emit signal to main thread and wait
                self.chapters_detected.emit(total_chapters)
                self._chapter_options_event.wait()
                if self.cancel_requested:
                    self.conversion_finished.emit("Cancelled", None)
                    return
                self.chapter_options_set = True

            # Log all detected chapters at the beginning
            if total_chapters > 1:
                chapter_list = "\n".join(
                    [f"{i+1}) {c[0]}" for i, c in enumerate(chapters)]
                )
                self.log_updated.emit(
                    (f"\nDetected chapters ({total_chapters}):\n" + chapter_list)
                )
            else:
                self.log_updated.emit((f"\nProcessing {chapters[0][0]}..."))

            # If save_chapters_separately is enabled, find a unique suffix ONCE and use for both folder and merged file
            save_chapters_separately = getattr(self, "save_chapters_separately", False)
            merge_chapters_at_end = getattr(self, "merge_chapters_at_end", True)

            # Ensure merge_chapters_at_end is True if not saving chapters separately
            if not save_chapters_separately:
                merge_chapters_at_end = True

            chapters_out_dir = None
            suffix = ""

            # Use file_name for logs if from_queue, otherwise use display_path if available
            if getattr(self, "from_queue", False):
                base_path = (
                    self.save_base_path or self.file_name
                )  # Use save_base_path if available
            else:
                base_path = self.display_path if self.display_path else self.file_name

            base_name = os.path.splitext(os.path.basename(base_path))[0]
            # Sanitize base_name for folder/file creation based on OS
            sanitized_base_name = sanitize_name_for_os(base_name, is_folder=True)

            if self.save_option == "Save to Desktop":
                parent_dir = user_desktop_dir()
            elif self.save_option == "Save next to input file":
                parent_dir = os.path.dirname(base_path)
            else:
                parent_dir = self.output_folder or os.getcwd()
            # Ensure the output folder exists, error if it doesn't
            if not os.path.exists(parent_dir):
                self.log_updated.emit(
                    (
                        f"Output folder does not exist: {parent_dir}",
                        "red",
                    )
                )
            # Find a unique suffix for both folder and merged file, always
            counter = 1
            allowed_exts = set(SUPPORTED_SOUND_FORMATS + SUPPORTED_SUBTITLE_FORMATS)
            while True:
                suffix = f"_{counter}" if counter > 1 else ""
                chapters_out_dir_candidate = os.path.join(
                    parent_dir, f"{sanitized_base_name}{suffix}_chapters"
                )
                # Only check for files with allowed extensions (extension without dot, case-insensitive)
                clash = any(
                    os.path.splitext(fname)[0] == f"{sanitized_base_name}{suffix}"
                    and os.path.splitext(fname)[1][1:].lower() in allowed_exts
                    for fname in os.listdir(parent_dir)
                )
                if not os.path.exists(chapters_out_dir_candidate) and not clash:
                    break
                counter += 1
            if save_chapters_separately and total_chapters > 1:
                separate_chapters_format = getattr(
                    self, "separate_chapters_format", "wav"
                )
                chapters_out_dir = chapters_out_dir_candidate
                os.makedirs(chapters_out_dir, exist_ok=True)
                self.log_updated.emit(
                    (f"\nChapters output folder: {chapters_out_dir}", "grey")
                )

            # Prepare merged output file for incremental writing ONLY if merge_chapters_at_end is True
            if merge_chapters_at_end:
                out_dir = parent_dir
                base_filepath_no_ext = os.path.join(
                    out_dir, f"{sanitized_base_name}{suffix}"
                )
                merged_out_path = f"{base_filepath_no_ext}.{self.output_format}"
                subtitle_entries = []
                current_time = 0.0
                rate = 24000
                subtitle_mode = self.subtitle_mode
                self.etr_start_time = time.time()
                self.processed_char_count = 0
                current_segment = 0
                chapters_time = [
                    {"chapter": chapter[0], "start": 0.0, "end": 0.0}
                    for chapter in chapters
                ]
                # SRT numbering fix: use a global counter
                merged_srt_index = 1  # SRT numbering for merged file
                # Prepare output file/ffmpeg process for merged output
                if self.output_format in ["wav", "mp3", "flac"]:
                    merged_out_file = sf.SoundFile(
                        merged_out_path,
                        "w",
                        samplerate=24000,
                        channels=1,
                        format=self.output_format,
                    )
                    ffmpeg_proc = None
                elif self.output_format == "m4b":
                    # Real-time M4B generation using FFmpeg pipe
                    static_ffmpeg.add_paths()
                    merged_out_file = None
                    ffmpeg_proc = None
                    metadata_options, cover_path = (
                        self._extract_and_add_metadata_tags_to_ffmpeg_cmd()
                    )
                    # Prepare ffmpeg command for m4b output
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-thread_queue_size",
                        "32768",
                        "-f",
                        "f32le",
                        "-ar",
                        "24000",
                        "-ac",
                        "1",
                        "-i",
                        "pipe:0",
                    ]
                    if cover_path and os.path.exists(cover_path):
                        cmd.extend(
                            [
                                "-i",
                                cover_path,
                                "-map",
                                "0:a",
                                "-map",
                                "1",
                                "-c:v",
                                "copy",
                                "-disposition:v",
                                "attached_pic",
                            ]
                        )
                    cmd.extend(
                        [
                            "-c:a",
                            "aac",
                            "-q:a",
                            "2",
                            "-movflags",
                            "+faststart+use_metadata_tags",
                        ]
                    )
                    cmd += metadata_options
                    cmd.append(merged_out_path)
                    ffmpeg_proc = create_process(cmd, stdin=subprocess.PIPE, text=False)
                elif self.output_format == "opus":
                    static_ffmpeg.add_paths()
                    cmd = [
                        "ffmpeg",
                        "-y",
                        "-thread_queue_size",
                        "32768",
                        "-f",
                        "f32le",
                        "-ar",
                        "24000",
                        "-ac",
                        "1",
                        "-i",
                        "pipe:0",
                    ]
                    cmd.extend(["-c:a", "libopus", "-b:a", "24000"])
                    cmd.append(merged_out_path)
                    ffmpeg_proc = create_process(cmd, stdin=subprocess.PIPE, text=False)
                    merged_out_file = None
                else:
                    self.log_updated.emit(
                        (f"Unsupported output format: {self.output_format}", "red")
                    )
                    self.conversion_finished.emit(
                        ("Audio generation failed.", "red"), None
                    )
                    return
                # Open merged subtitle file for incremental writing if needed
                merged_subtitle_file = None
                if self.subtitle_mode != "Disabled":
                    subtitle_format = getattr(self, "subtitle_format", "srt")
                    file_extension = "ass" if "ass" in subtitle_format else "srt"
                    merged_subtitle_path = (
                        os.path.splitext(merged_out_path)[0] + f".{file_extension}"
                    )
                    # Default subtitle layout flags/strings so they exist regardless
                    # of whether ASS-specific handling runs. This prevents runtime
                    # errors when non-ASS formats (like SRT) are selected.
                    is_centered = False
                    is_narrow = False
                    merged_subtitle_margin = ""
                    merged_subtitle_alignment_tag = ""
                    if "ass" in subtitle_format:
                        merged_subtitle_file = open(
                            merged_subtitle_path,
                            "w",
                            encoding="utf-8",
                            errors="replace",
                        )
                        # Minimal ASS header
                        merged_subtitle_file.write("[Script Info]\n")
                        merged_subtitle_file.write("Title: Generated by Abogen\n")
                        merged_subtitle_file.write("ScriptType: v4.00+\n\n")
                        # Add style definitions for karaoke highlighting
                        if self.subtitle_mode == "Sentence + Highlighting":
                            merged_subtitle_file.write("[V4+ Styles]\n")
                            merged_subtitle_file.write(
                                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                            )
                            merged_subtitle_file.write(
                                "Style: Default,Arial,24,&H00FFFFFF,&H00808080,&H00000000,&H00404040,0,0,0,0,100,100,0,0,3,2,0,5,10,10,10,1\n\n"
                            )
                        merged_subtitle_file.write("[Events]\n")
                        merged_subtitle_file.write(
                            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                        )
                        # Set margin/alignment for ASS
                        is_centered = subtitle_format in (
                            "ass_centered_wide",
                            "ass_centered_narrow",
                        )
                        is_narrow = subtitle_format in (
                            "ass_narrow",
                            "ass_centered_narrow",
                        )
                        merged_subtitle_margin = "90" if is_narrow else ""
                        merged_subtitle_alignment_tag = (
                            f"{{\\an5}}" if is_centered else ""
                        )
                    else:
                        merged_subtitle_file = open(
                            merged_subtitle_path,
                            "w",
                            encoding="utf-8",
                            errors="replace",
                        )
                else:
                    merged_subtitle_path = None
                    merged_subtitle_file = None
            else:
                # If not merging, set merged_out_file and related variables to None
                merged_out_file = None
                ffmpeg_proc = None
                merged_out_path = None
                subtitle_entries = []
                current_time = 0.0
                rate = 24000
                subtitle_mode = self.subtitle_mode
                self.etr_start_time = time.time()
                self.processed_char_count = 0
                current_segment = 0
                chapters_time = [
                    {"chapter": chapter[0], "start": 0.0, "end": 0.0}
                    for chapter in chapters
                ]
                srt_index = 1  # SRT numbering fix for chapter-only mode
            # Instead of processing the whole text, process by chapter
            for chapter_idx, (chapter_name, chapter_text) in enumerate(chapters, 1):
                chapter_out_path = None
                chapter_out_file = None
                chapter_ffmpeg_proc = None
                chapter_subtitle_file = None
                chapter_subtitle_path = None
                if total_chapters > 1:
                    self.log_updated.emit(
                        (
                            f"\nChapter {chapter_idx}/{total_chapters}: {chapter_name}",
                            "blue",
                        )
                    )
                chapter_subtitle_entries = []
                chapter_current_time = 0.0
                # Set chapter start time before processing
                chapter_time = chapters_time[chapter_idx - 1]
                if merge_chapters_at_end:
                    chapter_time["start"] = current_time

                # Check if the voice is a formula and load it if necessary
                if "*" in self.voice:
                    loaded_voice = get_new_voice(tts, self.voice, self.use_gpu)
                else:
                    loaded_voice = self.voice
                # Prepare per-chapter output file if needed
                if save_chapters_separately and total_chapters > 1:
                    # First pass: keep alphanumeric, spaces, hyphens, and underscores
                    sanitized = re.sub(r"[^\w\s\-]", "", chapter_name)
                    # Replace multiple spaces/hyphens with single underscore
                    sanitized = re.sub(r"[\s\-]+", "_", sanitized).strip("_")
                    # Apply OS-specific sanitization
                    sanitized = sanitize_name_for_os(sanitized, is_folder=False)
                    # Limit length (leaving room for the chapter number prefix)
                    MAX_LEN = 80
                    if len(sanitized) > MAX_LEN:
                        pos = sanitized[:MAX_LEN].rfind("_")
                        sanitized = sanitized[: pos if pos > 0 else MAX_LEN].rstrip("_")
                    chapter_filename = f"{chapter_idx:02d}_{sanitized}"
                    chapter_out_path = os.path.join(
                        chapters_out_dir,
                        f"{chapter_filename}.{separate_chapters_format}",
                    )
                    if separate_chapters_format in ["wav", "mp3", "flac"]:
                        chapter_out_file = sf.SoundFile(
                            chapter_out_path,
                            "w",
                            samplerate=24000,
                            channels=1,
                            format=separate_chapters_format,
                        )
                        chapter_ffmpeg_proc = None
                    elif separate_chapters_format == "opus":
                        static_ffmpeg.add_paths()
                        cmd = [
                            "ffmpeg",
                            "-y",
                            "-thread_queue_size",
                            "32768",
                            "-f",
                            "f32le",
                            "-ar",
                            "24000",
                            "-ac",
                            "1",
                            "-i",
                            "pipe:0",
                        ]
                        cmd.extend(["-c:a", "libopus", "-b:a", "24000"])
                        cmd.append(chapter_out_path)
                        chapter_ffmpeg_proc = create_process(
                            cmd, stdin=subprocess.PIPE, text=False
                        )
                        chapter_out_file = None
                    else:
                        self.log_updated.emit(
                            (
                                f"Unsupported chapter format: {separate_chapters_format}",
                                "red",
                            )
                        )
                        continue
                    # Open chapter subtitle file for incremental writing if needed
                    chapter_subtitle_file = None
                    chapter_srt_index = (
                        1  # Initialize SRT numbering for this chapter file
                    )
                    if self.subtitle_mode != "Disabled":
                        subtitle_format = getattr(self, "subtitle_format", "srt")
                        file_extension = "ass" if "ass" in subtitle_format else "srt"
                        chapter_subtitle_path = os.path.join(
                            chapters_out_dir, f"{chapter_filename}.{file_extension}"
                        )
                        # Ensure these variables exist even when not using ASS so
                        # later code can safely reference them.
                        is_centered = False
                        is_narrow = False
                        chapter_subtitle_margin = ""
                        chapter_subtitle_alignment_tag = ""
                        # Open the chapter subtitle file for writing for both SRT and ASS
                        chapter_subtitle_file = open(
                            chapter_subtitle_path,
                            "w",
                            encoding="utf-8",
                            errors="replace",
                        )
                        if "ass" in subtitle_format:
                            # Minimal ASS header
                            chapter_subtitle_file.write("[Script Info]\n")
                            chapter_subtitle_file.write("Title: Generated by Abogen\n")
                            chapter_subtitle_file.write("ScriptType: v4.00+\n\n")

                            # Add style definitions for karaoke highlighting
                            if self.subtitle_mode == "Sentence + Highlighting":
                                chapter_subtitle_file.write("[V4+ Styles]\n")
                                chapter_subtitle_file.write(
                                    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                                )
                                chapter_subtitle_file.write(
                                    "Style: Default,Arial,24,&H00FFFFFF,&H00808080,&H00000000,&H00404040,0,0,0,0,100,100,0,0,3,2,0,5,10,10,10,1\n\n"
                                )

                            chapter_subtitle_file.write("[Events]\n")
                            chapter_subtitle_file.write(
                                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                            )
                            is_centered = subtitle_format in (
                                "ass_centered_wide",
                                "ass_centered_narrow",
                            )
                            is_narrow = subtitle_format in (
                                "ass_narrow",
                                "ass_centered_narrow",
                            )
                            chapter_subtitle_margin = "90" if is_narrow else ""
                            chapter_subtitle_alignment_tag = (
                                f"{{\\an5}}" if is_centered else ""
                            )
                    else:
                        chapter_subtitle_file = None
                else:
                    chapter_subtitle_path = None
                    chapter_subtitle_file = None
                for result in tts(
                    chapter_text,
                    voice=loaded_voice,
                    speed=self.speed,
                    split_pattern=self.split_pattern,
                ):
                    # Print the result for debugging
                    # print(f"Result: {result}")
                    if self.cancel_requested:
                        if chapter_out_file:
                            chapter_out_file.close()
                        if merged_out_file:
                            merged_out_file.close()
                        self.conversion_finished.emit("Cancelled", None)
                        return
                    current_segment += 1
                    grapheme_len = len(result.graphemes)
                    self.processed_char_count += grapheme_len
                    # Log progress with both character counts and the graphemes content
                    self.log_updated.emit(
                        f"\n{self.processed_char_count:,}/{self.total_char_count:,}: {result.graphemes}"
                    )

                    chunk_dur = len(result.audio) / rate
                    chunk_start = current_time
                    # Write audio directly to merged file ONLY if merging
                    if merge_chapters_at_end and merged_out_file:
                        merged_out_file.write(result.audio)
                    elif merge_chapters_at_end and ffmpeg_proc:
                        if hasattr(result.audio, "numpy"):
                            audio_bytes = (
                                result.audio.numpy().astype("float32").tobytes()
                            )
                        else:
                            audio_bytes = result.audio.astype("float32").tobytes()
                        ffmpeg_proc.stdin.write(audio_bytes)
                    if chapter_out_file:
                        chapter_out_file.write(result.audio)
                    elif chapter_ffmpeg_proc:
                        if hasattr(result.audio, "numpy"):
                            audio_bytes = (
                                result.audio.numpy().astype("float32").tobytes()
                            )
                        else:
                            audio_bytes = result.audio.astype("float32").tobytes()
                        chapter_ffmpeg_proc.stdin.write(audio_bytes)
                    # Subtitle logic
                    if self.subtitle_mode != "Disabled":
                        tokens_list = getattr(result, "tokens", [])
                        tokens_with_timestamps = []
                        chapter_tokens_with_timestamps = []

                        # Process every token, regardless of text or timestamps
                        for tok in tokens_list:
                            tokens_with_timestamps.append(
                                {
                                    "start": chunk_start + (tok.start_ts or 0),
                                    "end": chunk_start + (tok.end_ts or 0),
                                    "text": tok.text,
                                    "whitespace": tok.whitespace,
                                }
                            )
                            if chapter_out_file or chapter_ffmpeg_proc:
                                chapter_tokens_with_timestamps.append(
                                    {
                                        "start": chapter_current_time
                                        + (tok.start_ts or 0),
                                        "end": chapter_current_time + (tok.end_ts or 0),
                                        "text": tok.text,
                                        "whitespace": tok.whitespace,
                                    }
                                )
                        # Process tokens according to subtitle mode
                        # Global subtitle processing ONLY if merging
                        if merge_chapters_at_end:
                            # Incremental subtitle writing for merged output
                            new_entries = []
                            self._process_subtitle_tokens(
                                tokens_with_timestamps,
                                new_entries,
                                self.max_subtitle_words,
                                fallback_end_time=chunk_start + chunk_dur,
                            )
                            if merged_subtitle_file:
                                subtitle_format = getattr(
                                    self, "subtitle_format", "srt"
                                )
                                if "ass" in subtitle_format:
                                    for start, end, text in new_entries:
                                        start_time = self._ass_time(start)
                                        end_time = self._ass_time(end)
                                        # Use karaoke effect for highlighting mode
                                        effect = (
                                            "karaoke"
                                            if self.subtitle_mode
                                            == "Sentence + Highlighting"
                                            else ""
                                        )
                                        merged_subtitle_file.write(
                                            f"Dialogue: 0,{start_time},{end_time},Default,,{merged_subtitle_margin},{merged_subtitle_margin},0,{effect},{merged_subtitle_alignment_tag}{text}\n"
                                        )
                                else:
                                    for entry in new_entries:
                                        start, end, text = entry
                                        merged_subtitle_file.write(
                                            f"{merged_srt_index}\n{self._srt_time(start)} --> {self._srt_time(end)}\n{text}\n\n"
                                        )
                                        merged_srt_index += 1
                        # Per-chapter subtitle processing for both file and ffmpeg_proc
                        if chapter_out_file or chapter_ffmpeg_proc:
                            new_chapter_entries = []
                            self._process_subtitle_tokens(
                                chapter_tokens_with_timestamps,
                                new_chapter_entries,
                                self.max_subtitle_words,
                                fallback_end_time=chapter_current_time + chunk_dur,
                            )
                            if chapter_subtitle_file:
                                subtitle_format = getattr(
                                    self, "subtitle_format", "srt"
                                )
                                if "ass" in subtitle_format:
                                    for start, end, text in new_chapter_entries:
                                        start_time = self._ass_time(start)
                                        end_time = self._ass_time(end)
                                        # Use karaoke effect for highlighting mode
                                        effect = (
                                            "karaoke"
                                            if self.subtitle_mode
                                            == "Sentence + Highlighting"
                                            else ""
                                        )
                                        chapter_subtitle_file.write(
                                            f"Dialogue: 0,{start_time},{end_time},Default,,{chapter_subtitle_margin},{chapter_subtitle_margin},0,{effect},{chapter_subtitle_alignment_tag}{text}\n"
                                        )
                                else:
                                    for entry in new_chapter_entries:
                                        start, end, text = entry
                                        chapter_subtitle_file.write(
                                            f"{chapter_srt_index}\n{self._srt_time(start)} --> {self._srt_time(end)}\n{text}\n\n"
                                        )
                                        chapter_srt_index += 1
                    if merge_chapters_at_end:
                        current_time += chunk_dur
                        if chapter_out_file or chapter_ffmpeg_proc:
                            chapter_current_time += chunk_dur
                    else:
                        if chapter_out_file or chapter_ffmpeg_proc:
                            chapter_current_time += chunk_dur
                    # Calculate percentage based on characters processed
                    percent = min(
                        int(self.processed_char_count / self.total_char_count * 100), 99
                    )

                    # Calculate ETR based on characters processed
                    etr_str = "Processing..."
                    chars_done = self.processed_char_count
                    elapsed = time.time() - self.etr_start_time

                    # Calculate ETR if enough data is available
                    if (
                        chars_done > 0 and elapsed > 0.5
                    ):  # Check elapsed > 0.5 to avoid instability
                        avg_time_per_char = elapsed / chars_done
                        remaining = self.total_char_count - self.processed_char_count
                        if remaining > 0:
                            secs = avg_time_per_char * remaining
                            h = int(secs // 3600)
                            m = int((secs % 3600) // 60)
                            s = int(secs % 60)
                            etr_str = f"{h:02d}:{m:02d}:{s:02d}"

                    # Update progress more frequently (after each result)
                    self.progress_updated.emit(percent, etr_str)

                # Add silence between chapters for merged output (except after the last chapter)
                if merge_chapters_at_end and chapter_idx < total_chapters:
                    silence_samples = int(
                        self.silence_duration * 24000
                    )  # Silence duration at 24,000 Hz
                    silence_audio = self.np.zeros(silence_samples, dtype="float32")
                    silence_bytes = silence_audio.tobytes()

                    if merged_out_file:
                        merged_out_file.write(silence_audio)
                    elif ffmpeg_proc:
                        ffmpeg_proc.stdin.write(silence_bytes)

                    # Update timing for the silence
                    current_time += self.silence_duration
                    if chapter_out_file or chapter_ffmpeg_proc:
                        chapter_current_time += self.silence_duration

                # Set chapter end time after processing
                if merge_chapters_at_end:
                    chapter_time["end"] = current_time
                # Finalize chapter file for ffmpeg formats
                if chapter_out_file or chapter_ffmpeg_proc:
                    self.log_updated.emit(("\nProcessing chapter audio...", "grey"))
                if chapter_ffmpeg_proc:
                    chapter_ffmpeg_proc.stdin.close()
                    chapter_ffmpeg_proc.wait()
                if chapter_out_file:
                    chapter_out_file.close()
                # Close chapter subtitle file if open
                if chapter_subtitle_file:
                    chapter_subtitle_file.close()
                if (
                    save_chapters_separately
                    and total_chapters > 1
                    and self.subtitle_mode != "Disabled"
                    and chapter_subtitle_path
                ):
                    self.log_updated.emit(
                        (
                            f"\nChapter {chapter_idx} saved to: {chapter_out_path}\n\nChapter subtitle saved to: {chapter_subtitle_path}",
                            "green",
                        )
                    )
                elif chapter_out_path:
                    self.log_updated.emit(
                        (
                            f"\nChapter {chapter_idx} saved to: {chapter_out_path}",
                            "green",
                        )
                    )
            # Finalize merged output file ONLY if merging
            if merge_chapters_at_end:
                self.log_updated.emit(("\nFinalizing audio. Please wait...", "grey"))
                if self.output_format in ["wav", "mp3", "flac"]:
                    merged_out_file.close()
                elif self.output_format == "m4b":
                    ffmpeg_proc.stdin.close()
                    ffmpeg_proc.wait()
                    # Add chapters via fast post-processing
                    if total_chapters > 1:
                        chapters_info_path = f"{base_filepath_no_ext}_chapters.txt"
                        with open(chapters_info_path, "w", encoding="utf-8") as f:
                            f.write(";FFMETADATA1\n")
                            for chapter in chapters_time:
                                chapter_title = chapter["chapter"].replace("=", "\\=")
                                f.write(f"[CHAPTER]\n")
                                f.write(f"TIMEBASE=1/1000\n")
                                f.write(f"START={int(chapter['start']*1000)}\n")
                                f.write(f"END={int(chapter['end']*1000)}\n")
                                f.write(f"title={chapter_title}\n\n")
                        # Fast mux chapters into m4b (write to temp file, then replace original)
                        static_ffmpeg.add_paths()
                        orig_path = merged_out_path
                        root, ext = os.path.splitext(orig_path)
                        tmp_path = root + ".tmp" + ext
                        metadata_options, cover_path = (
                            self._extract_and_add_metadata_tags_to_ffmpeg_cmd()
                        )
                        cmd = [
                            "ffmpeg",
                            "-y",
                            "-i",
                            orig_path,
                            "-i",
                            chapters_info_path,
                        ]
                        if cover_path and os.path.exists(cover_path):
                            cmd.extend(
                                [
                                    "-i",
                                    cover_path,
                                    "-map",
                                    "0:a",
                                    "-map",
                                    "2",
                                    "-c:v",
                                    "copy",
                                    "-disposition:v",
                                    "attached_pic",
                                ]
                            )
                        else:
                            cmd.extend(["-map", "0:a"])

                        cmd.extend(
                            [
                                "-map_metadata",
                                "1",
                                "-map_chapters",
                                "1",
                                "-c:a",
                                "copy",
                            ]
                        )
                        cmd += metadata_options
                        cmd.append(tmp_path)
                        proc = create_process(cmd)
                        proc.wait()
                        os.replace(tmp_path, orig_path)
                        os.remove(chapters_info_path)
                elif self.output_format in ["opus"]:
                    ffmpeg_proc.stdin.close()
                    ffmpeg_proc.wait()
                self.progress_updated.emit(100, "00:00:00")
                # Close merged subtitle file if open
                if merged_subtitle_file:
                    merged_subtitle_file.close()
            # Subtitle and final message logic
            if merge_chapters_at_end:
                if self.subtitle_mode != "Disabled":
                    self.conversion_finished.emit(
                        (
                            f"\nAudiobook saved to: {merged_out_path}\n\nSubtitle saved to: {merged_subtitle_path}",
                            "green",
                        ),
                        merged_out_path,
                    )
                else:
                    self.conversion_finished.emit(
                        (f"\nAudiobook saved to: {merged_out_path}", "green"),
                        merged_out_path,
                    )
            else:
                # If not merging, report the folder that holds the chapter files
                self.progress_updated.emit(100, "00:00:00")
                chapters_dir = os.path.abspath(chapters_out_dir or parent_dir)
                self.conversion_finished.emit(
                    (f"\nAll chapters saved to: {chapters_dir}", "green"),
                    chapters_dir,
                )
        except Exception as e:
            # Cleanup ffmpeg subprocesses on error
            try:
                if "ffmpeg_proc" in locals() and ffmpeg_proc:
                    ffmpeg_proc.stdin.close()
                    ffmpeg_proc.terminate()
                    ffmpeg_proc.wait()
            except Exception:
                pass
            try:
                if "chapter_ffmpeg_proc" in locals() and chapter_ffmpeg_proc:
                    chapter_ffmpeg_proc.stdin.close()
                    chapter_ffmpeg_proc.terminate()
                    chapter_ffmpeg_proc.wait()
            except Exception:
                pass
            self.log_updated.emit((f"Error occurred: {str(e)}", "red"))
            self.conversion_finished.emit(("Audio generation failed.", "red"), None)

    def _process_subtitle_file(self, tts, base_path, is_timestamp_text=False):
        """Process subtitle files with precise timing and generate output subtitles."""
        try:
            # Parse subtitle file
            if is_timestamp_text:
                subtitles = parse_timestamp_text_file(self.file_name)
            else:
                file_ext = os.path.splitext(self.file_name)[1].lower()
                if file_ext == ".srt":
                    subtitles = parse_srt_file(self.file_name)
                elif file_ext == ".vtt":
                    subtitles = parse_vtt_file(self.file_name)
                else:
                    subtitles = parse_ass_file(self.file_name)

            if not subtitles:
                self.log_updated.emit(("No valid subtitle entries found.", "red"))
                self.conversion_finished.emit(
                    ("No subtitle entries to process.", "red"), None
                )
                return

            self.log_updated.emit(f"\nFound {len(subtitles)} subtitle entries")

            # Setup output paths
            base_name = os.path.splitext(os.path.basename(base_path))[0]
            sanitized_base_name = sanitize_name_for_os(base_name, is_folder=True)
            parent_dir = (
                user_desktop_dir()
                if self.save_option == "Save to Desktop"
                else (
                    os.path.dirname(base_path)
                    if self.save_option == "Save next to input file"
                    else self.output_folder or os.getcwd()
                )
            )

            if not os.path.exists(parent_dir):
                self.log_updated.emit(
                    (f"Output folder does not exist: {parent_dir}", "red")
                )
                return

            # Find unique filename
            counter = 1
            allowed_exts = set(SUPPORTED_SOUND_FORMATS + SUPPORTED_SUBTITLE_FORMATS)
            while True:
                suffix = f"_{counter}" if counter > 1 else ""
                if not any(
                    os.path.splitext(f)[0] == f"{sanitized_base_name}{suffix}"
                    and os.path.splitext(f)[1][1:].lower() in allowed_exts
                    for f in os.listdir(parent_dir)
                ):
                    break
                counter += 1

            base_filepath_no_ext = os.path.join(
                parent_dir, f"{sanitized_base_name}{suffix}"
            )
            merged_out_path = f"{base_filepath_no_ext}.{self.output_format}"
            rate = 24000

            # Setup audio output
            merged_out_file, ffmpeg_proc = None, None
            if self.output_format in ["wav", "mp3", "flac"]:
                merged_out_file = sf.SoundFile(
                    merged_out_path,
                    "w",
                    samplerate=rate,
                    channels=1,
                    format=self.output_format,
                )
            else:
                static_ffmpeg.add_paths()
                cmd = [
                    "ffmpeg",
                    "-y",
                    "-thread_queue_size",
                    "32768",
                    "-f",
                    "f32le",
                    "-ar",
                    str(rate),
                    "-ac",
                    "1",
                    "-i",
                    "pipe:0",
                ]
                if self.output_format == "m4b":
                    metadata_options, cover_path = (
                        self._extract_and_add_metadata_tags_to_ffmpeg_cmd()
                    )
                    if cover_path and os.path.exists(cover_path):
                        cmd.extend(
                            [
                                "-i",
                                cover_path,
                                "-map",
                                "0:a",
                                "-map",
                                "1",
                                "-c:v",
                                "copy",
                                "-disposition:v",
                                "attached_pic",
                            ]
                        )
                    cmd.extend(
                        [
                            "-c:a",
                            "aac",
                            "-q:a",
                            "2",
                            "-movflags",
                            "+faststart+use_metadata_tags",
                        ]
                    )
                    cmd.extend(metadata_options)
                elif self.output_format == "opus":
                    cmd.extend(["-c:a", "libopus", "-b:a", "24000"])
                else:
                    self.log_updated.emit(
                        (f"Unsupported output format: {self.output_format}", "red")
                    )
                    return
                cmd.append(merged_out_path)
                ffmpeg_proc = create_process(cmd, stdin=subprocess.PIPE, text=False)

            # Always generate subtitles for subtitle input files
            subtitle_file, subtitle_path = None, None
            subtitle_format = getattr(self, "subtitle_format", "srt")
            file_extension = "ass" if "ass" in subtitle_format else "srt"
            subtitle_path = f"{base_filepath_no_ext}.{file_extension}"
            subtitle_file = open(subtitle_path, "w", encoding="utf-8", errors="replace")

            if "ass" in subtitle_format:
                # Write ASS header
                subtitle_file.write(
                    "[Script Info]\nTitle: Generated by Abogen\nScriptType: v4.00+\n\n"
                )
                if self.subtitle_mode == "Sentence + Highlighting":
                    subtitle_file.write(
                        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                    )
                    subtitle_file.write(
                        "Style: Default,Arial,24,&H00FFFFFF,&H00808080,&H00000000,&H00404040,0,0,0,0,100,100,0,0,3,2,0,5,10,10,10,1\n\n"
                    )
                subtitle_file.write(
                    "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                )

                is_narrow = subtitle_format in ("ass_narrow", "ass_centered_narrow")
                is_centered = subtitle_format in (
                    "ass_centered_wide",
                    "ass_centered_narrow",
                )
                margin = "90" if is_narrow else ""
                alignment = "{\\an5}" if is_centered else ""

            # Load voice
            loaded_voice = (
                get_new_voice(tts, self.voice, self.use_gpu)
                if "*" in self.voice
                else self.voice
            )

            # Calculate initial audio buffer size from timed subtitles only
            max_end_time = max(
                (end for _, end, _ in subtitles if end is not None), default=0
            )
            audio_buffer = self.np.zeros(
                int(max_end_time * rate) + rate, dtype="float32"
            )

            # Process each subtitle and mix into buffer
            self.etr_start_time = time.time()
            srt_index = 1

            for idx, (start_time, end_time, text) in enumerate(subtitles, 1):
                if self.cancel_requested:
                    if subtitle_file:
                        subtitle_file.close()
                    self.conversion_finished.emit("Cancelled", None)
                    return

                # Process text and timing
                replace_nl = getattr(self, "replace_single_newlines", False)
                processed_text = text.replace("\n", " ") if replace_nl else text
                use_gaps = getattr(self, "use_silent_gaps", False)
                next_start = (
                    subtitles[idx][0]
                    if (use_gaps and idx < len(subtitles))
                    else float("inf")
                )
                subtitle_duration = None if end_time is None else end_time - start_time

                h1, m1, s1 = (
                    int(start_time // 3600),
                    int(start_time % 3600 // 60),
                    int(start_time % 60),
                )
                ms1 = int((start_time - int(start_time)) * 1000)
                is_last = use_gaps and idx == len(subtitles)
                if is_last:
                    time_str = (
                        f"{h1:02d}:{m1:02d}:{s1:02d}"
                        + (f",{ms1:03d}" if ms1 > 0 else "")
                        + " - AUTO"
                    )
                else:
                    h2, m2, s2 = (
                        int(end_time // 3600),
                        int(end_time % 3600 // 60),
                        int(end_time % 60),
                    )
                    ms2 = int((end_time - int(end_time)) * 1000)
                    time_str = (
                        f"{h1:02d}:{m1:02d}:{s1:02d}"
                        + (f",{ms1:03d}" if ms1 > 0 else "")
                        + " - "
                        + f"{h2:02d}:{m2:02d}:{s2:02d}"
                        + (f",{ms2:03d}" if ms2 > 0 else "")
                    )
                self.log_updated.emit(
                    f"\n[{idx}/{len(subtitles)}] {time_str}: {processed_text}"
                )

                # Generate TTS audio
                tts_results = [
                    r
                    for r in tts(
                        processed_text,
                        voice=loaded_voice,
                        speed=self.speed,
                        split_pattern=None,
                    )
                    if not self.cancel_requested
                ]
                audio_chunks = [r.audio for r in tts_results]

                if self.cancel_requested:
                    if subtitle_file:
                        subtitle_file.close()
                    self.conversion_finished.emit("Cancelled", None)
                    return

                # Concatenate audio and determine duration
                full_audio = (
                    self.np.concatenate(
                        [a.numpy() if hasattr(a, "numpy") else a for a in audio_chunks]
                    )
                    if audio_chunks
                    else self.np.zeros(
                        int((subtitle_duration or 0) * rate), dtype="float32"
                    )
                )
                audio_duration = len(full_audio) / rate

                # Use actual audio length for timing
                if use_gaps:
                    end_time = min(start_time + audio_duration, next_start)
                    subtitle_duration = end_time - start_time
                elif subtitle_duration is None:
                    subtitle_duration = audio_duration
                    end_time = start_time + audio_duration

                # Speed up if needed
                speedup_threshold = (
                    next_start - start_time if use_gaps else subtitle_duration
                )
                if audio_duration > speedup_threshold:
                    speed_factor = audio_duration / speedup_threshold

                    if getattr(self, "subtitle_speed_method", "tts") == "ffmpeg":
                        # FFmpeg time-stretch (faster processing)
                        self.log_updated.emit(
                            (f"  -> FFmpeg time-stretch: {speed_factor:.2f}x", "grey")
                        )

                        static_ffmpeg.add_paths()
                        num_stages = max(
                            1,
                            int(
                                self.np.ceil(
                                    self.np.log(speed_factor) / self.np.log(2.0)
                                )
                            ),
                        )
                        tempo = speed_factor ** (1.0 / num_stages)
                        filter_str = ",".join([f"atempo={tempo:.6f}"] * num_stages)

                        speed_proc = subprocess.Popen(
                            [
                                "ffmpeg",
                                "-y",
                                "-f",
                                "f32le",
                                "-ar",
                                str(rate),
                                "-ac",
                                "1",
                                "-i",
                                "pipe:0",
                                "-filter:a",
                                filter_str,
                                "-f",
                                "f32le",
                                "-ar",
                                str(rate),
                                "-ac",
                                "1",
                                "pipe:1",
                            ],
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )
                        full_audio = self.np.frombuffer(
                            speed_proc.communicate(input=full_audio.tobytes())[0],
                            dtype="float32",
                        )
                        audio_duration = len(full_audio) / rate
                    else:
                        # TTS regeneration (better quality)
                        new_speed = self.speed * speed_factor
                        self.log_updated.emit(
                            (f"  -> Regenerating at {new_speed:.2f}x speed", "grey")
                        )

                        tts_results = [
                            r
                            for r in tts(
                                processed_text,
                                voice=loaded_voice,
                                speed=new_speed,
                                split_pattern=None,
                            )
                            if not self.cancel_requested
                        ]
                        audio_chunks = [r.audio for r in tts_results]

                        full_audio = (
                            self.np.concatenate(
                                [
                                    a.numpy() if hasattr(a, "numpy") else a
                                    for a in audio_chunks
                                ]
                            )
                            if audio_chunks
                            else self.np.zeros(
                                int(subtitle_duration * rate), dtype="float32"
                            )
                        )
                        audio_duration = len(full_audio) / rate

                # Adjust duration after potential speed changes
                if use_gaps:
                    end_time = min(start_time + audio_duration, next_start)
                    subtitle_duration = end_time - start_time
                elif subtitle_duration is None:
                    subtitle_duration = audio_duration
                    end_time = start_time + audio_duration

                # Pad or trim to subtitle duration
                target_samples = int(subtitle_duration * rate)
                if len(full_audio) < target_samples:
                    full_audio = self.np.concatenate(
                        [
                            full_audio,
                            self.np.zeros(
                                target_samples - len(full_audio), dtype="float32"
                            ),
                        ]
                    )
                elif len(full_audio) > target_samples:
                    full_audio = full_audio[:target_samples]

                # Mix audio into buffer at the correct position (handles overlaps)
                start_sample = int(start_time * rate)
                end_sample = start_sample + len(full_audio)
                if end_sample > len(audio_buffer):
                    # Extend buffer if needed
                    audio_buffer = self.np.concatenate(
                        [
                            audio_buffer,
                            self.np.zeros(
                                end_sample - len(audio_buffer), dtype="float32"
                            ),
                        ]
                    )

                # Mix (add) the audio - this handles overlaps by combining them
                audio_buffer[start_sample:end_sample] += full_audio

                # Write subtitle
                if subtitle_file:
                    if "ass" in subtitle_format:
                        effect = (
                            "karaoke"
                            if self.subtitle_mode == "Sentence + Highlighting"
                            else ""
                        )
                        ass_text = (
                            processed_text
                            if replace_nl
                            else processed_text.replace("\n", "\\N")
                        )
                        subtitle_file.write(
                            f"Dialogue: 0,{self._ass_time(start_time)},{self._ass_time(end_time)},Default,,{margin},{margin},0,{effect},{alignment}{ass_text}\n"
                        )
                    else:
                        subtitle_file.write(
                            f"{srt_index}\n{self._srt_time(start_time)} --> {self._srt_time(end_time)}\n{processed_text}\n\n"
                        )
                        srt_index += 1

                # Update progress
                percent = min(int(idx / len(subtitles) * 100), 99)
                elapsed = time.time() - self.etr_start_time
                etr_str = (
                    "Processing..."
                    if elapsed <= 0.5
                    else f"{int(elapsed*(len(subtitles)-idx)/idx)//3600:02d}:{(int(elapsed*(len(subtitles)-idx)/idx)%3600)//60:02d}:{int(elapsed*(len(subtitles)-idx)/idx)%60:02d}"
                )
                self.progress_updated.emit(percent, etr_str)

            # Normalize audio buffer to prevent clipping from mixed overlaps
            max_amplitude = self.np.abs(audio_buffer).max()
            if max_amplitude > 1.0:
                self.log_updated.emit(
                    f"\n  -> Normalizing audio (peak: {max_amplitude:.2f})"
                )
                audio_buffer = audio_buffer / max_amplitude

            # Write the complete audio buffer
            self.log_updated.emit(("\nFinalizing audio. Please wait...", "grey"))
            if merged_out_file:
                merged_out_file.write(audio_buffer)
                merged_out_file.close()
            elif ffmpeg_proc:
                ffmpeg_proc.stdin.write(audio_buffer.astype("float32").tobytes())
                ffmpeg_proc.stdin.close()
                ffmpeg_proc.wait()

            if subtitle_file:
                subtitle_file.close()

            self.progress_updated.emit(100, "00:00:00")
            result_msg = f"\nAudiobook saved to: {merged_out_path}" + (
                f"\n\nSubtitle saved to: {subtitle_path}" if subtitle_path else ""
            )
            self.conversion_finished.emit((result_msg, "green"), merged_out_path)

        except Exception as e:
            try:
                if "ffmpeg_proc" in locals() and ffmpeg_proc:
                    ffmpeg_proc.stdin.close()
                    ffmpeg_proc.terminate()
                    ffmpeg_proc.wait()
                if "subtitle_file" in locals() and subtitle_file:
                    subtitle_file.close()
            except:
                pass
            self.log_updated.emit((f"Error processing subtitle file: {str(e)}", "red"))
            self.conversion_finished.emit(("Audio generation failed.", "red"), None)

    def set_chapter_options(self, options):
        """Set chapter options from the dialog and resume processing"""
        self.save_chapters_separately = options["save_chapters_separately"]
        self.merge_chapters_at_end = options["merge_chapters_at_end"]
        self.waiting_for_user_input = False
        self._chapter_options_event.set()

    def set_timestamp_response(self, treat_as_subtitle):
        """Set whether to treat timestamp text file as subtitle."""
        self._timestamp_response = treat_as_subtitle

    def _extract_and_add_metadata_tags_to_ffmpeg_cmd(self):
        """Extract metadata tags from text content and add them to ffmpeg command"""
        metadata_options = []

        # Get the input text (either direct or from file)
        text = ""
        if self.is_direct_text:
            text = self.file_name
        else:
            try:
                encoding = detect_encoding(self.file_name)
                with open(
                    self.file_name, "r", encoding=encoding, errors="replace"
                ) as file:
                    text = file.read()
            except Exception as e:
                self.log_updated.emit(
                    f"Warning: Could not read file for metadata extraction: {e}"
                )
                return []

        # Extract metadata tags using regex
        title_match = re.search(r"<<METADATA_TITLE:([^>]*)>>", text)
        artist_match = re.search(r"<<METADATA_ARTIST:([^>]*)>>", text)
        album_match = re.search(r"<<METADATA_ALBUM:([^>]*)>>", text)
        year_match = re.search(r"<<METADATA_YEAR:([^>]*)>>", text)
        album_artist_match = re.search(r"<<METADATA_ALBUM_ARTIST:([^>]*)>>", text)
        composer_match = re.search(r"<<METADATA_COMPOSER:([^>]*)>>", text)
        genre_match = re.search(r"<<METADATA_GENRE:([^>]*)>>", text)
        cover_match = re.search(r"<<METADATA_COVER_PATH:([^>]*)>>", text)
        cover_path = cover_match.group(1) if cover_match else None

        # Use display path or filename as fallback for title

        # Use file_name for logs if from_queue, otherwise use display_path if available
        if getattr(self, "from_queue", False):
            filename = os.path.splitext(os.path.basename(self.file_name))[0]
        else:
            filename = os.path.splitext(
                os.path.basename(
                    self.display_path if self.display_path else self.file_name
                )
            )[0]

        if title_match:
            metadata_options.extend(["-metadata", f"title={title_match.group(1)}"])
        else:
            metadata_options.extend(["-metadata", f"title={filename}"])

        # Add artist metadata
        if artist_match:
            metadata_options.extend(["-metadata", f"artist={artist_match.group(1)}"])
        else:
            metadata_options.extend(["-metadata", f"artist=Unknown"])

        # Add album metadata
        if album_match:
            metadata_options.extend(["-metadata", f"album={album_match.group(1)}"])
        else:
            metadata_options.extend(["-metadata", f"album={filename}"])

        # Add year metadata
        if year_match:
            metadata_options.extend(["-metadata", f"date={year_match.group(1)}"])
        else:
            # Use current year if year is not specified
            import datetime

            current_year = datetime.datetime.now().year
            metadata_options.extend(["-metadata", f"date={current_year}"])

        # Add album artist metadata
        if album_artist_match:
            metadata_options.extend(
                ["-metadata", f"album_artist={album_artist_match.group(1)}"]
            )
        else:
            metadata_options.extend(["-metadata", f"album_artist=Unknown"])

        # Add composer metadata
        if composer_match:
            metadata_options.extend(
                ["-metadata", f"composer={composer_match.group(1)}"]
            )
        else:
            metadata_options.extend(["-metadata", f"composer=Narrator"])

        # Add genre metadata
        if genre_match:
            metadata_options.extend(["-metadata", f"genre={genre_match.group(1)}"])
        else:
            metadata_options.extend(["-metadata", f"genre=Audiobook"])

        # Add these to ffmpeg command
        return metadata_options, cover_path

    def _srt_time(self, t):
        """Helper function to format time for SRT files"""
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int((t - int(t)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    def _ass_time(self, t):
        """Helper function to format time for ASS files"""
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        cs = int((t - int(t)) * 100)  # Centiseconds for ASS format
        return f"{h:01d}:{m:02d}:{s:02d}.{cs:02d}"

    def _process_subtitle_tokens(
        self,
        tokens_with_timestamps,
        subtitle_entries,
        max_subtitle_words,
        fallback_end_time=None,
    ):
        """Helper function to process subtitle tokens according to the subtitle mode"""
        if not tokens_with_timestamps:
            return

        processed_tokens = tokens_with_timestamps  # Use tokens directly

        # Use processed_tokens instead of tokens_with_timestamps for the rest of the method
        if self.subtitle_mode == "Sentence + Highlighting":
            # Sentence-based processing with karaoke highlighting
            # Use language-specific punctuation for CJK languages (without comma)
            lang_punct = self.LANGUAGE_PUNCTUATION.get(self.lang_code, {})
            separator = (
                lang_punct.get("sentence", r"[.!?]")
                if isinstance(lang_punct, dict)
                else r"[.!?]"
            )
            current_sentence = []
            word_count = 0

            for token in processed_tokens:  # Updated to use processed_tokens
                current_sentence.append(token)
                word_count += 1

                # Split sentences based on separator or word count
                if (
                    re.search(separator, token["text"]) and token["whitespace"] == " "
                ) or word_count >= max_subtitle_words:
                    if current_sentence:
                        # Create karaoke subtitle entry for this sentence
                        start_time = current_sentence[0]["start"]
                        end_time = current_sentence[-1]["end"]

                        # Generate karaoke text with background highlighting
                        karaoke_text = ""
                        for t in current_sentence:
                            # Calculate duration in centiseconds
                            duration = (
                                t["end"] - t["start"]
                                if t["end"] and t["start"]
                                else 0.5
                            )
                            duration_cs = int(duration * 100)
                            # Add karaoke effect - relies on style's SecondaryColour for highlighting
                            karaoke_text += f"{{\\kf{duration_cs}}}{t['text']}{t.get('whitespace', '') or ''}"

                        subtitle_entries.append(
                            (start_time, end_time, karaoke_text.strip())
                        )
                        current_sentence = []
                        word_count = 0

            # Add any remaining tokens as a sentence
            if current_sentence:
                start_time = current_sentence[0]["start"]
                end_time = current_sentence[-1]["end"]

                # Generate karaoke text for remaining tokens
                karaoke_text = ""
                for t in current_sentence:
                    duration = t["end"] - t["start"] if t["end"] and t["start"] else 0.5
                    duration_cs = int(duration * 100)
                    karaoke_text += f"{{\\kf{duration_cs}}}{t['text']}{t.get('whitespace', '') or ''}"
                subtitle_entries.append((start_time, end_time, karaoke_text.strip()))

            # Fallback for last entry
            if subtitle_entries and fallback_end_time is not None:
                last_entry = subtitle_entries[-1]
                start, end, text = last_entry
                if end is None or end <= start or end <= 0:
                    subtitle_entries[-1] = (start, fallback_end_time, text)

        elif self.subtitle_mode in ["Sentence", "Sentence + Comma", "Line"]:
            # Define separator pattern based on mode
            if self.subtitle_mode == "Line":
                separator = r"\n"
            elif self.subtitle_mode == "Sentence":
                # Use language-specific punctuation for CJK languages (without comma)
                lang_punct = self.LANGUAGE_PUNCTUATION.get(self.lang_code, {})
                separator = (
                    lang_punct.get("sentence", r"[.!?]")
                    if isinstance(lang_punct, dict)
                    else r"[.!?]"
                )
            else:  # Sentence + Comma
                # Use language-specific punctuation for CJK languages (with comma)
                lang_punct = self.LANGUAGE_PUNCTUATION.get(self.lang_code, {})
                separator = (
                    lang_punct.get("comma", r"[.!?,]")
                    if isinstance(lang_punct, dict)
                    else r"[.!?,]"
                )
            current_sentence = []
            word_count = 0

            for token in processed_tokens:  # Updated to use processed_tokens
                current_sentence.append(token)
                word_count += 1

                # Split sentences based on separator or word count
                if (
                    re.search(separator, token["text"]) and token["whitespace"] == " "
                ) or word_count >= max_subtitle_words:
                    if current_sentence:
                        # Create subtitle entry for this sentence
                        start_time = current_sentence[0]["start"]
                        end_time = current_sentence[-1]["end"]

                        # Simplified text joining logic
                        sentence_text = ""
                        for t in current_sentence:
                            sentence_text += t["text"] + (t.get("whitespace", "") or "")

                        subtitle_entries.append(
                            (start_time, end_time, sentence_text.strip())
                        )
                        current_sentence = []
                        word_count = 0

            # Add any remaining tokens as a sentence
            if current_sentence:
                start_time = current_sentence[0]["start"]
                end_time = current_sentence[-1]["end"]

                # Simplified text joining logic
                sentence_text = ""
                for t in current_sentence:
                    sentence_text += t["text"] + (t.get("whitespace", "") or "")
                subtitle_entries.append((start_time, end_time, sentence_text.strip()))

            # Fallback for last entry
            if subtitle_entries and fallback_end_time is not None:
                last_entry = subtitle_entries[-1]
                start, end, text = last_entry
                if end is None or end <= start or end <= 0:
                    subtitle_entries[-1] = (start, fallback_end_time, text)

        else:
            # Word count-based grouping - simply count spaces and split after N spaces
            try:
                word_count = int(self.subtitle_mode.split()[0])
                word_count = min(word_count, max_subtitle_words)
            except (ValueError, IndexError):
                word_count = 1

            current_group = []
            space_count = 0

            for token in processed_tokens:
                current_group.append(token)

                # Count spaces after tokens (in the whitespace field)
                if token.get("whitespace", "") == " ":
                    space_count += 1

                    # Split after counting N spaces
                    if space_count >= word_count:
                        text = "".join(
                            t["text"] + (t.get("whitespace", "") or "")
                            for t in current_group
                        )
                        subtitle_entries.append(
                            (
                                current_group[0]["start"],
                                current_group[-1]["end"],
                                text.strip(),
                            )
                        )
                        current_group = []
                        space_count = 0

            # Add any remaining tokens
            if current_group:
                text = "".join(
                    t["text"] + (t.get("whitespace", "") or "") for t in current_group
                )
                subtitle_entries.append(
                    (current_group[0]["start"], current_group[-1]["end"], text.strip())
                )

            # Fallback for last entry
            if subtitle_entries and fallback_end_time is not None:
                last_entry = subtitle_entries[-1]
                start, end, text = last_entry
                if end is None or end <= start or end <= 0:
                    subtitle_entries[-1] = (start, fallback_end_time, text)

    def cancel(self):
        self.cancel_requested = True
        self.should_cancel = True
        self.waiting_for_user_input = False
        # Terminate subprocess if running
        if self.process:
            try:
                self.process.terminate()
            except Exception:
                pass
        # Terminate ffmpeg subprocesses if running
        try:
            if hasattr(self, "ffmpeg_proc") and self.ffmpeg_proc:
                self.ffmpeg_proc.stdin.close()
                self.ffmpeg_proc.terminate()
                self.ffmpeg_proc.wait()
        except Exception:
            pass
        try:
            if hasattr(self, "chapter_ffmpeg_proc") and self.chapter_ffmpeg_proc:
                self.chapter_ffmpeg_proc.stdin.close()
                self.chapter_ffmpeg_proc.terminate()
                self.chapter_ffmpeg_proc.wait()
        except Exception:
            pass


class VoicePreviewThread(QThread):
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(
        self,
        np_module,
        kpipeline_class,
        lang_code,
        voice,
        speed,
        use_gpu=False,
        parent=None,
    ):
        super().__init__(parent)
        self.np_module = np_module
        self.kpipeline_class = kpipeline_class
        self.lang_code = lang_code
        self.voice = voice
        self.speed = speed
        self.use_gpu = use_gpu

        # Cache location for preview audio
        self.cache_dir = get_user_cache_path("preview_cache")

        # Calculate cache path
        self.cache_path = self._get_cache_path()

    def _get_cache_path(self):
        """Generate a unique filename for the voice with its parameters"""
        # For a voice formula, use a hash of the formula
        if "*" in self.voice:
            voice_id = (
                f"voice_formula_{hashlib.md5(self.voice.encode()).hexdigest()[:8]}"
            )
        else:
            voice_id = self.voice

        # Create a unique filename based on voice_id, language, and speed
        filename = f"{voice_id}_{self.lang_code}_{self.speed:.2f}.wav"
        return os.path.join(self.cache_dir, filename)

    def run(self):
        print(
            f"\nVoice: {self.voice}\nLanguage: {self.lang_code}\nSpeed: {self.speed}\nGPU: {self.use_gpu}\n"
        )

        # Generate the preview and save to cache
        try:

            # Set device based on use_gpu setting and platform
            if self.use_gpu:
                if platform.system() == "Darwin" and platform.processor() == "arm":
                    device = "mps"  # Use MPS for Apple Silicon
                else:
                    device = "cuda"  # Use CUDA for other platforms
            else:
                device = "cpu"

            tts = self.kpipeline_class(
                lang_code=self.lang_code, repo_id="hexgrad/Kokoro-82M", device=device
            )
            # Enable voice formula support for preview
            if "*" in self.voice:
                loaded_voice = get_new_voice(tts, self.voice, self.use_gpu)
            else:
                loaded_voice = self.voice
            sample_text = get_sample_voice_text(self.lang_code)
            audio_segments = []
            for result in tts(
                sample_text, voice=loaded_voice, speed=self.speed, split_pattern=None
            ):
                audio_segments.append(result.audio)
            if audio_segments:
                audio = self.np_module.concatenate(audio_segments)
                # Save directly to the cache path
                sf.write(self.cache_path, audio, 24000)
                self.temp_wav = self.cache_path
            self.finished.emit()
        except Exception as e:
            self.error.emit(f"Voice preview error: {str(e)}")


class PlayAudioThread(QThread):
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, wav_path, parent=None):
        super().__init__(parent)
        self.wav_path = wav_path
        self.is_canceled = False

    def run(self):
        try:
            import pygame
            import time as _time

            pygame.mixer.init()
            pygame.mixer.music.load(self.wav_path)
            pygame.mixer.music.play()
            # Wait until playback is finished or canceled
            while pygame.mixer.music.get_busy() and not self.is_canceled:
                _time.sleep(0.2)

            # Make sure to clean up regardless of how we exited the loop
            try:
                pygame.mixer.music.stop()
                pygame.mixer.music.unload()
                pygame.mixer.quit()  # Quit the mixer
            except Exception:
                # Ignore any errors during cleanup
                pass

            self.finished.emit()
        except Exception as e:
            # Handle initialization errors separately to give better error messages
            if "mixer not initialized" in str(e):
                self.error.emit(
                    "Audio playback error: The audio system was not properly initialized"
                )
            else:
                self.error.emit(f"Audio playback error: {str(e)}")

    def stop(self):
        """Safely stop playback"""
        self.is_canceled = True
        # Try to stop pygame if it's running, but catch all exceptions
        try:
            import pygame

            if pygame.mixer.get_init():
                if pygame.mixer.music.get_busy():
                    pygame.mixer.music.stop()
                pygame.mixer.music.unload()
        except Exception:
            # Ignore all errors when stopping since mixer might not be initialized
            pass
