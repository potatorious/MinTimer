"""Windows audio output. The cue scheduler and display share the device's sample clock."""

from array import array
import ctypes as c
from ctypes import wintypes as w
from math import pi, sin
from pathlib import Path
from threading import Event, Thread
import wave

RATE = 44100
FRAME_BYTES = 2
MAX_FRAMES = RATE * 600
DEFAULT_NAMES = ("tick.wav", "ding.wav", "chime.wav")
ANNOUNCEMENT_INTERVALS = (1, 60, 3600)


class WaveFormat(c.Structure):
    _pack_ = 1
    _fields_ = [
        ("format", w.WORD), ("channels", w.WORD), ("rate", w.DWORD),
        ("byte_rate", w.DWORD), ("alignment", w.WORD),
        ("bits", w.WORD), ("extra", w.WORD),
    ]


class WaveHeader(c.Structure):
    _fields_ = [
        ("data", c.c_void_p), ("length", w.DWORD), ("recorded", w.DWORD),
        ("user", c.c_size_t), ("flags", w.DWORD), ("loops", w.DWORD),
        ("next", c.c_void_p), ("reserved", c.c_size_t),
    ]


class Position(c.Structure):
    _fields_ = [("kind", w.UINT), ("value", w.DWORD), ("extra", w.DWORD)]


winmm = c.WinDLL("winmm")
kernel = c.WinDLL("kernel32", use_last_error=True)
winmm.waveOutOpen.argtypes = [
    c.POINTER(w.HANDLE), w.UINT, c.POINTER(WaveFormat),
    c.c_size_t, c.c_size_t, w.DWORD,
]
for operation in ("waveOutPause", "waveOutRestart", "waveOutReset", "waveOutClose"):
    getattr(winmm, operation).argtypes = [w.HANDLE]
for operation in ("waveOutPrepareHeader", "waveOutUnprepareHeader", "waveOutWrite"):
    getattr(winmm, operation).argtypes = [w.HANDLE, c.POINTER(WaveHeader), w.UINT]
winmm.waveOutGetPosition.argtypes = [w.HANDLE, c.POINTER(Position), w.UINT]
kernel.CreateEventW.argtypes = [c.c_void_p, w.BOOL, w.BOOL, w.LPCWSTR]
kernel.CreateEventW.restype = w.HANDLE
kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
kernel.WaitForSingleObject.restype = w.DWORD
kernel.SetEvent.argtypes = kernel.CloseHandle.argtypes = [w.HANDLE]


def checked(result):
    if result:
        raise OSError(f"오디오 출력 장치를 사용할 수 없습니다. (WinMM {result})")


def default_tone(index):
    duration = (0.055, 0.24, 0.55)[index]
    samples = array("h")
    for sample in range(int(RATE * duration)):
        t = sample / RATE
        phase = t - 0.25 if index == 2 and t >= 0.25 else t
        length = (0.25 if t < 0.25 else 0.30) if index == 2 else duration
        frequency = (1200, 880, 659.25 if t < 0.25 else 880)[index]
        envelope = min(1, phase / 0.005) * max(0, 1 - phase / length)
        samples.append(int(sin(2 * pi * frequency * phase) * envelope * 11000))
    return samples


def decode_file(path):
    if Path(path).suffix.lower() == ".wav":
        try:
            with wave.open(path, "rb") as source:
                if source.getnframes() / source.getframerate() > 600:
                    raise ValueError("음원은 최대 10분까지 재생할 수 있습니다.")
                if (source.getnchannels(), source.getsampwidth(), source.getframerate()) == (1, 2, RATE):
                    return array("h", source.readframes(source.getnframes()))
        except (wave.Error, EOFError):
            pass

    try:
        import av
    except ImportError as error:
        raise ValueError("이 음원 형식을 읽으려면 PyAV가 필요합니다. python -m pip install av") from error

    samples = array("h")
    with av.open(path) as source:
        stream = next(iter(source.streams.audio), None)
        if stream is None or stream.codec_context.channels not in (1, 2):
            raise ValueError("모노 또는 스테레오 음원을 선택해 주세요.")
        channels = stream.codec_context.channels
        resample = av.AudioResampler(format="s16", layout="mono" if channels == 1 else "stereo", rate=RATE)

        def append(frames):
            for frame in frames:
                if len(samples) + frame.samples > MAX_FRAMES:
                    raise ValueError("음원은 최대 10분까지 재생할 수 있습니다.")
                pcm = array("h", bytes(frame.planes[0])[:frame.samples * channels * FRAME_BYTES])
                if channels == 1:
                    samples.extend(pcm)
                else:
                    samples.extend(int((left + right) / 2) for left, right in zip(pcm[::2], pcm[1::2]))

        for frame in source.decode(stream):
            append(resample.resample(frame))
        append(resample.resample(None))
    return samples


class Clips:
    def __init__(self):
        self.cache = [None] * 3

    def get(self, index, path):
        key = ("", 0, 0)
        if path:
            info = Path(path).stat()
            key = (path, info.st_mtime_ns, info.st_size)
        cached = self.cache[index]
        if cached is not None and cached[0] == key:
            return cached[1]
        samples = decode_file(path) if path else default_tone(index)
        if not samples:
            raise ValueError("음원에 재생할 소리가 없습니다.")
        edge = min(88, len(samples) // 2)
        for i in range(edge):
            samples[i] = int(samples[i] * i / edge)
            samples[-1 - i] = int(samples[-1 - i] * i / edge)
        self.cache[index] = (key, samples)
        return samples


class Mixer:
    def __init__(self, clips, volumes, start_frame=None):
        self.clips = clips
        self.volumes = volumes
        self.frame = start_frame
        self.voice = -1
        self.cursor = 0
        self.gain = 0.0

    def trigger(self, index):
        if self.clips[index] is not None and self.volumes[index] > 0 and index >= self.voice:
            self.voice = index
            self.cursor = 0
            self.gain = self.volumes[index] / 100

    def render(self, count):
        block = array("h", [0]) * count
        out = 0
        while out < count:
            length = count - out
            if self.frame is not None:
                boundary = self.frame % RATE
                if self.frame and boundary == 0:
                    second = self.frame // RATE
                    for index in (2, 1, 0):
                        if self.clips[index] is not None and second % ANNOUNCEMENT_INTERVALS[index] == 0:
                            self.trigger(index)
                            break
                length = min(length, RATE - boundary)
            if self.voice >= 0:
                clip = self.clips[self.voice]
                part = clip[self.cursor:self.cursor + length]
                target = self.volumes[self.voice] / 100
                if self.gain == target:
                    scaled = array("h", (int(sample * target) for sample in part))
                else:
                    scaled = array("h")
                    for sample in part:
                        self.gain += max(-1 / 220, min(1 / 220, target - self.gain))
                        scaled.append(int(sample * self.gain))
                block[out:out + len(scaled)] = scaled
                self.cursor += len(part)
                if self.cursor == len(clip):
                    self.voice = -1
            if self.frame is not None:
                self.frame += length
            out += length
        return block


class WaveOutput:
    """Keep three 20 ms buffers queued; report frames consumed, not frames rendered."""

    chunk_frames = RATE // 50

    def __init__(self, mixer):
        self.mixer = mixer
        self.handle = w.HANDLE()
        self.buffers = []
        self.thread = None
        self.stop_event = Event()
        self.error = None
        self.signal = kernel.CreateEventW(None, False, False, None)
        if not self.signal:
            raise c.WinError(c.get_last_error())
        format_ = WaveFormat(1, 1, RATE, RATE * FRAME_BYTES, FRAME_BYTES, 16, 0)
        try:
            checked(winmm.waveOutOpen(c.byref(self.handle), 0xFFFFFFFF, c.byref(format_), self.signal, 0, 0x50000))
            for _ in range(3):
                raw = c.create_string_buffer(self.chunk_frames * FRAME_BYTES)
                header = WaveHeader(c.addressof(raw), len(raw))
                checked(winmm.waveOutPrepareHeader(self.handle, c.byref(header), c.sizeof(header)))
                self.buffers.append((raw, header))
            checked(winmm.waveOutPause(self.handle))
            for raw, header in self.buffers:
                self.queue(raw, header)
            checked(winmm.waveOutRestart(self.handle))
            self.thread = Thread(target=self.service, daemon=True)
            self.thread.start()
        except BaseException:
            self.close()
            raise

    def queue(self, raw, header):
        raw[:] = self.mixer.render(self.chunk_frames).tobytes()
        checked(winmm.waveOutWrite(self.handle, c.byref(header), c.sizeof(header)))

    def service(self):
        try:
            while not self.stop_event.is_set():
                if kernel.WaitForSingleObject(self.signal, 50) == 0xFFFFFFFF:
                    raise c.WinError(c.get_last_error())
                if self.stop_event.is_set():
                    break
                for raw, header in self.buffers:
                    if header.flags & 1:  # WHDR_DONE
                        self.queue(raw, header)
        except OSError as error:
            self.error = error

    def frames_played(self):
        if self.error is not None:
            raise self.error
        position = Position(2)  # TIME_SAMPLES
        checked(winmm.waveOutGetPosition(self.handle, c.byref(position), c.sizeof(position)))
        if position.kind == 4:  # TIME_BYTES
            return position.value // FRAME_BYTES
        if position.kind == 1:  # TIME_MS
            return position.value * RATE // 1000
        if position.kind != 2:
            raise OSError("오디오 장치의 재생 시간을 확인할 수 없습니다.")
        return position.value

    def close(self):
        self.stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            kernel.SetEvent(self.signal)
            self.thread.join()
        if self.handle:
            winmm.waveOutReset(self.handle)
            for _, header in self.buffers:
                winmm.waveOutUnprepareHeader(self.handle, c.byref(header), c.sizeof(header))
            winmm.waveOutClose(self.handle)
            self.handle = w.HANDLE()
        if self.signal:
            kernel.CloseHandle(self.signal)
            self.signal = None
